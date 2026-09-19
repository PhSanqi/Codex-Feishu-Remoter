"""Launch the local-only M3A Control Center."""

import argparse
import json
from pathlib import Path
import socket
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import webbrowser


ROOT = Path(__file__).resolve().parents[1]
UI_INDEX = ROOT / 'm3_control' / 'dist' / 'index.html'
PROBE_TIMEOUT_SECONDS = 0.5
sys.path.insert(0, str(ROOT / 'src'))

from cfr.control import CfrSupervisor, LocalControlServer


def _root_url(port):
    return f'http://127.0.0.1:{port}/'


def _json_payload(response):
    try:
        return json.loads(response.read().decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _is_cfr_payload(payload):
    return isinstance(payload, dict) and (
        payload.get('error_code') == 'CONTROL_AUTH_REQUIRED'
        or (payload.get('status') == 'ok' and 'current_state' in payload)
    )


def _existing_cfr(port):
    if port == 0:
        return False
    request = Request(f'{_root_url(port)}api/v1/status')
    try:
        with urlopen(request, timeout=PROBE_TIMEOUT_SECONDS) as response:
            return _is_cfr_payload(_json_payload(response))
    except HTTPError as error:
        try:
            return error.code in {401, 403} and _is_cfr_payload(_json_payload(error))
        finally:
            error.close()
    except (OSError, URLError, TimeoutError):
        return False


def _port_is_used(port):
    if port == 0:
        return False
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=PROBE_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def _emit(payload, quiet):
    if not quiet:
        print(json.dumps(payload))


def _browser_opened(url, requested):
    if not requested:
        return None
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--db', default='cfr.sqlite3')
    parser.add_argument('--bootstrap-nonce', default=None)
    parser.add_argument('--quiet', action='store_true')
    parser.add_argument('--open-browser', action='store_true')
    args = parser.parse_args(argv)
    root_url = _root_url(args.port)
    if not UI_INDEX.is_file():
        _emit({
            'status': 'error',
            'error_code': 'CONTROL_UI_BUILD_MISSING',
            'message': 'Control UI build is missing. Run: cd m3_control && npm run build',
        }, args.quiet)
        return 2
    if _existing_cfr(args.port):
        browser_opened = _browser_opened(root_url, args.open_browser)
        payload = {'status': 'already_running', 'url': root_url}
        if browser_opened is not None:
            payload['browser_opened'] = browser_opened
        _emit(payload, args.quiet)
        return 0
    if _port_is_used(args.port):
        _emit({
            'status': 'error',
            'error_code': 'CONTROL_PORT_IN_USE',
            'message': 'Requested local port is already in use by another process',
            'url': root_url,
        }, args.quiet)
        return 1
    supervisor = CfrSupervisor(project_root=ROOT, database=ROOT / args.db)
    try:
        server = LocalControlServer(
            supervisor,
            host='127.0.0.1',
            port=args.port,
            bootstrap_nonce=args.bootstrap_nonce,
        ).start()
    except OSError:
        if _existing_cfr(args.port):
            browser_opened = _browser_opened(root_url, args.open_browser)
            payload = {'status': 'already_running', 'url': root_url}
            if browser_opened is not None:
                payload['browser_opened'] = browser_opened
            _emit(payload, args.quiet)
            return 0
        _emit({
            'status': 'error',
            'error_code': 'CONTROL_PORT_IN_USE',
            'message': 'Requested local port is already in use by another process',
            'url': root_url,
        }, args.quiet)
        return 1
    # Make the local control plane visible before optional Codex/Chat/Feishu
    # readiness probes. Those probes can take seconds on a cold machine.
    browser_opened = _browser_opened(server.bootstrap_url, args.open_browser)
    setup = supervisor.setup_state()
    browser_runtime = {'status': 'not_started'}
    feishu_runtime = None
    if setup.get('ready'):
        chat_ready = True
        if setup.get('selected_surface') == 'chat':
            browser_runtime = supervisor.start_browser_bridge()
            chat_ready = bool(browser_runtime.get('available'))
        if chat_ready:
            feishu_runtime = supervisor.start_feishu()
    payload = {
        'status': 'started',
        'url': server.bootstrap_url,
        'chat_browser': browser_runtime.get('status') if isinstance(browser_runtime, dict) else 'unknown',
        'setup_ready': bool(setup.get('ready')),
        'feishu': 'running' if getattr(feishu_runtime, 'status', None) == 'ok' else 'not_started' if feishu_runtime is None else 'error',
    }
    feishu_error_code = getattr(feishu_runtime, 'error_code', None)
    if isinstance(feishu_error_code, str) and feishu_error_code:
        payload['feishu_error_code'] = feishu_error_code
    if browser_opened is not None:
        payload['browser_opened'] = browser_opened
    _emit(payload, args.quiet)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
