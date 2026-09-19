import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from cfr.core.models import TurnResult
from cfr.feishu.commands import CommandParser
from cfr.feishu.daemon import FeishuDaemon
from cfr.feishu.models import FeishuInboundMessage
from cfr.feishu.replies import FeishuReplyClient
from cfr.feishu.store import FeishuStore
from cfr.feishu.transport import FakeFeishuTransport


class _Replies:
    def __init__(self):
        self.text = []
        self.cards = []

    def reply_text(self, message_id, text, phase, **_kwargs):
        self.text.append((message_id, text, phase))
        return ['reply']

    def send_card(self, _message_id, _open_id, card, **_kwargs):
        self.cards.append(card)
        return 'card'

    def update_card(self, _card_id, _card):
        return 'card'


class _Registry:
    def __init__(self, active=True):
        self.active = SimpleNamespace(turn_id='turn-x') if active else None

    def get(self, _thread_id):
        return self.active


class _Adapter:
    def __init__(self, active=True, stop_status='STOP_REQUESTED'):
        self.registry = _Registry(active)
        self.stop_status = stop_status
        self.calls = []
        self.old_turn_id = 'turn-x'
        self.new_turn_id = 'turn-y'

    async def stop(self, thread_id):
        self.calls.append(('stop', thread_id))
        if self.stop_status == 'STOP_REQUESTED':
            self.registry.active = None
        return {'status': self.stop_status}

    async def send_message(self, thread_id, text, **kwargs):
        self.calls.append(('send_message', thread_id, text))
        kwargs['on_progress']({'state': 'running', 'thread_id': thread_id, 'turn_id': 'turn-y', 'steer_available': True, 'stage': 'running'})
        return TurnResult(thread_id, self.new_turn_id, 'completed', 'NEW_TURN_FINAL')


class RedirectTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = FeishuStore(Path(self.directory.name) / 'cfr.sqlite3')
        self.store.create_pending_session('chat', 'p2p', 'user', Path(self.directory.name))
        self.store.bind_session('chat', 'thread-t')
        self.daemon = FeishuDaemon.__new__(FeishuDaemon)
        self.daemon.parser = CommandParser()
        self.daemon.replies = _Replies()
        self.daemon.store = self.store
        self.daemon.settings = SimpleNamespace(allowed_workspace_roots=(Path(self.directory.name),))
        self.daemon.adapter = _Adapter()
        self.daemon.binding_store = SimpleNamespace(get_binding=lambda _thread: SimpleNamespace(cwd=Path(self.directory.name)))
        self.daemon.approvals = SimpleNamespace(finalize_feedback_for_turn=lambda **_kwargs: None)
        self.daemon._locks = {}
        self.daemon._locks_guard = threading.RLock()
        self.queued = []
        self.daemon.enqueue = lambda message_id, _chat_id=None: self.queued.append(message_id)
        self.message = FeishuInboundMessage('event', 'message', 'chat', 'p2p', 'user', 'user', 'text', '/redirect NEW_DIRECTION')

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_parser_supports_redirect_and_requires_argument(self):
        parser = CommandParser()
        self.assertEqual(parser.parse('/redirect NEW_DIRECTION').argument, 'NEW_DIRECTION')
        self.assertEqual(parser.parse('/cfr redirect NEW_DIRECTION').argument, 'NEW_DIRECTION')
        with self.assertRaisesRegex(Exception, 'FEISHU_REDIRECT_REQUIRED'):
            self.daemon._handle_command(self.message, parser.parse('/redirect'))

    def test_redirect_requires_active_turn_without_queueing(self):
        self.daemon.adapter = _Adapter(active=False)
        self.daemon._handle_command(self.message, CommandParser().parse('/redirect NEW_DIRECTION'))
        self.assertEqual(self.daemon.adapter.calls, [])
        self.assertIsNone(self.store.get_inbox('message'))
        self.assertEqual(self.queued, [])
        self.assertIn('当前没有可重定向的活动任务', self.daemon.replies.text[-1][1])

    def test_redirect_interrupts_then_queues_raw_direction_without_direct_send(self):
        order = []
        enqueue_message = self.store.enqueue_message

        async def stop(thread_id):
            order.append('stop')
            self.daemon.adapter.registry.active = None
            return {'status': 'STOP_REQUESTED'}

        self.daemon.adapter.stop = stop
        self.store.enqueue_message = lambda message: (order.append('queue') or enqueue_message(message))
        self.daemon._handle_command(self.message, CommandParser().parse('/redirect NEW_DIRECTION'))
        record = self.store.get_inbox('message')
        self.assertEqual(order, ['stop', 'queue'])
        self.assertEqual(record.text_content, 'NEW_DIRECTION')
        self.assertEqual((record.chat_id, record.sender_open_id), ('chat', 'user'))
        self.assertEqual(self.queued, ['message'])
        self.assertFalse(any(call[0] == 'send_message' for call in self.daemon.adapter.calls))

    def test_redirect_interrupt_failure_fails_closed_without_queueing(self):
        self.daemon.adapter = _Adapter(stop_status='CFR_DAEMON_REQUIRED_FOR_STOP')
        with self.assertRaisesRegex(Exception, 'FEISHU_REDIRECT_INTERRUPT_FAILED'):
            self.daemon._handle_command(self.message, CommandParser().parse('/redirect NEW_DIRECTION'))
        self.assertIsNone(self.store.get_inbox('message'))
        self.assertEqual(self.queued, [])

    def test_queued_redirect_reuses_normal_path_for_new_turn_and_reply_phases(self):
        self.daemon._handle_command(self.message, CommandParser().parse('/redirect NEW_DIRECTION'))
        self.daemon.process_one()
        self.assertIn(('send_message', 'thread-t', 'NEW_DIRECTION'), self.daemon.adapter.calls)
        self.assertNotEqual(self.daemon.adapter.old_turn_id, self.daemon.adapter.new_turn_id)
        self.assertEqual(self.daemon.replies.cards[0]['header']['title']['content'], 'CFR · Codex · 执行过程')
        self.assertEqual(self.daemon.replies.text[-1][1], 'NEW_TURN_FINAL')

        transport = FakeFeishuTransport()
        replies = FeishuReplyClient(transport, self.store)
        replies.reply_text('phase-message', 'status', 'status', chat_id='chat')
        replies.send_card('phase-message', 'user', {'schema': '2.0', 'body': {'elements': []}}, phase='progress')
        replies.reply_text('phase-message', 'final', 'final', chat_id='chat')
        self.assertIsNotNone(self.store.get_reply_record('phase-message', 'status', 0))
        self.assertIsNotNone(self.store.get_reply_record('phase-message', 'progress', 0))
        self.assertIsNotNone(self.store.get_reply_record('phase-message', 'final', 0))


if __name__ == '__main__':
    unittest.main()
