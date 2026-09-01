import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from cfr.codex.approvals import ApprovalRequest
from cfr.control.read_model import ControlReadModel
from cfr.feishu.store import FeishuStore


class ApprovalReadModelTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / 'cfr.sqlite3'
        self.store = FeishuStore(self.database)
        self.model = ControlReadModel(object(), self.database)

    def tearDown(self):
        self.directory.cleanup()

    def _seed(self, approval_id='approval-1'):
        request = ApprovalRequest(
            'request-sensitive', 'thread-1', 'turn-1', 'item-1', 'command', 'safe reason', 'echo sensitive',
            Path(self.directory.name), (), ('accept', 'decline'),
        )
        self.store.create_approval(approval_id, request, 'requester-sensitive', time.time() + 60, json.dumps({'command': 'echo sensitive', 'cwd': self.directory.name, 'token': 'secret'}))

    def test_approvals_read_model_uses_durable_store(self):
        self._seed()
        approvals = self.model.approvals()
        self.assertEqual(approvals[0]['approval_id'], 'approval-1')
        self.assertEqual(approvals[0]['thread_id'], 'thread-1')
        self.assertEqual(approvals[0]['feedback_state'], 'PENDING')

    def test_decision_and_outcome_remain_separate(self):
        self._seed()
        self.store.resolve_approval('approval-1', 'accept', 'approved')
        self.store.transition_feedback('approval-1', 'EXECUTION_FAILED')
        approval = self.model.approvals()[0]
        self.assertEqual(approval['decision'], 'accept')
        self.assertEqual(approval['feedback_state'], 'EXECUTION_FAILED')

    def test_approvals_projection_is_sanitized_and_read_only(self):
        self._seed()
        with patch.object(FeishuStore, 'resolve_approval', side_effect=AssertionError('must not resolve')):
            approval = self.model.approvals()[0]
        rendered = json.dumps(approval)
        for forbidden in ('requester-sensitive', 'request-sensitive', 'echo sensitive', 'secret', self.directory.name, 'summary_json', 'codex_request_id'):
            self.assertNotIn(forbidden, rendered)


if __name__ == '__main__':
    unittest.main()
