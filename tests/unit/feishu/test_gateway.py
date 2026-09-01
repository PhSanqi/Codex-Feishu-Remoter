import json
from pathlib import Path
import tempfile
import unittest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'src'))

from cfr.core.models import StructuredError
from cfr.feishu.commands import CommandParser
from cfr.feishu.config import FeishuSettings, load_settings
from cfr.feishu.credentials import LocalConfigStore
from cfr.feishu.gateway import FeishuGateway
from cfr.feishu.models import FeishuInboundMessage
from cfr.feishu.replies import chunk_text, deterministic_reply_uuid
from cfr.feishu.security import validate_message_scope, validate_workspace
from cfr.feishu.store import FeishuStore
from cfr.feishu.transport import FakeFeishuTransport


def payload(message_id, text, sender='ou-ok', sender_type='user', chat_type='p2p', mentioned_bot=False):
    return {'event': {'sender': {'sender_id': {'open_id': sender}, 'sender_type': sender_type}, 'message': {'message_id': message_id, 'chat_id': 'oc-1', 'chat_type': chat_type, 'message_type': 'text', 'content': json.dumps({'text': text}), 'mentioned_bot': mentioned_bot}}}


class GatewayTests(unittest.TestCase):
    def test_event_parse_cleans_mentions_and_rejects_non_user(self):
        message = FeishuInboundMessage.from_event({'event': {'sender': {'sender_id': {'open_id': 'ou'}, 'sender_type': 'user'}, 'message': {'message_id': 'm', 'chat_id': 'c', 'message_type': 'text', 'content': json.dumps({'text': '@_bot_ hello'}), 'mentions': [{'key': '@_bot_'}]}}})
        self.assertEqual(message.text, 'hello')
        self.assertFalse(FeishuInboundMessage.from_event(payload('m2', 'x', sender_type='app')).is_user)

    def test_gateway_dedupes_by_message_id(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = FeishuSettings('app', 'secret', ('ou-ok',), (Path(directory),), database=Path(directory) / 'db.sqlite3')
            store = FeishuStore(settings.database)
            gateway = FeishuGateway(settings, store)
            self.assertEqual(gateway.handle_message_event(payload('m', 'prompt'))['status'], 'queued')
            self.assertEqual(gateway.handle_message_event(payload('m', 'prompt'))['status'], 'deduped')

    def test_slash_commands_do_not_enter_durable_inbox(self):
        class Commands:
            def __init__(self): self.messages = []
            def handle_control_command(self, message): self.messages.append(message)
        with tempfile.TemporaryDirectory() as directory:
            settings = FeishuSettings('app', 'secret', ('ou-ok',), (Path(directory),), database=Path(directory) / 'db.sqlite3')
            daemon = Commands()
            store = FeishuStore(settings.database)
            result = FeishuGateway(settings, store, daemon).handle_message_event(payload('m', '/help'))
            self.assertEqual(result['status'], 'command')
            self.assertEqual(len(daemon.messages), 1)
            self.assertIsNone(store.claim_next())

    def test_callback_does_not_call_codex(self):
        class ForbiddenDaemon:
            def enqueue(self, _message_id):
                return None
        with tempfile.TemporaryDirectory() as directory:
            settings = FeishuSettings('app', 'secret', ('ou-ok',), (Path(directory),), database=Path(directory) / 'db.sqlite3')
            gateway = FeishuGateway(settings, FeishuStore(settings.database), ForbiddenDaemon())
            result = gateway.handle_message_event(payload('m', 'prompt'))
            self.assertEqual(result['status'], 'queued')

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


class UtilityTests(unittest.TestCase):
    def test_command_parser(self):
        parser = CommandParser()
        self.assertEqual(parser.parse('/cfr new C:\\A B').argument, 'C:\\A B')
        self.assertIsNone(parser.parse('normal text'))

    def test_chunks_and_uuid_are_deterministic(self):
        self.assertEqual(len(chunk_text('x' * 25, 10)), 3)
        self.assertEqual(deterministic_reply_uuid('m', 'final', 1), deterministic_reply_uuid('m', 'final', 1))
        self.assertNotEqual(deterministic_reply_uuid('m', 'final', 1), deterministic_reply_uuid('m', 'final', 2))


if __name__ == '__main__':
    unittest.main()
