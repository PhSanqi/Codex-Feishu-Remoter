import sys
import tempfile
import threading
import time
import unittest
import queue
from unittest.mock import ANY, MagicMock, patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from cfr.codex.app_server import AppServerClient, AppServerRpcError, DIAGNOSTIC_QUEUE_LIMIT, ServerRequestResolution


class FakeLauncher:
    def build_app_server_command(self):
        return [sys.executable, '-u', str(Path(__file__).resolve().parents[1] / 'fixtures' / 'fake_app_server.py')]


class AppServerTests(unittest.TestCase):
    def test_fail_pending_never_blocks_when_a_response_already_filled_the_waiter(self):
        client = AppServerClient(FakeLauncher(), timeout=2.0, process_env={})
        waiter = queue.Queue(1)
        response = {'result': {'ok': True}}
        waiter.put(response)
        client._pending[1] = waiter
        client._fail_pending(RuntimeError('closed'))
        self.assertEqual(waiter.get_nowait(), response)

    def test_diagnostic_mirror_drops_oldest_instead_of_growing_without_bound(self):
        client = AppServerClient(FakeLauncher(), timeout=2.0, process_env={})
        for index in range(DIAGNOSTIC_QUEUE_LIMIT + 10):
            client._put_diagnostic(client.notifications, {'index': index})
        self.assertEqual(client.notifications.qsize(), DIAGNOSTIC_QUEUE_LIMIT)
        values = [client.notifications.get_nowait()['index'] for _ in range(DIAGNOSTIC_QUEUE_LIMIT)]
        self.assertEqual(values[0], 10)
        self.assertEqual(values[-1], DIAGNOSTIC_QUEUE_LIMIT + 9)

    def test_server_request_handler_result_is_written(self):
        with AppServerClient(FakeLauncher(), timeout=2.0, on_server_request=lambda message: {'decision': 'accept_once'}) as client:
            subscription = client.subscribe(lambda message: message.get('method') == 'server_response_observed')
            client.request('server_request', {})
            observed = subscription.get(timeout=1).get('params', {})
            subscription.close()
            self.assertEqual(observed['result'], {'decision': 'accept_once'})

    def test_server_request_handler_error_is_written(self):
        with AppServerClient(FakeLauncher(), timeout=2.0, on_server_request=lambda message: ServerRequestResolution(error={'code': -32001, 'message': 'DECLINED'})) as client:
            subscription = client.subscribe(lambda message: message.get('method') == 'server_response_observed')
            client.request('server_request', {})
            observed = subscription.get(timeout=1).get('params', {})
            subscription.close()
            self.assertEqual(observed['error'], {'code': -32001, 'message': 'DECLINED'})

    def test_unknown_server_request_safe_reject(self):
        with AppServerClient(FakeLauncher(), timeout=2.0) as client:
            subscription = client.subscribe(lambda message: message.get('method') == 'server_response_observed')
            client.request('server_request', {})
            observed = subscription.get(timeout=1).get('params', {})
            subscription.close()
            self.assertEqual(observed['error']['message'], 'UNSUPPORTED_CODEX_SERVER_REQUEST')

    def test_server_request_handler_runs_off_reader_thread(self):
        names = []
        with AppServerClient(FakeLauncher(), timeout=2.0, on_server_request=lambda message: names.append(threading.current_thread().name) or {'ok': True}) as client:
            subscription = client.subscribe(lambda message: message.get('method') == 'server_response_observed')
            client.request('server_request', {})
            subscription.get(timeout=1)
            subscription.close()
        self.assertTrue(names)
        self.assertNotEqual(names[0], 'cfr-app-server-stdout')

    def test_file_change_patch_projection_enriches_current_approval_request_by_item_id(self):
        client = AppServerClient(FakeLauncher(), timeout=2.0, process_env={})
        client._record_file_change_projection({
            'method': 'item/fileChange/patchUpdated',
            'params': {
                'threadId': 'thread-1', 'turnId': 'turn-1', 'itemId': 'item-1',
                'changes': [
                    {'path': 'C:/workspace/test/report.xlsx', 'kind': 'add', 'diff': '...'},
                    {'path': 'C:/workspace/test/preview.png', 'kind': 'add', 'diff': '...'},
                ],
            },
        })
        request = client._enrich_server_request({
            'id': 99,
            'method': 'item/fileChange/requestApproval',
            'params': {'threadId': 'thread-1', 'turnId': 'turn-1', 'itemId': 'item-1'},
        }, wait_seconds=0)
        self.assertEqual(request['params']['_cfrChangedPaths'], [
            'C:/workspace/test/report.xlsx', 'C:/workspace/test/preview.png',
        ])

    def test_file_change_approval_wait_can_observe_patch_notification_from_reader(self):
        client = AppServerClient(FakeLauncher(), timeout=2.0, process_env={})
        result = []
        request = {
            'id': 99,
            'method': 'item/fileChange/requestApproval',
            'params': {'threadId': 'thread-1', 'turnId': 'turn-1', 'itemId': 'item-1'},
        }
        thread = threading.Thread(target=lambda: result.append(client._enrich_server_request(request, wait_seconds=0.2)))
        thread.start()
        time.sleep(0.03)
        client._record_file_change_projection({
            'method': 'item/fileChange/patchUpdated',
            'params': {
                'threadId': 'thread-1', 'turnId': 'turn-1', 'itemId': 'item-1',
                'changes': [{'path': 'C:/workspace/test/result.xlsx', 'kind': 'add', 'diff': '...'}],
            },
        })
        thread.join(1)
        self.assertEqual(result[0]['params']['_cfrChangedPaths'], ['C:/workspace/test/result.xlsx'])

    def test_server_request_exactly_once(self):
        with AppServerClient(FakeLauncher(), timeout=2.0, on_server_request=lambda message: {'ok': True}) as client:
            subscription = client.subscribe(lambda message: message.get('method') == 'server_response_observed')
            client.request('server_request', {})
            observed = subscription.get(timeout=1).get('params', {})
            subscription.close()
            self.assertEqual(observed['id'], 99)
            self.assertFalse(client.respond_server_request(99, result={'ok': False}))

    def test_server_request_resolution_snapshot_is_read_only_evidence(self):
        with AppServerClient(FakeLauncher(), timeout=2.0, on_server_request=lambda message: {'ok': True}) as client:
            client.request('server_request', {})
            snapshot = client.server_request_resolution_snapshot()
            for _ in range(50):
                if 99 in snapshot['resolved']:
                    break
                time.sleep(0.01)
                snapshot = client.server_request_resolution_snapshot()
            self.assertIn(99, snapshot['resolved'])
            self.assertEqual(snapshot['inflight'], ())

    def test_late_server_request_after_close_does_not_write(self):
        entered = threading.Event()
        release = threading.Event()

        def handler(message):
            entered.set()
            release.wait(1)
            return {'late': True}

        client = AppServerClient(FakeLauncher(), timeout=2.0, on_server_request=handler)
        client.start()
        client.request('server_request', {})
        self.assertTrue(entered.wait(1))
        client.close()
        release.set()
        time.sleep(0.1)
        self.assertFalse(client.respond_server_request(99, result={'late': False}))

    def test_server_request_write_failure_does_not_commit_resolved(self):
        client = AppServerClient(FakeLauncher(), timeout=2.0)
        client.start()
        with patch.object(client, '_write', side_effect=RuntimeError('write failed')):
            self.assertFalse(client.respond_server_request(99, result={'ok': True}))
        with patch.object(client, '_write') as writer:
            self.assertTrue(client.respond_server_request(99, result={'ok': True}))
            writer.assert_called_once()
        client.close()

    def test_rpc_error_notification_server_request_malformed_and_timeout(self):
        with AppServerClient(FakeLauncher(), timeout=2.0) as client:
            self.assertEqual(client.request('echo', {'x': 1}), {'x': 1})
            client.request('notify', {})
            self.assertEqual(client.notifications.get(timeout=0.2)['method'], 'unrelated')
            self.assertEqual(client.request('server_request', {})['request_was_sent'], True)
            with self.assertRaises(AppServerRpcError) as caught:
                client.request('error', {})
            self.assertEqual(caught.exception.code, -32001)
            with self.assertRaises(TimeoutError):
                client.request('hang', {}, timeout=0.2)
        with AppServerClient(FakeLauncher(), timeout=2.0) as client:
            client.request('malformed', {})
            self.assertEqual(client.notifications.get(timeout=0.2)['method'], 'cfr/malformed')

    def test_startup_timeout_is_independent_from_request_timeout(self):
        with AppServerClient(FakeLauncher(), timeout=2.0) as client:
            self.assertEqual(client.timeout, 2.0)
            with self.assertRaises(TimeoutError):
                client.request('hang', {}, timeout=0.2)

    def test_stdout_eof_fails_pending_request(self):
        client = AppServerClient(FakeLauncher(), timeout=2.0)
        client.start()
        with self.assertRaises(RuntimeError) as caught:
            client.request('exit', {})
        self.assertIn('stdout EOF', str(caught.exception))
        client.close()

    def test_process_env_is_child_only(self):
        with tempfile.TemporaryDirectory() as directory:
            with AppServerClient(FakeLauncher(), timeout=2.0, process_env={'CFR_TEST_PROXY': 'http://127.0.0.1:57777'}, codex_home=directory) as client:
                environment = client.request('env', {})
                self.assertEqual(environment['CFR_TEST_PROXY'], 'http://127.0.0.1:57777')
                self.assertEqual(environment['CODEX_HOME'], directory)

    def test_default_process_env_projects_detected_system_proxy(self):
        with patch('cfr.codex.app_server.resolve_cfr_proxy', return_value=object()), patch(
            'cfr.codex.app_server.proxy_child_env',
            return_value={
                'HTTP_PROXY': 'http://127.0.0.1:57777',
                'HTTPS_PROXY': 'http://127.0.0.1:57777',
                'NO_PROXY': 'localhost,127.0.0.1,::1',
            },
        ):
            client = AppServerClient(FakeLauncher(), timeout=2.0)
        self.assertEqual(client.process_env['HTTPS_PROXY'], 'http://127.0.0.1:57777')

    def test_explicit_process_env_does_not_autodiscover_proxy(self):
        with patch('cfr.codex.app_server.resolve_cfr_proxy', side_effect=AssertionError('should not resolve proxy')):
            client = AppServerClient(FakeLauncher(), timeout=2.0, process_env={'CFR_EXPLICIT_ENV': '1'})
        self.assertEqual(client.process_env['CFR_EXPLICIT_ENV'], '1')

    def test_close_is_idempotent_and_records_exit(self):
        client = AppServerClient(FakeLauncher(), timeout=2.0)
        client.start()
        self.assertTrue(client.is_running)
        self.assertIsNotNone(client.process_id)
        client.close()
        first_snapshot = client.lifecycle_snapshot()
        client.close()
        self.assertFalse(client.is_running)
        self.assertIsNotNone(client.exit_code)
        self.assertTrue(first_snapshot['exited'])

    def test_windows_close_terminates_owned_cmd_process_tree(self):
        client = AppServerClient(FakeLauncher(), timeout=2.0)
        client.proc = MagicMock(pid=123)
        client.proc.poll.return_value = None
        client.proc.stdin.closed = False
        client.proc.stdout = None
        client.proc.stderr = None
        client.proc.wait.side_effect = [__import__('subprocess').TimeoutExpired('codex', 0.5), 0]
        with patch('cfr.codex.app_server.os.name', 'nt'), patch('cfr.codex.app_server.subprocess.run') as taskkill:
            client.close()
        taskkill.assert_called_once_with(
            ['taskkill', '/PID', '123', '/T', '/F'],
            stdout=__import__('subprocess').DEVNULL, stderr=__import__('subprocess').DEVNULL, timeout=5, check=False,
            creationflags=getattr(__import__('subprocess'), 'CREATE_NO_WINDOW', 0), startupinfo=ANY,
        )
        client.proc.send_signal.assert_called_once()
        self.assertEqual(client.close_mode, 'kill_tree')

    def test_windows_close_does_not_flush_stdin_before_process_exit(self):
        client = AppServerClient(FakeLauncher(), timeout=2.0)
        client.proc = MagicMock(pid=123)
        client.proc.poll.return_value = None
        client.proc.stdin.closed = False
        client.proc.stdout = None
        client.proc.stderr = None
        client.proc.wait.return_value = 0
        events = []
        client.proc.send_signal.side_effect = lambda *_args: events.append('signal')
        client.proc.stdin.close.side_effect = lambda: events.append('stdin_close')
        with patch('cfr.codex.app_server.os.name', 'nt'):
            client.close()
        self.assertEqual(events[:2], ['signal', 'stdin_close'])


if __name__ == '__main__':
    unittest.main()
