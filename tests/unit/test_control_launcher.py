import contextlib
import importlib.util
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


def _launcher_module():
    spec = importlib.util.spec_from_file_location('cfr_control_launcher_test', ROOT / 'scripts' / 'run_cfr_control.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _CfrStatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"status":"error","error_code":"CONTROL_AUTH_REQUIRED"}'
        self.send_response(403)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


class _ForeignHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_args):
        return


@contextlib.contextmanager
def _listener(handler):
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class _Server:
    url = 'http://127.0.0.1:8765'
    bootstrap_url = 'http://127.0.0.1:8765/?bootstrap=fresh'

    def __init__(self, *_args, **_kwargs):
        self.stopped = False

    def start(self):
        return self

    def stop(self):
        self.stopped = True


class ControlLauncherTests(unittest.TestCase):
    def setUp(self):
        self.launcher = _launcher_module()
        self.ui_directory = tempfile.TemporaryDirectory()
        self.ui_index = Path(self.ui_directory.name) / 'dist' / 'index.html'
        self.ui_index.parent.mkdir()
        self.ui_index.write_text('<div id="root"></div>', encoding='utf-8')
        self.ui_patch = patch.object(self.launcher, 'UI_INDEX', self.ui_index)
        self.ui_patch.start()

    def tearDown(self):
        self.ui_patch.stop()
        self.ui_directory.cleanup()

    def _run(self, argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = self.launcher.main(argv)
        return result, json.loads(output.getvalue())

    def test_existing_cfr_is_detected_without_second_server(self):
        with _listener(_CfrStatusHandler) as port, patch.object(self.launcher, 'CfrSupervisor') as supervisor:
            result, payload = self._run(['--port', str(port)])
        self.assertEqual(result, 0)
        self.assertEqual(payload['status'], 'already_running')
        supervisor.assert_not_called()

    def test_foreign_port_fails_closed(self):
        with _listener(_ForeignHandler) as port, patch.object(self.launcher, 'CfrSupervisor') as supervisor:
            result, payload = self._run(['--port', str(port)])
        self.assertEqual(result, 1)
        self.assertEqual(payload['error_code'], 'CONTROL_PORT_IN_USE')
        supervisor.assert_not_called()

    def test_fresh_browser_uses_bootstrap_url(self):
        server = _Server()
        with patch.object(self.launcher, 'CfrSupervisor') as supervisor_type, patch.object(self.launcher, 'LocalControlServer', return_value=server), patch.object(self.launcher.webbrowser, 'open', return_value=True) as browser, patch.object(self.launcher.time, 'sleep', side_effect=KeyboardInterrupt):
            supervisor_type.return_value.start_browser_bridge.return_value = {'status': 'ready'}
            supervisor_type.return_value.start_feishu.return_value.status = 'ok'
            supervisor_type.return_value.start_feishu.return_value.error_code = None
            result, payload = self._run(['--port', '0', '--open-browser'])
        self.assertEqual(result, 0)
        self.assertEqual(payload['url'], server.bootstrap_url)
        self.assertEqual(payload['feishu'], 'running')
        self.assertTrue(payload['browser_opened'])
        browser.assert_called_once_with(server.bootstrap_url)
        supervisor_type.return_value.start_feishu.assert_called_once_with()
        self.assertTrue(server.stopped)

    def test_browser_failure_keeps_fresh_server_running(self):
        server = _Server()
        with patch.object(self.launcher, 'CfrSupervisor'), patch.object(self.launcher, 'LocalControlServer', return_value=server), patch.object(self.launcher.webbrowser, 'open', return_value=False), patch.object(self.launcher.time, 'sleep', side_effect=KeyboardInterrupt):
            result, payload = self._run(['--port', '0', '--open-browser'])
        self.assertEqual(result, 0)
        self.assertFalse(payload['browser_opened'])
        self.assertEqual(payload['url'], server.bootstrap_url)
        self.assertTrue(server.stopped)

    def test_missing_ui_fails_before_supervisor(self):
        missing = Path(self.ui_directory.name) / 'missing-control-ui.html'
        with patch.object(self.launcher, 'UI_INDEX', missing), patch.object(self.launcher, 'CfrSupervisor') as supervisor:
            result, payload = self._run([])
        self.assertEqual(result, 2)
        self.assertEqual(payload['error_code'], 'CONTROL_UI_BUILD_MISSING')
        supervisor.assert_not_called()

    def test_start_cmd_uses_root_and_has_no_service_or_install_commands(self):
        content = (ROOT / 'START_CFR.cmd').read_text(encoding='utf-8').lower()
        self.assertIn('cd /d "%~dp0"', content)
        self.assertIn('run_cfr_control.py --open-browser', content)
        self.assertIn('dedicated chatgpt browser, and feishu runtime', content)
        for forbidden in ('pip install', 'npm install', 'schtasks', 'taskkill'):
            self.assertNotIn(forbidden, content)
