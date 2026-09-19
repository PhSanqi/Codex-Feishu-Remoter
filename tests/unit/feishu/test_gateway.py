import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'scripts'))

from cfr.core.models import StructuredError
from cfr.feishu.commands import CommandParser
from cfr.feishu.config import FeishuSettings, load_settings
from cfr.feishu.credentials import LocalConfigStore
from cfr.feishu.daemon import FeishuDaemon
from cfr.feishu.gateway import FeishuGateway
from cfr.feishu.models import FeishuInboundMessage
from cfr.feishu.replies import chunk_text, deterministic_reply_uuid
from cfr.feishu.security import validate_file, validate_message_scope, validate_workspace
from cfr.feishu.store import FeishuStore
from cfr.feishu.transport import FakeFeishuTransport
import run_m2_feishu_gate as m2_gate


def payload(message_id, text, sender='ou-ok', sender_type='user', chat_type='p2p', mentioned_bot=False):
    return {'event': {'sender': {'sender_id': {'open_id': sender}, 'sender_type': sender_type}, 'message': {'message_id': message_id, 'chat_id': 'oc-1', 'chat_type': chat_type, 'message_type': 'text', 'content': json.dumps({'text': text}), 'mentioned_bot': mentioned_bot}}}


class GatewayTests(unittest.TestCase):
    def test_event_parser_rejects_malformed_envelopes_without_throwing(self):
        self.assertIsNone(FeishuInboundMessage.from_event(None))
        self.assertIsNone(FeishuInboundMessage.from_event({'event': 'invalid'}))
        self.assertIsNone(FeishuInboundMessage.from_event({'event': {'message': 'invalid'}}))

    def test_event_parser_ignores_malformed_mentions(self):
        event = payload('m-mentions', 'hello')
        event['event']['message']['mentions'] = [None, 'invalid', {'key': '@bot'}]
        event['event']['message']['content'] = json.dumps({'text': '@bot hello'})
        message = FeishuInboundMessage.from_event(event)
        self.assertEqual(message.text, 'hello')
        self.assertEqual(message.mentions, ({'key': '@bot'},))

    def test_event_parse_cleans_mentions_and_rejects_non_user(self):
        message = FeishuInboundMessage.from_event({'event': {'sender': {'sender_id': {'open_id': 'ou'}, 'sender_type': 'user'}, 'message': {'message_id': 'm', 'chat_id': 'c', 'message_type': 'text', 'content': json.dumps({'text': '@_bot_ hello'}), 'mentions': [{'key': '@_bot_'}]}}})
        self.assertEqual(message.text, 'hello')
        self.assertEqual(message.chat_type, 'unknown')
        self.assertFalse(FeishuInboundMessage.from_event(payload('m2', 'x', sender_type='app')).is_user)

    def test_gateway_dedupes_by_message_id(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = FeishuSettings('app', 'secret', ('ou-ok',), (Path(directory),), database=Path(directory) / 'db.sqlite3')
            store = FeishuStore(settings.database)
            gateway = FeishuGateway(settings, store)
            self.assertEqual(gateway.handle_message_event(payload('m', 'prompt'))['status'], 'queued')
            self.assertEqual(gateway.handle_message_event(payload('m', 'prompt'))['status'], 'deduped')

    def test_gateway_rejects_missing_identity_and_durably_ignores_empty_text(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = FeishuSettings('app', 'secret', ('ou-ok',), (Path(directory),), database=Path(directory) / 'db.sqlite3')
            store = FeishuStore(settings.database)
            gateway = FeishuGateway(settings, store)
            missing_chat = payload('m-missing-chat', 'prompt')
            missing_chat['event']['message']['chat_id'] = ''
            self.assertEqual(gateway.handle_message_event(missing_chat)['reason'], 'INVALID_MESSAGE')
            empty = payload('m-empty', '   ')
            self.assertEqual(gateway.handle_message_event(empty)['reason'], 'EMPTY_TEXT')
            self.assertEqual(store.get_inbox('m-empty').status, 'ignored')
            self.assertEqual(gateway.handle_message_event(empty)['status'], 'deduped')

    def test_gateway_callback_latency_history_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = FeishuSettings('app', 'secret', ('ou-ok',), (Path(directory),), database=Path(directory) / 'db.sqlite3')
            gateway = FeishuGateway(settings, FeishuStore(settings.database))
            for index in range(250):
                gateway.handle_message_event(payload(f'm-{index}', 'prompt'))
            self.assertEqual(gateway.metrics['received'], 250)
            self.assertEqual(len(gateway.metrics['callback_duration_ms']), 200)

    def test_immediate_slash_commands_are_durable_and_deduplicated(self):
        class Commands:
            def __init__(self): self.messages = []
            def handle_control_command(self, message): self.messages.append(message); return ['reply-1']
        with tempfile.TemporaryDirectory() as directory:
            settings = FeishuSettings('app', 'secret', ('ou-ok',), (Path(directory),), database=Path(directory) / 'db.sqlite3')
            daemon = Commands()
            store = FeishuStore(settings.database)
            result = FeishuGateway(settings, store, daemon).handle_message_event(payload('m', '/help'))
            self.assertEqual(result['status'], 'command')
            self.assertEqual(len(daemon.messages), 1)
            record = store.get_inbox('m')
            self.assertEqual(record.status, 'completed')
            self.assertEqual(record.response_message_id, 'reply-1')
            self.assertIsNone(store.claim_next())
            self.assertEqual(FeishuGateway(settings, store, daemon).handle_message_event(payload('m', '/help'))['status'], 'deduped')
            self.assertEqual(len(daemon.messages), 1)

    def test_callback_does_not_call_codex(self):
        class ForbiddenDaemon:
            def enqueue(self, _message_id):
                return None
        with tempfile.TemporaryDirectory() as directory:
            settings = FeishuSettings('app', 'secret', ('ou-ok',), (Path(directory),), database=Path(directory) / 'db.sqlite3')
            gateway = FeishuGateway(settings, FeishuStore(settings.database), ForbiddenDaemon())
            result = gateway.handle_message_event(payload('m', 'prompt'))
            self.assertEqual(result['status'], 'queued')

    def test_state_mutating_command_is_durably_serialized_with_chat_messages(self):
        class QueuingDaemon:
            parser = CommandParser()
            IMMEDIATE_COMMANDS = FeishuDaemon.IMMEDIATE_COMMANDS

            def __init__(self):
                self.enqueued = []
                self.direct = []

            def enqueue_for_chat(self, message_id, chat_id):
                self.enqueued.append((message_id, chat_id))

            def handle_control_command(self, message):
                self.direct.append(message.message_id)

        with tempfile.TemporaryDirectory() as directory:
            settings = FeishuSettings('app', 'secret', ('ou-ok',), (Path(directory),), database=Path(directory) / 'db.sqlite3')
            daemon = QueuingDaemon()
            store = FeishuStore(settings.database)
            result = FeishuGateway(settings, store, daemon).handle_message_event(payload('m', '/workspace 1'))
            self.assertEqual(result['status'], 'queued_command')
            self.assertEqual(daemon.enqueued, [('m', 'oc-1')])
            self.assertEqual(daemon.direct, [])
            self.assertEqual(store.get_inbox('m').status, 'queued')

    def test_rejected_redirect_is_not_left_queued_for_second_execution(self):
        class RejectingRedirect:
            parser = CommandParser()
            IMMEDIATE_COMMANDS = FeishuDaemon.IMMEDIATE_COMMANDS

            def handle_control_command(self, _message):
                return ['error-reply']

        with tempfile.TemporaryDirectory() as directory:
            settings = FeishuSettings('app', 'secret', ('ou-ok',), (Path(directory),), database=Path(directory) / 'db.sqlite3')
            store = FeishuStore(settings.database)
            gateway = FeishuGateway(settings, store, RejectingRedirect())
            result = gateway.handle_message_event(payload('redirect-1', '/redirect next'))
            record = store.get_inbox('redirect-1')
        self.assertEqual(result['status'], 'command')
        self.assertEqual(record.status, 'completed')
        self.assertEqual(record.response_message_id, 'error-reply')

    def test_group_default_is_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = FeishuSettings('app', 'secret', ('ou-ok',), (Path(directory),), database=Path(directory) / 'db.sqlite3')
            result = FeishuGateway(settings, FeishuStore(settings.database)).handle_message_event(payload('m', 'prompt', chat_type='group'))
            self.assertEqual(result['reason'], 'GROUPS_DISABLED')

    def test_group_requires_exact_bot_mention(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = FeishuSettings('app', 'secret', ('ou-ok',), (Path(directory),), enable_group_chats=True, database=Path(directory) / 'db.sqlite3')
            store = FeishuStore(settings.database)
            gateway = FeishuGateway(settings, store)
            self.assertEqual(gateway.handle_message_event(payload('m-no-bot', 'prompt', chat_type='group'))['reason'], 'GROUPS_DISABLED')
            self.assertEqual(gateway.handle_message_event(payload('m-bot', 'prompt', chat_type='group', mentioned_bot=True))['status'], 'queued')

    def test_image_message_is_durably_queued_with_resource_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = FeishuSettings('app', 'secret', ('ou-ok',), (Path(directory),), database=Path(directory) / 'db.sqlite3')
            store = FeishuStore(settings.database)
            gateway = FeishuGateway(settings, store)
            event = payload('img-1', '', sender='ou-ok')
            event['event']['message']['message_type'] = 'image'
            event['event']['message']['content'] = json.dumps({'image_key': 'img_real_1'})
            result = gateway.handle_message_event(event)
            self.assertEqual(result['status'], 'queued')
            record = store.get_inbox('img-1')
            self.assertEqual(record.message_type, 'image')
            self.assertIn('img_real_1', record.resources_json)

    def test_file_message_is_durably_queued_with_resource_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = FeishuSettings('app', 'secret', ('ou-ok',), (Path(directory),), database=Path(directory) / 'db.sqlite3')
            store = FeishuStore(settings.database)
            gateway = FeishuGateway(settings, store)
            event = payload('file-1', '', sender='ou-ok')
            event['event']['message']['message_type'] = 'file'
            event['event']['message']['content'] = json.dumps({'file_key': 'file_real_1', 'file_name': 'report.pdf'})
            result = gateway.handle_message_event(event)
            self.assertEqual(result['status'], 'queued')
            record = store.get_inbox('file-1')
            self.assertEqual(record.message_type, 'file')
            self.assertIn('file_real_1', record.resources_json)
            self.assertIn('report.pdf', record.resources_json)

    def test_video_message_is_durably_queued_with_resource_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = FeishuSettings('app', 'secret', ('ou-ok',), (Path(directory),), database=Path(directory) / 'db.sqlite3')
            store = FeishuStore(settings.database)
            gateway = FeishuGateway(settings, store)
            event = payload('video-1', '', sender='ou-ok')
            event['event']['message']['message_type'] = 'media'
            event['event']['message']['content'] = json.dumps({'file_key': 'video_real_1', 'file_name': 'clip.mp4'})
            result = gateway.handle_message_event(event)
            self.assertEqual(result['status'], 'queued')
            record = store.get_inbox('video-1')
            self.assertEqual(record.message_type, 'media')
            self.assertIn('video_real_1', record.resources_json)
            self.assertIn('clip.mp4', record.resources_json)

    def test_attachment_boundary_ignores_malformed_resource_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = FeishuSettings('app', 'secret', ('ou-ok',), (Path(directory),), database=Path(directory) / 'db.sqlite3')
            store = FeishuStore(settings.database)
            gateway = FeishuGateway(settings, store)
            message = FeishuInboundMessage(
                None, 'image-malformed', 'oc', 'p2p', 'ou-ok', 'user', 'image', None,
                resources=(None, 'invalid'),
            )
            result = gateway.handle_message_event(message)
            self.assertEqual(result['reason'], 'FEISHU_ATTACHMENT_RESOURCE_MISSING')
            self.assertEqual(store.get_inbox('image-malformed').status, 'ignored')


class DispatchWorkerTests(unittest.TestCase):
    def test_transient_scheduler_database_error_does_not_kill_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = FeishuSettings('app', 'secret', ('ou-ok',), (root,), worker_concurrency=1, database=root / 'db.sqlite3')
            daemon = FeishuDaemon(settings, FakeFeishuTransport(), adapter=object())
            message = FeishuInboundMessage(None, 'm-retry', 'chat-a', 'p2p', 'ou-ok', 'user', 'text', '/help')
            self.assertTrue(daemon.store.enqueue_message(message))
            original = daemon.store.next_queued_message_id
            calls = 0

            def transient(chat_id):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise sqlite3.OperationalError('busy')
                return original(chat_id)

            with patch.object(daemon.store, 'next_queued_message_id', side_effect=transient):
                worker = threading.Thread(target=daemon._worker, daemon=True)
                worker.start()
                daemon.enqueue_for_chat(message.message_id, message.chat_id)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and daemon.store.get_inbox(message.message_id).status != 'completed':
                    time.sleep(0.01)
                self.assertTrue(worker.is_alive())
                self.assertEqual(daemon.store.get_inbox(message.message_id).status, 'completed')
                self.assertEqual(daemon.runtime_snapshot()['recent_worker_failures'][-1]['error_type'], 'OperationalError')
                daemon.stop_event.set()
                worker.join(timeout=1)

    def test_duplicate_scheduler_token_does_not_clear_other_worker_chat_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = FeishuSettings('app', 'secret', ('ou-ok',), (root,), worker_concurrency=1, database=root / 'db.sqlite3')
            daemon = FeishuDaemon(settings, FakeFeishuTransport(), adapter=object())
            with daemon._queue_lock:
                daemon._active_chats.add('chat-a')
                daemon._scheduled_chats.add('chat-a')
            daemon.queue.put('chat-a')
            worker = threading.Thread(target=daemon._worker, daemon=True)
            worker.start()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and daemon.queue.unfinished_tasks:
                time.sleep(0.01)
            with daemon._queue_lock:
                self.assertIn('chat-a', daemon._active_chats)
            daemon.stop_event.set()
            worker.join(timeout=1)

    def test_busy_chat_does_not_block_independent_chat_dispatch(self):
        class BlockingDaemon(FeishuDaemon):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.chat_a_started = threading.Event()
                self.chat_b_started = threading.Event()
                self.release_chat_a = threading.Event()

            def _handle_prompt(self, message, record=None):
                if message.chat_id == 'chat-a':
                    self.chat_a_started.set()
                    self.release_chat_a.wait(1.0)
                else:
                    self.chat_b_started.set()
                return []

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = FeishuSettings(
                'app', 'secret', ('ou-ok',), (root,),
                worker_concurrency=4, database=root / 'db.sqlite3',
            )
            daemon = BlockingDaemon(settings, FakeFeishuTransport(), adapter=object())
            workers = [
                threading.Thread(target=daemon._worker, daemon=True)
                for _ in range(settings.worker_concurrency)
            ]
            for worker in workers:
                worker.start()
            try:
                messages = [
                    FeishuInboundMessage(None, f'a-{index}', 'chat-a', 'p2p', 'ou-ok', 'user', 'text', f'a-{index}')
                    for index in range(4)
                ] + [
                    FeishuInboundMessage(None, 'b-1', 'chat-b', 'p2p', 'ou-ok', 'user', 'text', 'b-1')
                ]
                for message in messages:
                    self.assertTrue(daemon.store.enqueue_message(message))
                    daemon.enqueue(message.message_id)

                self.assertTrue(daemon.chat_a_started.wait(0.5))
                independent_chat_started = daemon.chat_b_started.wait(0.25)
                chat_a_states = [daemon.store.get_inbox(f'a-{index}').status for index in range(4)]
            finally:
                daemon.release_chat_a.set()
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline:
                    if all(daemon.store.get_inbox(message.message_id).status == 'completed' for message in messages):
                        break
                    time.sleep(0.01)
                daemon.stop_event.set()
                for worker in workers:
                    worker.join(timeout=0.5)

            self.assertTrue(independent_chat_started)
            self.assertEqual(chat_a_states.count('running'), 1)
            self.assertEqual(chat_a_states.count('queued'), 3)

    def test_same_chat_messages_execute_in_fifo_order_without_parallelism(self):
        class OrderedDaemon(FeishuDaemon):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.order = []
                self.active = 0
                self.max_active = 0
                self.guard = threading.Lock()

            def _handle_prompt(self, message, record=None):
                with self.guard:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                self.order.append(message.message_id)
                time.sleep(0.01)
                with self.guard:
                    self.active -= 1
                return []

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = FeishuSettings('app', 'secret', ('ou-ok',), (root,), worker_concurrency=4, database=root / 'db.sqlite3')
            daemon = OrderedDaemon(settings, FakeFeishuTransport(), adapter=object())
            workers = [threading.Thread(target=daemon._worker, daemon=True) for _ in range(4)]
            for worker in workers:
                worker.start()
            messages = [FeishuInboundMessage(None, f'm-{index}', 'chat-a', 'p2p', 'ou-ok', 'user', 'text', str(index)) for index in range(5)]
            try:
                for message in messages:
                    daemon.store.enqueue_message(message)
                    daemon.enqueue_for_chat(message.message_id, message.chat_id)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and len(daemon.order) < len(messages):
                    time.sleep(0.01)
            finally:
                daemon.stop_event.set()
                for worker in workers:
                    worker.join(timeout=0.5)
        self.assertEqual(daemon.order, [message.message_id for message in messages])
        self.assertEqual(daemon.max_active, 1)


class ConfigSecurityTests(unittest.TestCase):
    def test_scope_whitelists_p2p_group_topic_and_denies_unknown(self):
        base = FeishuSettings('app', 'secret', ('ou-ok',), ())
        enabled = FeishuSettings('app', 'secret', ('ou-ok',), (), enable_group_chats=True)

        def message(chat_type, mentioned=False):
            return FeishuInboundMessage('e', 'm', 'c', chat_type, 'ou-ok', 'user', 'text', 'hello', mentioned_bot=mentioned)

        self.assertTrue(validate_message_scope(message('p2p'), base))
        self.assertFalse(validate_message_scope(message('group'), base))
        self.assertFalse(validate_message_scope(message('group'), enabled))
        self.assertTrue(validate_message_scope(message('group', True), enabled))
        self.assertFalse(validate_message_scope(message('topic'), base))
        self.assertFalse(validate_message_scope(message('topic'), enabled))
        self.assertTrue(validate_message_scope(message('topic', True), enabled))
        self.assertFalse(validate_message_scope(message('unknown'), enabled))
        self.assertFalse(validate_message_scope(message(''), enabled))
        self.assertFalse(validate_message_scope(message('future_type'), enabled))

    def test_missing_allowlists_are_not_execution_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings({'CFR_FEISHU_APP_ID': 'app', 'CFR_FEISHU_APP_SECRET': 'secret'}, config_store=LocalConfigStore(directory))
            with self.assertRaises(StructuredError) as caught:
                settings.validate_execution()
            self.assertEqual(caught.exception.code, 'FEISHU_OPERATOR_ALLOWLIST_REQUIRED')

    def test_unbounded_worker_configuration_is_rejected_before_thread_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(StructuredError) as caught:
                load_settings({
                    'CFR_FEISHU_APP_ID': 'app',
                    'CFR_FEISHU_APP_SECRET': 'secret',
                    'CFR_FEISHU_WORKER_CONCURRENCY': '1000',
                }, config_store=LocalConfigStore(directory))
            self.assertEqual(caught.exception.code, 'FEISHU_CONFIG_INVALID')

    def test_secret_is_redacted(self):
        settings = FeishuSettings('app', 'top-secret', (), ())
        self.assertNotIn('top-secret', repr(settings))

    def test_workspace_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'root'
            child = root / 'child'
            root.mkdir()
            child.mkdir()
            settings = FeishuSettings('app', 'secret', ('ou',), (root,))
            self.assertEqual(validate_workspace(child, settings), child.resolve())
            with self.assertRaises(StructuredError):
                validate_workspace(Path(directory), FeishuSettings('app', 'secret', ('ou',), (child,)))

    def test_file_upload_path_must_stay_inside_workspace_allowlist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'root'
            root.mkdir()
            allowed = root / 'allowed.txt'
            denied = Path(directory) / 'denied.txt'
            allowed.write_text('allowed', encoding='utf-8')
            denied.write_text('denied', encoding='utf-8')
            settings = FeishuSettings('app', 'secret', ('ou',), (root,))
            self.assertEqual(validate_file(allowed, settings), allowed.resolve())
            with self.assertRaises(StructuredError) as caught:
                validate_file(denied, settings)
            self.assertEqual(caught.exception.code, 'FEISHU_FILE_NOT_ALLOWED')


class UtilityTests(unittest.TestCase):
    def test_command_parser(self):
        parser = CommandParser()
        self.assertEqual(parser.parse('/cfr new C:\\A B').argument, 'C:\\A B')
        self.assertEqual(parser.parse('/cfr does-not-exist').name, 'unknown')
        self.assertEqual(parser.parse('/cfr does-not-exist').argument, '/cfr does-not-exist')
        self.assertIsNone(parser.parse('normal text'))

    def test_chunks_and_uuid_are_deterministic(self):
        self.assertEqual(len(chunk_text('x' * 25, 10)), 3)
        self.assertEqual(deterministic_reply_uuid('m', 'final', 1), deterministic_reply_uuid('m', 'final', 1))
        self.assertNotEqual(deterministic_reply_uuid('m', 'final', 1), deterministic_reply_uuid('m', 'final', 2))


class GateRoutingTests(unittest.TestCase):
    def test_shutdown_core_change_requires_host_regression(self):
        host_next, blockers, contract = m2_gate.resolve_gate_routing(
            shutdown_required=True,
            shutdown_satisfied=False,
            allow_ux_required=True,
            decline_ux_required=True,
            m1_required=True,
        )
        self.assertEqual(host_next, 'SHUTDOWN_HOST_REGRESSION')
        self.assertEqual(blockers[0], 'M2B_SHUTDOWN_HOST_REGRESSION_REQUIRED')
        self.assertEqual(contract, 'PASS')

    def test_shutdown_host_regression_has_priority_over_allow_ux(self):
        self.assertEqual(
            m2_gate.determine_host_next(
                shutdown_required=True,
                shutdown_satisfied=False,
                allow_ux_required=True,
            ),
            'SHUTDOWN_HOST_REGRESSION',
        )

    def test_implementation_ready_cannot_override_shutdown_regression(self):
        self.assertEqual(
            m2_gate.determine_host_next(
                deterministic_fail=False,
                shutdown_required=True,
                shutdown_satisfied=False,
                allow_ux_required=True,
            ),
            'SHUTDOWN_HOST_REGRESSION',
        )

    def test_historical_shutdown_pass_does_not_satisfy_new_core_change(self):
        self.assertEqual(m2_gate.HISTORICAL_SHUTDOWN_HOST_RUN_ID, '20260820T045904Z-26dffdbf')
        self.assertEqual(
            m2_gate.determine_host_next(shutdown_required=True, shutdown_satisfied=False),
            'SHUTDOWN_HOST_REGRESSION',
        )

    def test_current_blockers_keep_feedback_and_m1_after_shutdown_requirement(self):
        _, blockers, _ = m2_gate.resolve_gate_routing(
            shutdown_required=True,
            shutdown_satisfied=False,
            allow_ux_required=True,
            m1_required=True,
        )
        self.assertEqual(blockers, [
            'M2B_SHUTDOWN_HOST_REGRESSION_REQUIRED',
            'M2B_APPROVAL_DECISION_FEEDBACK_REQUIRED',
            'M1_FINAL_REGRESSION_AFTER_M2B_REQUIRED',
        ])

    def test_stale_historical_host_blockers_not_reintroduced(self):
        _, blockers, _ = m2_gate.resolve_gate_routing(
            shutdown_required=True,
            shutdown_satisfied=False,
            allow_ux_required=True,
            m1_required=True,
        )
        self.assertNotIn('M2_CODEX_INTEGRATION_HOST_REQUIRED', blockers)
        self.assertNotIn('FEISHU_CHANNEL_NATIVE_PROBE_REQUIRED', blockers)
        self.assertNotIn('FEISHU_PERSISTENT_CREDENTIALS_HOST_REQUIRED', blockers)

    def test_gate_reports_sdk_compat_as_shutdown_core_change(self):
        self.assertEqual(m2_gate.SHUTDOWN_CORE_FILES, ('src/cfr/feishu/sdk_compat.py', 'src/cfr/feishu/transport.py'))
        self.assertTrue(m2_gate.HISTORICAL_SHUTDOWN_HOST_RUN_ID)

    def test_current_report_baseline_is_not_stale(self):
        report = (Path(__file__).resolve().parents[3] / 'docs' / 'M2_FEISHU_GATEWAY_REPORT.md').read_text(encoding='utf-8')
        self.assertIn('M2LocalAcceptance: PASS (L01-L230)', report)
        self.assertIn('ShutdownLifecycleCoreChanges:', report)
        self.assertIn('HostNext: SHUTDOWN_HOST_REGRESSION', report)
        self.assertNotIn('HostNext: ALLOW_UX', report.split('## Current M2B Production Shutdown Host Routing', 1)[-1])


if __name__ == '__main__':
    unittest.main()
