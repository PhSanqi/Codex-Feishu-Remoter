from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'src'))

from cfr.core.models import StructuredError
from cfr.feishu.config import FeishuSettings
from cfr.feishu.models import FeishuInboundMessage
from cfr.feishu.store import FeishuStore


class StoreTests(unittest.TestCase):
    def message(self, message_id='m', chat_id='chat'):
        return FeishuInboundMessage(None, message_id, chat_id, 'p2p', 'ou', 'user', 'text', 'hello')

    def test_inbox_claim_and_restart_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = FeishuStore(root / 'db.sqlite3')
            self.assertTrue(store.enqueue_message(self.message()))
            self.assertFalse(store.enqueue_message(self.message()))
            self.assertEqual(store.claim_next().status, 'running')
            cached = root / 'retry-after-restart.txt'
            cached.write_text('retry', encoding='utf-8')
            store.add_pending_attachment('chat', cached, 'file', cached.name)
            store.claim_pending_attachments('chat', 'm')
            store.recover_on_startup()
            self.assertEqual(store.get_inbox('m').status, 'interrupted_on_restart')
            self.assertEqual([item['name'] for item in store.list_pending_attachments('chat')], [cached.name])

    def test_reserved_redirect_row_can_be_rewritten_without_new_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FeishuStore(Path(directory) / 'db.sqlite3')
            self.assertTrue(store.enqueue_message(self.message('redirect')))
            self.assertTrue(store.rewrite_queued_text('redirect', 'NEW_DIRECTION'))
            record = store.get_inbox('redirect')
            self.assertEqual(record.status, 'queued')
            self.assertEqual(record.text_content, 'NEW_DIRECTION')
            self.assertFalse(store.rewrite_queued_text('missing', 'x'))

    def test_claimed_redirect_row_can_be_requeued_without_new_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FeishuStore(Path(directory) / 'db.sqlite3')
            self.assertTrue(store.enqueue_message(self.message('redirect-running')))
            self.assertEqual(store.claim_next('redirect-running').status, 'running')
            self.assertTrue(store.rewrite_queued_text('redirect-running', 'NEXT'))
            record = store.get_inbox('redirect-running')
            self.assertEqual(record.status, 'queued')
            self.assertIsNone(record.started_at)
            self.assertEqual(record.text_content, 'NEXT')

    def test_history_pruning_keeps_active_rows_and_returns_stale_cache_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = FeishuStore(root / 'db.sqlite3')
            old = time.time() - 1000
            for index in range(4):
                message = self.message(f'terminal-{index}')
                store.enqueue_message(message)
                store.mark_completed(message.message_id, f'reply-{index}')
                with store._connection() as connection:
                    connection.execute(
                        'update feishu_inbox set received_at=?,completed_at=? where message_id=?',
                        (old, old, message.message_id),
                    )
            store.enqueue_message(self.message('queued'))
            store.enqueue_message(self.message('running'))
            store.claim_next('running')
            cached = root / 'stale.txt'
            cached.write_text('stale', encoding='utf-8')
            store.add_pending_attachment('stale-chat', cached, 'file', cached.name)
            with store._connection() as connection:
                connection.execute('update feishu_pending_attachments set created_at=?', (old,))

            result = store.prune_history(
                now=time.time(), inbox_retention=100, attachment_retention=100,
                max_inbox=2, max_replies=0, max_approvals=0,
            )

            self.assertGreaterEqual(result['inbox'], 4)
            self.assertEqual(result['attachments'], 1)
            self.assertEqual(result['attachment_paths'], [str(cached.resolve())])
            self.assertEqual(store.get_inbox('queued').status, 'queued')
            self.assertEqual(store.get_inbox('running').status, 'running')
            self.assertIsNone(store.get_inbox('terminal-0'))

    def test_history_pruning_keeps_stale_attachment_needed_by_queued_message(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = FeishuStore(root / 'db.sqlite3')
            cached = root / 'needed.pdf'
            cached.write_bytes(b'pdf')
            attachment = store.add_pending_attachment('chat', cached, 'file', cached.name)
            with store._connection() as connection:
                connection.execute('update feishu_pending_attachments set created_at=?', (1.0,))
            message = self.message('queued-with-file', 'chat')
            store.enqueue_message(message)
            with store._connection() as connection:
                connection.execute('update feishu_inbox set received_at=? where message_id=?', (2.0, message.message_id))
            result = store.prune_history(now=10_000, attachment_retention=100)
            self.assertEqual(result['attachments'], 0)
            self.assertEqual(store.list_pending_attachments('chat')[0]['attachment_id'], attachment['attachment_id'])

    def test_pending_attachment_batch_is_claimed_once_per_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = FeishuStore(root / 'db.sqlite3')
            first = root / 'first.txt'
            second = root / 'second.txt'
            third = root / 'third.txt'
            for path in (first, second, third):
                path.write_text(path.stem, encoding='utf-8')
            store.add_pending_attachment('chat', first, 'file', first.name)
            store.add_pending_attachment('chat', second, 'file', second.name)
            claimed = store.claim_pending_attachments('chat', 'prompt-1')
            self.assertEqual([item['name'] for item in claimed], ['first.txt', 'second.txt'])
            self.assertEqual(store.list_pending_attachments('chat'), [])
            self.assertEqual(len(store.list_claimed_attachments('prompt-1')), 2)

            store.add_pending_attachment('chat', third, 'file', third.name)
            next_batch = store.claim_pending_attachments('chat', 'prompt-2')
            self.assertEqual([item['name'] for item in next_batch], ['third.txt'])
            self.assertEqual(len(store.list_claimed_attachments('prompt-1')), 2)

    def test_failed_prompt_can_release_claimed_attachments_for_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = FeishuStore(root / 'db.sqlite3')
            cached = root / 'retry.txt'
            cached.write_text('retry', encoding='utf-8')
            attachment = store.add_pending_attachment('chat', cached, 'file', cached.name)
            store.claim_pending_attachments('chat', 'prompt-failed')
            self.assertEqual(store.release_claimed_attachments('prompt-failed'), 1)
            pending = store.list_pending_attachments('chat')
            self.assertEqual([item['attachment_id'] for item in pending], [attachment['attachment_id']])
            self.assertEqual(store.list_claimed_attachments('prompt-failed'), [])

    def test_prompt_claim_does_not_steal_attachment_from_later_feishu_message(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = FeishuStore(root / 'db.sqlite3')
            before = root / 'before.txt'
            after = root / 'after.txt'
            before.write_text('before', encoding='utf-8')
            after.write_text('after', encoding='utf-8')
            for message_id in ('file-before', 'prompt', 'file-after'):
                store.enqueue_message(self.message(message_id, 'chat'))
            with store._connection() as connection:
                connection.execute('update feishu_inbox set received_at=? where message_id=?', (1.0, 'file-before'))
                connection.execute('update feishu_inbox set received_at=? where message_id=?', (2.0, 'prompt'))
                connection.execute('update feishu_inbox set received_at=? where message_id=?', (3.0, 'file-after'))
            store.add_pending_attachment('chat', before, 'file', before.name, source_message_id='file-before')
            store.add_pending_attachment('chat', after, 'file', after.name, source_message_id='file-after')

            claimed = store.claim_pending_attachments('chat', 'prompt')

            self.assertEqual([item['name'] for item in claimed], ['before.txt'])
            self.assertEqual([item['name'] for item in store.list_pending_attachments('chat')], ['after.txt'])

    def test_terminal_claimed_attachment_is_pruned_without_waiting_seven_days(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = FeishuStore(root / 'db.sqlite3')
            cached = root / 'claimed.txt'
            cached.write_text('claimed', encoding='utf-8')
            store.add_pending_attachment('chat', cached, 'file', cached.name)
            store.enqueue_message(self.message('prompt-claimed', 'chat'))
            store.claim_pending_attachments('chat', 'prompt-claimed')
            store.mark_failed('prompt-claimed', 'FAIL', 'failed')
            result = store.prune_history(now=time.time(), attachment_retention=7 * 24 * 60 * 60)
            self.assertEqual(result['attachments'], 1)
            self.assertEqual(result['attachment_paths'], [str(cached.resolve())])
            self.assertEqual(store.list_claimed_attachments('prompt-claimed'), [])

    def test_daemon_lease_is_fenced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db.sqlite3'
            first = FeishuStore(path)
            second = FeishuStore(path)
            first.acquire_daemon_lease('feishu:a', 'A', 1, ttl=30)
            with self.assertRaises(StructuredError) as caught:
                second.acquire_daemon_lease('feishu:a', 'B', 2, ttl=30)
            self.assertEqual(caught.exception.code, 'FEISHU_DAEMON_ALREADY_RUNNING')
            self.assertTrue(first.release_daemon_lease('feishu:a', 'A'))
            second.acquire_daemon_lease('feishu:a', 'B', 2, ttl=30)

    def test_reply_reservation_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FeishuStore(Path(directory) / 'db.sqlite3')
            self.assertTrue(store.reserve_reply('m', 'final', 0))
            self.assertFalse(store.reserve_reply('m', 'final', 0))
            store.set_reply_response('m', 'final', 0, 'reply-1')
            self.assertEqual(store.get_reply('m', 'final', 0), 'reply-1')

    def test_approval_resolution_is_atomic(self):
        from cfr.codex.approvals import ApprovalRequest
        with tempfile.TemporaryDirectory() as directory:
            store = FeishuStore(Path(directory) / 'db.sqlite3')
            request = ApprovalRequest('request', 'thread', None, None, 'command', None, 'echo', directory, (), ('accept', 'decline'))
            store.create_approval('approval', request, 'ou', time.time() + 60)
            self.assertTrue(store.resolve_approval('approval', 'accept'))
            self.assertFalse(store.resolve_approval('approval', 'decline'))

    def test_recent_approvals_is_bounded_and_deterministic(self):
        from cfr.codex.approvals import ApprovalRequest
        with tempfile.TemporaryDirectory() as directory:
            store = FeishuStore(Path(directory) / 'db.sqlite3')
            request = ApprovalRequest('request', 'thread', None, None, 'command', None, 'echo', directory, (), ('accept', 'decline'))
            for approval_id in ('approval-a', 'approval-b'):
                store.create_approval(approval_id, request, 'ou', time.time() + 60)
            approvals = store.list_recent_approvals(500)
            self.assertEqual([item['approval_id'] for item in approvals], ['approval-b', 'approval-a'])
            self.assertNotIn('requester_open_id', approvals[0])

    def test_surface_and_chat_binding_are_independent_from_codex_session(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FeishuStore(Path(directory) / 'db.sqlite3')
            self.assertEqual(store.get_selected_surface('chat'), 'code')
            store.set_selected_surface('chat', 'chat')
            binding = store.ensure_chat_binding('chat')
            store.update_chat_binding('chat', tab_id='tab-1', url='https://chatgpt.com/c/conversation-1')
            store.create_pending_session('chat', 'p2p', 'ou', directory)
            store.unbind_session('chat')
            restored = store.get_chat_binding('chat')
            self.assertEqual(store.get_selected_surface('chat'), 'chat')
            self.assertEqual(restored['tab_key'], binding['tab_key'])
            self.assertEqual(restored['tab_id'], 'tab-1')
            self.assertEqual(restored['url'], 'https://chatgpt.com/c/conversation-1')

    def test_surface_and_chat_binding_lists_support_control_state_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FeishuStore(Path(directory) / 'db.sqlite3')
            store.set_selected_surface('chat-a', 'chat')
            store.ensure_chat_binding('chat-a')
            store.update_chat_binding('chat-a', tab_id='7', url='https://chatgpt.com/c/conversation-a')
            self.assertEqual(store.list_surface_states()[0]['selected_surface'], 'chat')
            binding = store.list_chat_bindings()[0]
            self.assertEqual(binding['chat_id'], 'chat-a')
            self.assertEqual(binding['tab_id'], '7')
            self.assertEqual(binding['url'], 'https://chatgpt.com/c/conversation-a')

    def test_recent_control_chat_state_limits_by_chat_without_dropping_older_companion_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FeishuStore(Path(directory) / 'db.sqlite3')
            store.ensure_chat_binding('important')
            store.set_selected_surface('important', 'chat')
            store.create_pending_session('important', 'p2p', 'ou', directory)
            with store._connection() as connection:
                connection.execute("update feishu_chat_bindings set updated_at=1 where chat_id='important'")
                connection.execute("update feishu_surface_state set updated_at=1 where chat_id='important'")
                connection.execute("update feishu_sessions set updated_at=1000 where chat_id='important'")
            for index in range(5):
                chat_id = f'other-{index}'
                store.ensure_chat_binding(chat_id)
                with store._connection() as connection:
                    connection.execute('update feishu_chat_bindings set updated_at=? where chat_id=?', (100 + index, chat_id))
            state = store.recent_control_chat_state(limit=1)
        self.assertEqual([item.chat_id for item in state['sessions']], ['important'])
        self.assertEqual([item['chat_id'] for item in state['chat_bindings']], ['important'])
        self.assertEqual([item['chat_id'] for item in state['surface_states']], ['important'])

    def test_current_surface_tracks_most_recent_inbound_chat(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FeishuStore(Path(directory) / 'db.sqlite3')
            store.enqueue_message(self.message('m1', 'chat-a'))
            store.set_selected_surface('chat-a', 'chat')
            time.sleep(0.01)
            store.enqueue_message(self.message('m2', 'chat-b'))
            self.assertEqual(store.get_current_surface_state()['chat_id'], 'chat-b')
            self.assertEqual(store.get_current_surface_state()['selected_surface'], 'code')
            store.set_selected_surface('chat-b', 'chat')
            self.assertEqual(store.get_current_surface_state()['selected_surface'], 'chat')
            self.assertTrue(store.chat_exists('chat-a'))
            self.assertTrue(store.chat_exists('chat-b'))
            self.assertFalse(store.chat_exists('missing'))

    def test_pending_attachments_are_durable_and_clearable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'report.pdf'
            path.write_bytes(b'pdf')
            store = FeishuStore(root / 'db.sqlite3')
            added = store.add_pending_attachment('chat', path, 'file', 'report.pdf')
            restored = FeishuStore(root / 'db.sqlite3').list_pending_attachments('chat')
            self.assertEqual(restored[0]['attachment_id'], added['attachment_id'])
            self.assertEqual(restored[0]['path'], str(path.resolve()))
            store.clear_pending_attachments('chat', [added['attachment_id']])
            self.assertEqual(store.list_pending_attachments('chat'), [])

    def test_pending_thread_settings_survive_restart_and_clear_when_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db.sqlite3'
            store = FeishuStore(path)
            store.create_pending_session('chat', 'p2p', 'ou', directory)
            store.update_pending_thread_settings(
                'chat', model='gpt-5.6-sol', reasoning_effort='medium', service_tier='priority',
            )
            restored = FeishuStore(path)
            self.assertEqual(restored.get_pending_thread_settings('chat'), {
                'model': 'gpt-5.6-sol',
                'reasoning_effort': 'medium',
                'service_tier': 'priority',
            })
            restored.bind_session('chat', 'thread-1')
            self.assertEqual(restored.get_pending_thread_settings('chat'), {})

    def test_code_approval_mode_defaults_persists_and_survives_pending_session_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db.sqlite3'
            store = FeishuStore(path)
            store.create_pending_session('chat', 'p2p', 'ou', directory)
            self.assertEqual(store.get_approval_mode('chat'), 'ask')
            store.set_approval_mode('chat', 'auto')
            self.assertEqual(FeishuStore(path).get_approval_mode('chat'), 'auto')
            store.switch_to_pending_session('chat', 'p2p', 'ou', directory)
            self.assertEqual(store.get_approval_mode('chat'), 'auto')

    def test_file_database_initialization_is_cached_and_adds_query_indexes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db.sqlite3'
            original = FeishuStore._initialize
            calls = []

            def counted(store):
                calls.append(store.db_path)
                return original(store)

            with patch.object(FeishuStore, '_initialize', counted):
                FeishuStore(path)
                FeishuStore(path)
            self.assertEqual(calls, [path])
            import sqlite3
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(connection.execute('pragma journal_mode').fetchone()[0], 'wal')
                indexes = {row[0] for row in connection.execute(
                    "select name from sqlite_master where type='index' and name like 'idx_feishu_%'"
                )}
            finally:
                connection.close()
            self.assertIn('idx_feishu_inbox_status_received', indexes)
            self.assertIn('idx_feishu_approvals_turn_state', indexes)
            self.assertIn('idx_feishu_approvals_state_thread_turn', indexes)

    def test_pending_approval_turns_are_projected_in_one_query(self):
        from cfr.codex.approvals import ApprovalRequest
        with tempfile.TemporaryDirectory() as directory:
            store = FeishuStore(Path(directory) / 'db.sqlite3')
            pending = ApprovalRequest('request-a', 'thread-a', 'turn-a', None, 'command', None, 'echo', directory, (), ('accept', 'decline'))
            resolved = ApprovalRequest('request-b', 'thread-b', 'turn-b', None, 'command', None, 'echo', directory, (), ('accept', 'decline'))
            store.create_approval('approval-a', pending, 'ou', time.time() + 60)
            store.create_approval('approval-b', resolved, 'ou', time.time() + 60)
            store.resolve_approval('approval-b', 'accept')
            self.assertEqual(store.list_pending_approval_turns(), {('thread-a', 'turn-a')})


if __name__ == '__main__':
    unittest.main()
