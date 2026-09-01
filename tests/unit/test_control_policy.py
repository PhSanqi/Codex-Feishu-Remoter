from types import SimpleNamespace
import unittest

from cfr.control.supervisor import CfrSupervisor
from cfr.feishu.models import FeishuInboundMessage
from cfr.feishu.security import authorize_sender, validate_message_scope


class _Settings:
    allowed_open_ids = {'user-1'}
    enable_group_chats = False

    def validate_execution(self):
        return None


class _Transport:
    def __init__(self, _settings):
        self.is_running = False
        self.connection_state = 'stopped'
        self.message_handler = None
        self.card_handler = None
        self.replies = []

    def connect_until_ready(self, message_handler, card_handler, **_kwargs):
        self.message_handler = message_handler
        self.card_handler = card_handler
        self.is_running = True
        self.connection_state = 'ready'

    def reply_text(self, message_id, chat_id, text, uuid):
        self.replies.append((message_id, chat_id, text, uuid))


class _Daemon:
    def __init__(self, _settings, transport):
        self.transport = transport
        self.store = object()
        self._started = False
        self.stop_calls = 0

    def start(self, **_kwargs):
        self._started = True

    def stop(self):
        self.stop_calls += 1
        self._started = False
        self.transport.is_running = False


class _Gateway:
    def __init__(self, settings, *_args):
        self.settings = settings
        self.gateway_calls = []
        self.messages = []
        self.cards = []

    def handle_message_event(self, message, event_id=None):
        self.gateway_calls.append((message, event_id))
        if not authorize_sender(message, self.settings) or not validate_message_scope(message, self.settings):
            return {'status': 'ignored', 'reason': 'UNAUTHORIZED_SENDER'}
        self.messages.append((message, event_id))
        return {'status': 'queued', 'message_id': message.message_id}

    def handle_card_action(self, payload):
        self.cards.append(payload)
        return {'status': 'resolved'}


def _message(text):
    return FeishuInboundMessage(
        event_id='evt-1', message_id='msg-1', chat_id='chat-1', chat_type='p2p',
        sender_open_id='user-1', sender_type='user', message_type='text', text=text,
    )


class RuntimeAdmissionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.transports = []

        def transport_factory(*args):
            transport = _Transport(*args)
            self.transports.append(transport)
            return transport

        self.supervisor = CfrSupervisor(
            settings_loader=lambda **_kwargs: _Settings(),
            transport_factory=transport_factory,
            daemon_factory=_Daemon,
            gateway_factory=_Gateway,
        )
        self.assertEqual(self.supervisor.start_feishu().status, 'ok')
        self.transport = self.transports[0]

    def test_accept_new_tasks_false_rejects_new_execution(self):
        self.supervisor.set_accept_new_tasks(False)
        result = self.transport.message_handler(_message('write a report'), 'evt-1')
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(result['reason'], 'CONTROL_NOT_ACCEPTING_NEW_TASKS')
        self.assertEqual(self.supervisor._gateway.messages, [])
        self.assertEqual(self.transport.replies[0][2], 'CFR 当前暂停接受新任务。')

    def test_remote_execution_false_rejects_new_execution(self):
        self.supervisor.set_remote_execution(False)
        result = self.transport.message_handler(_message('run this task'), 'evt-1')
        self.assertEqual(result['reason'], 'CONTROL_NOT_ACCEPTING_NEW_TASKS')
        self.assertEqual(self.supervisor._gateway.messages, [])

    def test_approval_callback_still_allowed_when_new_tasks_disabled(self):
        self.supervisor.set_accept_new_tasks(False)
        result = self.transport.card_handler(SimpleNamespace(approval_id='approval-1'))
        self.assertEqual(result['status'], 'resolved')
        self.assertEqual(len(self.supervisor._gateway.cards), 1)

    def test_existing_work_is_not_interrupted_by_admission_toggle(self):
        daemon = self.supervisor._daemon
        self.supervisor.set_accept_new_tasks(False)
        self.supervisor.set_remote_execution(False)
        self.assertTrue(daemon._started)
        self.assertTrue(self.transport.is_running)
        self.assertEqual(daemon.stop_calls, 0)
        self.assertEqual(self.transport.message_handler(_message('/cfr status'), 'evt-2')['status'], 'queued')

    def test_unauthorized_sender_gets_no_admission_feedback(self):
        self.supervisor.set_accept_new_tasks(False)
        unauthorized = FeishuInboundMessage(
            event_id='evt-unauthorized', message_id='msg-unauthorized', chat_id='chat-1', chat_type='p2p',
            sender_open_id='not-allowed', sender_type='user', message_type='text', text='write a report',
        )
        result = self.transport.message_handler(unauthorized, 'evt-unauthorized')
        self.assertEqual(result['reason'], 'UNAUTHORIZED_SENDER')
        self.assertEqual(len(self.supervisor._gateway.gateway_calls), 1)
        self.assertEqual(self.supervisor._gateway.messages, [])
        self.assertEqual(self.transport.replies, [])

    def test_authorized_sender_gets_admission_feedback(self):
        self.supervisor.set_accept_new_tasks(False)
        result = self.transport.message_handler(_message('write a report'), 'evt-1')
        self.assertEqual(result['reason'], 'CONTROL_NOT_ACCEPTING_NEW_TASKS')
        self.assertEqual(self.supervisor._gateway.messages, [])
        self.assertEqual(self.transport.replies[0][2], 'CFR 当前暂停接受新任务。')

    def test_authorized_admission_enabled_still_queues(self):
        result = self.transport.message_handler(_message('write a report'), 'evt-1')
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(len(self.supervisor._gateway.messages), 1)

    def test_authorized_command_records_safe_dispatch_events(self):
        result = self.transport.message_handler(_message('/reasoning'), 'evt-1')
        events = self.supervisor.activity()
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(
            [(item['event'], item.get('stage')) for item in events[-2:]],
            [('FEISHU_COMMAND_RECEIVED', 'command_dispatch'), ('FEISHU_COMMAND_COMPLETED', 'command_dispatch')],
        )
        self.assertNotIn('sender_open_id', str(events[-2:]))

    def test_command_error_is_recorded_safely_and_propagates(self):
        self.supervisor._gateway.handle_message_event = lambda *_args: (_ for _ in ()).throw(RuntimeError('unexpected'))
        with self.assertRaisesRegex(RuntimeError, 'unexpected'):
            self.transport.message_handler(_message('/reasoning secret-argument'), 'evt-1')
        event = self.supervisor.activity()[-1]
        self.assertEqual((event['event'], event['level'], event['stage'], event['code']), ('FEISHU_COMMAND_ERROR', 'error', 'command_dispatch', 'FEISHU_COMMAND_RUNTIME_ERROR'))
        self.assertNotIn('secret-argument', event['message'])
