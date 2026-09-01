from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'src'))

from cfr.codex.app_server import AppServerClient
from cfr.feishu.approvals import ApprovalBridge
from cfr.feishu.config import FeishuSettings
from cfr.feishu.models import FeishuExecutionContext
from cfr.feishu.replies import FeishuReplyClient
from cfr.feishu.store import FeishuStore
from cfr.feishu.transport import FakeFeishuTransport
from cfr.codex.approvals import ApprovalRequest


class FakeLauncher:
    def build_app_server_command(self):
        return [sys.executable, '-u', str(Path(__file__).resolve().parents[2] / 'fixtures' / 'fake_app_server.py')]


class ApprovalRoundtripTests(unittest.TestCase):
    def test_protocol_server_request_roundtrip_returns_schema_valid_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FeishuStore(Path(directory) / 'db.sqlite3')
            transport = FakeFeishuTransport()
            settings = FeishuSettings('app', 'secret', ('ou-operator',), (Path(directory),), database=Path(directory) / 'db.sqlite3', approval_timeout_seconds=5)
            bridge = ApprovalBridge(store, FeishuReplyClient(transport, store), settings)
            context = FeishuExecutionContext('message-1', 'chat-1', 'ou-operator', 'thread-x')
            with AppServerClient(FakeLauncher(), timeout=2, on_server_request=lambda message: bridge.handle_server_request(message, context)) as client:
                observed_subscription = client.subscribe(lambda message: message.get('method') == 'server_response_observed')
                client.request('approval_roundtrip', {})
                approval_id = None
                for _ in range(50):
                    rows = store.find_approval_prefix('')
                    if rows:
                        approval_id = rows[0]['approval_id']
                        break
                    time.sleep(0.02)
                self.assertIsNotNone(approval_id)
                bridge.resolve(approval_id, 'ou-operator', 'approve_once')
                response = observed_subscription.get(timeout=2).get('params', {})
                observed_subscription.close()
                self.assertEqual(response['id'], 99)
                self.assertEqual(response['result'], {'decision': 'accept'})

    def test_multiple_approvals_have_distinct_cards_and_reply_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FeishuStore(Path(directory) / 'db.sqlite3')
            transport = FakeFeishuTransport()
            settings = FeishuSettings('app', 'secret', ('ou-operator',), (Path(directory),), database=Path(directory) / 'db.sqlite3', approval_timeout_seconds=5)
            bridge = ApprovalBridge(store, FeishuReplyClient(transport, store), settings)
            results = []

            def run(request_id):
                results.append(bridge.handle_server_request({'id': request_id, 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': 'thread-x', 'item': {'command': 'echo safe'}}}, FeishuExecutionContext('message-1', 'chat-1', 'ou-operator', 'thread-x')))

            import threading
            first = threading.Thread(target=run, args=('r1',))
            second = threading.Thread(target=run, args=('r2',))
            first.start()
            second.start()
            approval_ids = []
            for _ in range(100):
                rows = store.find_approval_prefix('')
                if len(rows) == 2:
                    approval_ids = [row['approval_id'] for row in rows]
                    break
                time.sleep(0.01)
            self.assertEqual(len(approval_ids), 2)
            for approval_id in approval_ids:
                bridge.resolve(approval_id, 'ou-operator', 'approve_once')
            first.join(2)
            second.join(2)
            self.assertEqual(len(results), 2)
            self.assertEqual(len({item.uuid for item in transport.messages}), 2)

    def test_card_send_failure_cancels_approval_and_declines(self):
        class FailingCardTransport(FakeFeishuTransport):
            def send_card(self, open_id, card, uuid):
                raise RuntimeError('card unavailable')

        with tempfile.TemporaryDirectory() as directory:
            store = FeishuStore(Path(directory) / 'db.sqlite3')
            settings = FeishuSettings('app', 'secret', ('ou-operator',), (Path(directory),), database=Path(directory) / 'db.sqlite3')
            bridge = ApprovalBridge(store, FeishuReplyClient(FailingCardTransport(), store), settings)
            result = bridge.handle_server_request({'id': 'r1', 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': 'thread-x', 'item': {'command': 'echo safe'}}}, FeishuExecutionContext('message-1', 'chat-1', 'ou-operator', 'thread-x'))
            rows = store.find_approval_prefix('')
            self.assertEqual(result, {'decision': 'decline'})
            self.assertEqual(rows, [])
            with store._connection() as connection:
                row = connection.execute('select state, decision from feishu_approvals limit 1').fetchone()
            self.assertEqual((row['state'], row['decision']), ('cancelled', 'decline'))

    def test_card_send_failure_records_fail_closed_evidence(self):
        class FailingCardTransport(FakeFeishuTransport):
            def send_card(self, open_id, card, uuid):
                raise RuntimeError('card unavailable')

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db.sqlite3'
            store = FeishuStore(path)
            settings = FeishuSettings('app', 'secret', ('ou-operator',), (Path(directory),), database=path)
            bridge = ApprovalBridge(store, FeishuReplyClient(FailingCardTransport(), store), settings)
            result = bridge.handle_server_request(
                {'id': 'r-failure-evidence', 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': 'thread-x', 'item': {'command': 'echo safe'}}},
                FeishuExecutionContext('message-failure', 'chat-1', 'ou-operator', 'thread-x'),
            )
            row = store.get_approval(next(iter({item['approval_id'] for item in store.find_approval_prefix('')}), ''))
            evidence = next(iter(bridge._card_send_evidence.values()))
            self.assertEqual(result, {'decision': 'decline'})
            self.assertFalse(evidence['FeishuSendSuccess'])
            self.assertEqual(evidence['FailureDisposition'], 'decline')
            self.assertIsNone(row)

    def test_duplicate_card_click_is_exactly_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db.sqlite3'
            store = FeishuStore(path)
            settings = FeishuSettings('app', 'secret', ('ou-operator',), (Path(directory),), database=path)
            bridge = ApprovalBridge(store, FeishuReplyClient(FakeFeishuTransport(), store), settings)
            request = ApprovalRequest('req-duplicate', 'thread', None, None, 'command', 'safe', 'echo safe', directory, (), ('accept', 'decline'))
            store.create_approval('approval-duplicate', request, 'ou-operator', time.time() + 60)
            first = bridge.resolve('approval-duplicate', 'ou-operator', 'allow_once')
            second = bridge.resolve('approval-duplicate', 'ou-operator', 'allow_once')
            self.assertEqual(first['status'], 'RESOLVED')
            self.assertIn(second['status'], {'ALREADY_RESOLVED', 'ALREADY_RESOLVED'})

    def test_timeout_declines_and_cancel_unblocks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db.sqlite3'
            request = {'id': 'r-timeout', 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': 'thread-x', 'item': {'command': 'echo safe'}}}
            timeout_store = FeishuStore(path)
            timeout_settings = FeishuSettings('app-timeout', 'secret', ('ou-operator',), (Path(directory),), database=path, approval_timeout_seconds=0.05)
            timeout_bridge = ApprovalBridge(timeout_store, FeishuReplyClient(FakeFeishuTransport(), timeout_store), timeout_settings)
            self.assertEqual(timeout_bridge.handle_server_request(request, FeishuExecutionContext('m-timeout', 'oc', 'ou-operator', 'thread-x')), {'decision': 'decline'})
            with timeout_store._connection() as connection:
                timeout_state = connection.execute('select state from feishu_approvals limit 1').fetchone()['state']
            self.assertEqual(timeout_state, 'expired')

            cancel_store = FeishuStore(Path(directory) / 'cancel.sqlite3')
            cancel_settings = FeishuSettings('app-cancel', 'secret', ('ou-operator',), (Path(directory),), database=Path(directory) / 'cancel.sqlite3', approval_timeout_seconds=5)
            cancel_bridge = ApprovalBridge(cancel_store, FeishuReplyClient(FakeFeishuTransport(), cancel_store), cancel_settings)
            import threading
            result = []
            worker = threading.Thread(target=lambda: result.append(cancel_bridge.handle_server_request({'id': 'r-cancel', **request}, FeishuExecutionContext('m-cancel', 'oc', 'ou-operator', 'thread-x'))))
            worker.start()
            approval_id = None
            for _ in range(100):
                rows = cancel_store.find_approval_prefix('')
                if rows:
                    approval_id = rows[0]['approval_id']
                    break
                time.sleep(0.01)
            self.assertIsNotNone(approval_id)
            cancel_bridge.resolve(approval_id, 'ou-operator', 'cancel')
            worker.join(2)
            self.assertEqual(result, [{'decision': 'cancel'}])

    def test_wrong_operator_cannot_resolve(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db.sqlite3'
            store = FeishuStore(path)
            settings = FeishuSettings('app', 'secret', ('ou-operator',), (Path(directory),), database=path)
            bridge = ApprovalBridge(store, FeishuReplyClient(FakeFeishuTransport(), store), settings)
            request = ApprovalRequest('req', 'thread', None, None, 'command', 'safe', 'echo safe', directory, (), ('accept', 'decline'))
            store.create_approval('approval-operator', request, 'ou-operator', time.time() + 60)
            with self.assertRaises(Exception) as caught:
                bridge.resolve('approval-operator', 'ou-other', 'approve_once')
            self.assertEqual(caught.exception.code, 'FEISHU_APPROVAL_OPERATOR_MISMATCH')

    def test_server_request_resolved_cleans_pending_waiter(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db.sqlite3'
            store = FeishuStore(path)
            settings = FeishuSettings('app-resolved', 'secret', ('ou-operator',), (Path(directory),), database=path, approval_timeout_seconds=5)
            bridge = ApprovalBridge(store, FeishuReplyClient(FakeFeishuTransport(), store), settings)
            result = []
            import threading
            worker = threading.Thread(target=lambda: result.append(bridge.handle_server_request({'id': 'request-resolved', 'method': 'item/fileChange/requestApproval', 'params': {'threadId': 'thread-resolved', 'item': {'changedPaths': ['allow.txt']}}}, FeishuExecutionContext('m-resolved', 'oc', 'ou-operator', 'thread-resolved'))))
            worker.start()
            approval_id = None
            for _ in range(100):
                rows = store.find_approval_prefix('')
                if rows:
                    approval_id = rows[0]['approval_id']
                    break
                time.sleep(0.01)
            self.assertIsNotNone(approval_id)
            self.assertEqual(bridge.handle_server_notification({'method': 'serverRequest/resolved', 'params': {'requestId': 'request-resolved'}}), 1)
            worker.join(2)
            self.assertEqual(result, [{'decision': 'cancel'}])
            with store._connection() as connection:
                row = connection.execute('select state, decision from feishu_approvals limit 1').fetchone()
            self.assertEqual((row['state'], row['decision']), ('resolved', 'cancel'))

    def test_zero_request_id_notification_matches_and_wrong_id_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db.sqlite3'
            store = FeishuStore(path)
            settings = FeishuSettings('app-zero', 'secret', ('ou-operator',), (Path(directory),), database=path, approval_timeout_seconds=5)
            bridge = ApprovalBridge(store, FeishuReplyClient(FakeFeishuTransport(), store), settings)
            result = []
            import threading
            worker = threading.Thread(target=lambda: result.append(bridge.handle_server_request({'id': 0, 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': 'thread-zero', 'item': {'command': 'echo safe'}}}, FeishuExecutionContext('m-zero', 'oc', 'ou-operator', 'thread-zero'))))
            worker.start()
            for _ in range(100):
                if store.find_approval_prefix(''):
                    break
                time.sleep(0.01)
            self.assertEqual(bridge.handle_server_notification({'method': 'serverRequest/resolved', 'params': {'requestId': 1}}), 0)
            self.assertEqual(bridge.handle_server_notification({'method': 'serverRequest/resolved', 'params': {'requestId': 0}}), 1)
            worker.join(2)
            self.assertEqual(result, [{'decision': 'cancel'}])


if __name__ == '__main__':
    unittest.main()
