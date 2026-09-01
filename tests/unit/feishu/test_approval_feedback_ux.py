from pathlib import Path
import tempfile
import threading
import time
import unittest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'src'))

from cfr.codex.approvals import ApprovalRequest
from cfr.feishu.approval_card import build_approval_feedback_card, inspect_approval_feedback_card
from cfr.feishu.approvals import ApprovalBridge
from cfr.feishu.config import FeishuSettings
from cfr.feishu.models import FeishuExecutionContext
from cfr.feishu.replies import FeishuReplyClient
from cfr.feishu.store import FeishuStore
from cfr.feishu.transport import FakeFeishuTransport
from cfr.core.models import StructuredError


class ApprovalFeedbackUxTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='cfr-feedback-test-')
        self.path = Path(self.directory.name) / 'feedback.sqlite3'
        self.store = FeishuStore(self.path)
        self.transport = FakeFeishuTransport()
        self.settings = FeishuSettings('app', 'secret', ('ou-operator',), (Path(self.directory.name),), database=self.path, approval_timeout_seconds=5)
        self.bridge = ApprovalBridge(self.store, FeishuReplyClient(self.transport, self.store), self.settings)

    def tearDown(self):
        self.bridge.close()
        self.store.close()
        self.directory.cleanup()

    @staticmethod
    def _request(kind='command'):
        return ApprovalRequest('request-feedback', 'thread-feedback', None, None, kind, 'safe', 'echo safe', '.', (), ('accept', 'decline'))

    def _start(self):
        values = []
        message = {'id': 'request-feedback', 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': 'thread-feedback', 'item': {'command': 'echo safe'}}}
        worker = threading.Thread(target=lambda: values.append(self.bridge.handle_server_request(message, FeishuExecutionContext('source-message', 'chat', 'ou-operator', 'thread-feedback'))))
        worker.start()
        row = None
        for _ in range(100):
            rows = self.store.find_approval_prefix('')
            if rows and rows[0].get('card_message_id'):
                row = rows[0]
                break
            time.sleep(0.01)
        self.assertIsNotNone(row)
        return row, worker, values

    def test_all_feedback_cards_are_v2_and_have_expected_callbacks(self):
        request = self._request()
        expected = {
            'PENDING': 2,
            'ACKNOWLEDGED_PROCESSING': 0,
            'APPROVED': 0,
            'DECLINED': 0,
            'EXECUTION_FAILED': 0,
        }
        for state, count in expected.items():
            card = build_approval_feedback_card(request, 'approval-feedback', state, 'accept' if state != 'PENDING' else None, 'Execution failed' if state == 'EXECUTION_FAILED' else None).payload
            evidence = inspect_approval_feedback_card(card)
            self.assertEqual(card['schema'], '2.0')
            self.assertFalse(evidence['ApprovalFeedbackLegacyActionTagPresent'])
            self.assertEqual(evidence['ApprovalFeedbackActiveButtons'], count)
            self.assertTrue(evidence['ApprovalFeedbackNoSecretFields'])

    def test_allow_feedback_updates_original_card_and_finalizes(self):
        row, worker, values = self._start()
        self.assertEqual(inspect_approval_feedback_card(self.transport.latest_card(row['card_message_id']))['ApprovalFeedbackActiveButtons'], 2)
        self.bridge.resolve(row['approval_id'], 'ou-operator', 'allow_once')
        worker.join(2)
        self.bridge.wait_for_feedback()
        ack = self.transport.latest_card(row['card_message_id'])
        self.assertEqual(inspect_approval_feedback_card(ack)['ApprovalFeedbackActiveButtons'], 0)
        self.assertIn('Processing', repr(ack))
        self.bridge.finalize_feedback(row['approval_id'])
        final = self.transport.latest_card(row['card_message_id'])
        self.assertEqual(self.store.get_approval(row['approval_id'])['feedback_state'], 'APPROVED')
        self.assertEqual(inspect_approval_feedback_card(final)['ApprovalFeedbackActiveButtons'], 0)
        self.assertEqual({item['message_id'] for item in self.transport.card_updates}, {row['card_message_id']})
        self.assertEqual(values, [{'decision': 'accept'}])

    def test_decline_feedback_is_terminal_and_has_no_buttons(self):
        row, worker, values = self._start()
        self.bridge.resolve(row['approval_id'], 'ou-operator', 'decline')
        worker.join(2)
        self.bridge.finalize_feedback(row['approval_id'])
        final = self.transport.latest_card(row['card_message_id'])
        current = self.store.get_approval(row['approval_id'])
        self.assertEqual(current['decision'], 'decline')
        self.assertEqual(current['feedback_state'], 'DECLINED')
        self.assertEqual(inspect_approval_feedback_card(final)['ApprovalFeedbackActiveButtons'], 0)
        self.assertIn('not executed', repr(final))

    def test_execution_failure_preserves_accept_decision(self):
        row, worker, _ = self._start()
        self.bridge.resolve(row['approval_id'], 'ou-operator', 'allow_once')
        worker.join(2)
        self.bridge.finalize_feedback(row['approval_id'], execution_succeeded=False, execution_error='command failed')
        current = self.store.get_approval(row['approval_id'])
        self.assertEqual(current['decision'], 'accept')
        self.assertEqual(current['feedback_state'], 'EXECUTION_FAILED')
        self.assertIn('Execution failed', repr(self.transport.latest_card(row['card_message_id'])))

    def test_wrong_operator_keeps_shared_card_pending(self):
        row, worker, _ = self._start()
        with self.assertRaises(StructuredError) as caught:
            self.bridge.resolve(row['approval_id'], 'ou-other', 'allow_once')
        self.assertEqual(caught.exception.code, 'FEISHU_APPROVAL_OPERATOR_MISMATCH')
        self.assertEqual(self.store.get_approval(row['approval_id'])['state'], 'pending')
        self.assertEqual(inspect_approval_feedback_card(self.transport.latest_card(row['card_message_id']))['ApprovalFeedbackActiveButtons'], 2)
        self.bridge.resolve(row['approval_id'], 'ou-operator', 'allow_once')
        worker.join(2)

    def test_duplicate_decisions_are_visible_and_do_not_regress(self):
        row, worker, _ = self._start()
        self.bridge.resolve(row['approval_id'], 'ou-operator', 'allow_once')
        worker.join(2)
        self.bridge.finalize_feedback(row['approval_id'])
        same = self.bridge.resolve(row['approval_id'], 'ou-operator', 'allow_once')
        opposite = self.bridge.resolve(row['approval_id'], 'ou-operator', 'decline')
        self.bridge.wait_for_feedback()
        current = self.store.get_approval(row['approval_id'])
        self.assertEqual(same['status'], 'ALREADY_RESOLVED')
        self.assertEqual(opposite['decision'], 'accept')
        self.assertEqual(current['decision'], 'accept')
        self.assertEqual(current['feedback_state'], 'APPROVED')
        self.assertIn('Already resolved', repr(self.transport.latest_card(row['card_message_id'])))
        self.assertEqual(inspect_approval_feedback_card(self.transport.latest_card(row['card_message_id']))['ApprovalFeedbackActiveButtons'], 0)

    def test_update_failure_does_not_rollback_durable_decision(self):
        class FailingTransport(FakeFeishuTransport):
            def update_card(self, message_id, card):
                raise StructuredError('FEISHU_API_PERMISSION_DENIED', 'update denied')

        self.transport = FailingTransport()
        self.bridge.close()
        self.bridge = ApprovalBridge(self.store, FeishuReplyClient(self.transport, self.store), self.settings)
        row, worker, _ = self._start()
        self.bridge.resolve(row['approval_id'], 'ou-operator', 'allow_once')
        worker.join(2)
        current = self.store.get_approval(row['approval_id'])
        self.assertEqual(current['decision'], 'accept')
        self.assertEqual(current['state'], 'approved')
        self.assertEqual(current['feedback_update_failed'], 1)

    def test_feedback_state_is_monotonic(self):
        row, worker, _ = self._start()
        self.bridge.resolve(row['approval_id'], 'ou-operator', 'allow_once')
        worker.join(2)
        self.bridge.finalize_feedback(row['approval_id'])
        self.assertFalse(self.store.transition_feedback(row['approval_id'], 'ACKNOWLEDGED_PROCESSING'))
        self.assertEqual(self.store.get_approval(row['approval_id'])['feedback_state'], 'APPROVED')


if __name__ == '__main__':
    unittest.main()
