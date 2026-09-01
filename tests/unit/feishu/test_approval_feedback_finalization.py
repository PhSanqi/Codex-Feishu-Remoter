from pathlib import Path
import json
import tempfile
import time
import unittest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'src'))

from cfr.core.models import ConversationResult, ThreadRef, TurnResult
from cfr.codex.approvals import ApprovalRequest
from cfr.feishu.approvals import ApprovalBridge, extract_turn_terminal_identity
from cfr.feishu.config import FeishuSettings
from cfr.feishu.replies import FeishuReplyClient
from cfr.feishu.store import FeishuStore
from cfr.feishu.transport import FakeFeishuTransport


class ApprovalFeedbackFinalizationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='cfr-feedback-finalization-')
        self.path = Path(self.directory.name) / 'feedback.sqlite3'
        self.store = FeishuStore(self.path)
        self.transport = FakeFeishuTransport()
        self.settings = FeishuSettings('app', 'secret', ('ou-operator',), (Path(self.directory.name),), database=self.path)
        self.bridge = ApprovalBridge(self.store, FeishuReplyClient(self.transport, self.store), self.settings)

    def tearDown(self):
        self.bridge.close()
        self.store.close()
        self.directory.cleanup()

    def _approval(self, approval_id, decision='accept', thread_id='thread-A', turn_id='turn-A'):
        request = ApprovalRequest(
            f'request-{approval_id}', thread_id, turn_id, 'item-1', 'command',
            'safe', 'echo safe', self.directory.name, (), ('accept', 'decline'),
        )
        self.store.create_approval(approval_id, request, 'ou-operator', time.time() + 60, json.dumps({'command': 'echo safe'}))
        self.store.bind_approval_card(approval_id, f'message-{approval_id}')
        self.store.resolve_approval(approval_id, decision, 'approved' if decision == 'accept' else 'declined')
        self.store.transition_feedback(approval_id, 'ACKNOWLEDGED_PROCESSING')
        self.bridge._requests[approval_id] = request
        return request

    def test_turn_completed_nested_turn_id_without_request_id_finalizes_by_turn_id(self):
        self._approval('approval-1')
        notification = {
            'method': 'turn/completed',
            'params': {'threadId': 'thread-A', 'turn': {'id': 'turn-A', 'status': 'completed', 'items': []}},
        }
        self.assertIsNone(notification['params'].get('requestId'))
        self.bridge.handle_server_notification(notification)
        row = self.store.get_approval('approval-1')
        self.assertEqual(row['feedback_state'], 'APPROVED')
        self.assertEqual(self.bridge.feedback_evidence('approval-1')['ApprovalFeedbackFinalizationAuthority'], 'TURN_NOTIFICATION_FALLBACK')

    def test_turn_identity_prefers_nested_turn_id(self):
        identity = extract_turn_terminal_identity({
            'method': 'turn/completed',
            'params': {'threadId': 'thread-A', 'turnId': 'wrong', 'turn': {'id': 'turn-A', 'status': 'completed'}},
        })
        self.assertEqual(identity, {'thread_id': 'thread-A', 'turn_id': 'turn-A', 'turn_status': 'completed'})

    def test_finalize_feedback_for_turn_maps_decline_and_failed_accept(self):
        self._approval('approval-decline', decision='decline')
        self._approval('approval-failed', decision='accept', turn_id='turn-B')
        self.bridge.finalize_feedback_for_turn(thread_id='thread-A', turn_id='turn-A', turn_status='completed')
        self.bridge.finalize_feedback_for_turn(thread_id='thread-A', turn_id='turn-B', turn_status='failed', error='command failed')
        self.assertEqual(self.store.get_approval('approval-decline')['feedback_state'], 'DECLINED')
        self.assertEqual(self.store.get_approval('approval-failed')['feedback_state'], 'EXECUTION_FAILED')

    def test_finalizer_is_idempotent_for_terminal_feedback(self):
        self._approval('approval-1')
        first = self.bridge.finalize_feedback_for_turn(thread_id='thread-A', turn_id='turn-A', turn_status='completed')
        update_count = len(self.transport.card_updates)
        second = self.bridge.finalize_feedback_for_turn(thread_id='thread-A', turn_id='turn-A', turn_status='completed')
        self.assertTrue(first['observed'])
        self.assertTrue(second['observed'])
        self.assertEqual(len(self.transport.card_updates), update_count)
        self.assertEqual(self.store.get_approval('approval-1')['feedback_state'], 'APPROVED')

    def test_same_turn_multiple_approvals_all_finalize(self):
        self._approval('approval-1')
        self._approval('approval-2', decision='decline')
        result = self.bridge.finalize_feedback_for_turn(thread_id='thread-A', turn_id='turn-A', turn_status='completed')
        self.assertEqual(result['matched_count'], 2)
        self.assertEqual(self.store.get_approval('approval-1')['feedback_state'], 'APPROVED')
        self.assertEqual(self.store.get_approval('approval-2')['feedback_state'], 'DECLINED')

    def test_wrong_thread_same_turn_id_does_not_match(self):
        self._approval('approval-1', thread_id='thread-A', turn_id='shared-turn')
        result = self.bridge.finalize_feedback_for_turn(thread_id='thread-B', turn_id='shared-turn', turn_status='completed')
        self.assertEqual(result['matched_count'], 0)
        self.assertEqual(self.store.get_approval('approval-1')['feedback_state'], 'ACKNOWLEDGED_PROCESSING')

    def test_production_daemon_finalizer_accepts_conversation_and_turn_results(self):
        request = self._approval('approval-1')
        daemon = object.__new__(__import__('cfr.feishu.daemon', fromlist=['FeishuDaemon']).FeishuDaemon)
        daemon.approvals = self.bridge
        daemon._finalize_turn_result(ConversationResult(ThreadRef('thread-A', 'name', Path(self.directory.name)), TurnResult('thread-A', 'turn-A', 'completed')))
        self.assertEqual(self.store.get_approval('approval-1')['feedback_state'], 'APPROVED')
        self.assertEqual(request.turn_id, 'turn-A')


if __name__ == '__main__':
    unittest.main()
