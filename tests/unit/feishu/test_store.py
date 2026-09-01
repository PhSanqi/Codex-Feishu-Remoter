from pathlib import Path
import tempfile
import time
import unittest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'src'))

from cfr.core.models import StructuredError
from cfr.feishu.config import FeishuSettings
from cfr.feishu.models import FeishuInboundMessage
from cfr.feishu.store import FeishuStore


class StoreTests(unittest.TestCase):
    def message(self, message_id='m'):
        return FeishuInboundMessage(None, message_id, 'chat', 'p2p', 'ou', 'user', 'text', 'hello')

    def test_inbox_claim_and_restart_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FeishuStore(Path(directory) / 'db.sqlite3')
            self.assertTrue(store.enqueue_message(self.message()))
            self.assertFalse(store.enqueue_message(self.message()))
            self.assertEqual(store.claim_next().status, 'running')
            store.recover_on_startup()
            self.assertEqual(store.get_inbox('m').status, 'interrupted_on_restart')

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


if __name__ == '__main__':
    unittest.main()
