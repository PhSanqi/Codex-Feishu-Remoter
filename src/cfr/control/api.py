from __future__ import annotations

from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import parse_qs, unquote, urlparse

from .codex_settings import CodexSettingsError
from .read_model import ControlReadModel
from .supervisor import ControlCommandResult
from cfr.feishu.store import FeishuStore
from cfr.platform import open_codex_desktop_thread
from cfr.storage.db import BindingStore


class LocalControlServer:
    """Authenticated loopback-only M3A Control API and static UI host."""

    def __init__(self, supervisor, *, host='127.0.0.1', port=8787, web_root: Path | str | None = None, bootstrap_nonce: str | None = None):
        if host != '127.0.0.1':
            raise ValueError('M3 Control API only binds to 127.0.0.1')
        self.supervisor = supervisor
        self.host = host
        self.port = port
        self.web_root = Path(web_root or Path(__file__).resolve().parents[3] / 'm3_control' / 'dist')
        self.read_model = ControlReadModel(supervisor, supervisor.database)
        self._bootstrap_nonce = bootstrap_nonce or secrets.token_urlsafe(32)
        self._session_token: str | None = None
        self._csrf_token: str | None = None
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        port = self._httpd.server_port if self._httpd is not None else self.port
        return f'http://127.0.0.1:{port}'

    @property
    def bootstrap_url(self) -> str:
        return f'{self.url}/?bootstrap={self._bootstrap_nonce}'

    def start(self):
        if self._httpd is not None:
            return self
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def _json(self, payload, status=HTTPStatus.OK):
                encoded = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def _cookies(self):
                cookie = SimpleCookie()
                cookie.load(self.headers.get('Cookie', ''))
                return cookie

            def _authorized(self):
                token = self._cookies().get('cfr_control_session')
                return bool(token and owner._session_token and secrets.compare_digest(token.value, owner._session_token))

            def _csrf_valid(self):
                cookie = self._cookies().get('cfr_control_csrf')
                header = self.headers.get('X-CFR-CSRF')
                return bool(cookie and header and owner._csrf_token and secrets.compare_digest(cookie.value, owner._csrf_token) and secrets.compare_digest(header, owner._csrf_token))

            def _command(self, operation):
                if not self._authorized() or not self._csrf_valid():
                    return self._json({'status': 'error', 'error_code': 'CONTROL_AUTH_REQUIRED', 'message': 'A local Control Center session is required', 'current_state': owner.supervisor.current_state()}, HTTPStatus.FORBIDDEN)
                self._json(operation().as_dict())

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == '/':
                    nonce = parse_qs(parsed.query).get('bootstrap', [None])[0]
                    if nonce and secrets.compare_digest(nonce, owner._bootstrap_nonce):
                        owner._bootstrap_nonce = secrets.token_urlsafe(32)
                        owner._session_token = secrets.token_urlsafe(32)
                        owner._csrf_token = secrets.token_urlsafe(32)
                        self.send_response(HTTPStatus.FOUND)
                        self.send_header('Set-Cookie', f'cfr_control_session={owner._session_token}; HttpOnly; SameSite=Strict; Path=/')
                        self.send_header('Set-Cookie', f'cfr_control_csrf={owner._csrf_token}; SameSite=Strict; Path=/')
                        self.send_header('Location', '/')
                        self.end_headers()
                        return
                    if not self._authorized():
                        return self._json({'status': 'error', 'error_code': 'CONTROL_AUTH_REQUIRED', 'message': 'Open the bootstrap URL printed by the launcher'}, HTTPStatus.FORBIDDEN)
                    return self._static('index.html', 'text/html; charset=utf-8')
                if parsed.path == '/api/v1/status':
                    return self._read(owner.read_model.build)
                if parsed.path == '/api/v1/runtime':
                    return self._read(owner.supervisor.current_state)
                if parsed.path == '/api/v1/sessions':
                    return self._read(owner.read_model.sessions)
                if parsed.path == '/api/v1/bindings':
                    return self._read(owner.read_model.bindings)
                if parsed.path == '/api/v1/jobs':
                    return self._read(owner.read_model.jobs)
                if parsed.path == '/api/v1/approvals':
                    return self._read(owner.read_model.approvals)
                if parsed.path == '/api/v1/models':
                    return self._read(owner.read_model.models)
                if parsed.path == '/api/v1/capabilities':
                    return self._read(owner.read_model.capabilities)
                if parsed.path == '/api/v1/settings':
                    return self._read(owner.read_model.settings)
                if parsed.path == '/api/v1/feishu':
                    return self._read(lambda: owner.read_model.build()['feishu'])
                if parsed.path == '/api/v1/feishu/activity':
                    return self._read(owner.supervisor.activity, 'Feishu activity')
                if parsed.path == '/api/v1/doctor':
                    return self._read(owner.supervisor.doctor_result)
                if parsed.path.startswith('/assets/') and self._authorized():
                    return self._static(parsed.path.removeprefix('/'), 'application/javascript; charset=utf-8')
                self.send_error(HTTPStatus.NOT_FOUND)

            def _read(self, operation, message='Read model'):
                if not self._authorized():
                    return self._json({'status': 'error', 'error_code': 'CONTROL_AUTH_REQUIRED', 'message': 'A local Control Center session is required'}, HTTPStatus.FORBIDDEN)
                return self._json({'status': 'ok', 'error_code': None, 'message': message, 'current_state': operation()})

            def _static(self, relative, content_type):
                path = (owner.web_root / relative).resolve()
                if owner.web_root.resolve() not in path.parents and path != owner.web_root.resolve():
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                if not path.is_file():
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                body = path.read_bytes()
                if path.suffix == '.css':
                    content_type = 'text/css; charset=utf-8'
                elif path.suffix == '.js':
                    content_type = 'application/javascript; charset=utf-8'
                self.send_response(HTTPStatus.OK)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                parsed = urlparse(self.path)
                if parsed.path in {'/api/v1/runtime/remote-execution', '/api/v1/runtime/accept-new-work'}:
                    if not self._authorized() or not self._csrf_valid():
                        return self._json({'status': 'error', 'error_code': 'CONTROL_AUTH_REQUIRED', 'message': 'A local Control Center session is required', 'current_state': owner.supervisor.current_state()}, HTTPStatus.FORBIDDEN)
                    try:
                        length = int(self.headers.get('Content-Length', '0'))
                        body = json.loads(self.rfile.read(length) or b'{}')
                        enabled = body['enabled']
                        if not isinstance(enabled, bool):
                            raise ValueError
                    except Exception:
                        return self._json({'status': 'error', 'error_code': 'CONTROL_INVALID_REQUEST', 'message': 'enabled must be a boolean', 'current_state': owner.supervisor.current_state()}, HTTPStatus.BAD_REQUEST)
                    operation = owner.supervisor.set_remote_execution if parsed.path.endswith('remote-execution') else owner.supervisor.set_accept_new_tasks
                    return self._json(operation(enabled).as_dict())
                session_prefix = '/api/v1/sessions/'
                session_suffix = '/unbind'
                if parsed.path.startswith(session_prefix) and parsed.path.endswith(session_suffix):
                    chat_id = unquote(parsed.path[len(session_prefix):-len(session_suffix)])

                    def unbind_session():
                        FeishuStore(owner.supervisor.database).unbind_session(chat_id)
                        return ControlCommandResult('ok', None, 'Session unbound', owner.supervisor.current_state())

                    return self._command(unbind_session)
                thread_prefix = '/api/v1/threads/'
                thread_suffix = '/open-desktop'
                if parsed.path.startswith(thread_prefix) and parsed.path.endswith(thread_suffix):
                    thread_id = unquote(parsed.path[len(thread_prefix):-len(thread_suffix)])

                    def open_desktop():
                        binding = BindingStore(owner.supervisor.database).get_binding(thread_id)
                        state = owner.supervisor.current_state()
                        if binding is None:
                            return ControlCommandResult('error', 'CONTROL_THREAD_NOT_FOUND', 'The CFR thread binding was not found', state)
                        if binding.active_turn_id is not None:
                            return ControlCommandResult('error', 'CONTROL_THREAD_ACTIVE', 'The CFR thread has an active turn', state)
                        adapter = getattr(getattr(owner.supervisor, '_daemon', None), 'adapter', None)
                        registry = getattr(adapter, 'registry', None)
                        if registry is not None and registry.get(thread_id) is not None:
                            return ControlCommandResult('error', 'CONTROL_THREAD_ACTIVE', 'The CFR thread has an active turn', state)
                        live_leases = getattr(adapter, 'leases', None)
                        if live_leases is not None and live_leases.state_for(thread_id).value != 'idle':
                            return ControlCommandResult('error', 'CONTROL_THREAD_WRITER_BUSY', 'The CFR thread writer is not idle', state)
                        if binding.writer_state != 'idle':
                            return ControlCommandResult('error', 'CONTROL_THREAD_WRITER_BUSY', 'The CFR thread writer is not idle', state)
                        try:
                            open_codex_desktop_thread(binding.thread_id)
                        except NotImplementedError:
                            return ControlCommandResult('error', 'CODEX_DESKTOP_OPEN_UNSUPPORTED', 'Codex Desktop handoff is unsupported on this platform', state)
                        except Exception:
                            return ControlCommandResult('error', 'CODEX_DESKTOP_OPEN_FAILED', 'Codex Desktop could not open the thread', state)
                        return ControlCommandResult('ok', None, 'Codex Desktop open request sent', state)

                    return self._command(open_desktop)
                commands = {
                    '/api/v1/runtime/drain': lambda: owner.supervisor.set_accept_new_tasks(False),
                    '/api/v1/feishu/start': owner.supervisor.start_feishu,
                    '/api/v1/feishu/stop': owner.supervisor.stop_feishu,
                    '/api/v1/feishu/reconnect': owner.supervisor.reconnect_feishu,
                    '/api/v1/feishu/pairing/start': owner.supervisor.start_feishu_pairing,
                    '/api/v1/feishu/pairing/cancel': owner.supervisor.cancel_feishu_pairing,
                    '/api/v1/doctor': owner.supervisor.run_doctor,
                }
                if parsed.path == '/api/v1/feishu/pairing/confirm':
                    if not self._authorized() or not self._csrf_valid():
                        return self._json({'status': 'error', 'error_code': 'CONTROL_AUTH_REQUIRED', 'message': 'A local Control Center session is required', 'current_state': owner.supervisor.current_state()}, HTTPStatus.FORBIDDEN)
                    try:
                        length = int(self.headers.get('Content-Length', '0'))
                        body = json.loads(self.rfile.read(length) or b'')
                        if not isinstance(body, dict) or set(body) != {'workspace_root'} or not isinstance(body['workspace_root'], str) or not body['workspace_root'].strip():
                            raise ValueError
                    except Exception:
                        return self._json({'status': 'error', 'error_code': 'CONTROL_INVALID_REQUEST', 'message': 'workspace_root is required', 'current_state': owner.supervisor.current_state()}, HTTPStatus.BAD_REQUEST)
                    return self._json(owner.supervisor.confirm_feishu_pairing(body['workspace_root']).as_dict())
                workspace_operations = {
                    '/api/v1/feishu/workspaces/add': owner.supervisor.add_workspace_root,
                    '/api/v1/feishu/workspaces/remove': owner.supervisor.remove_workspace_root,
                }
                if parsed.path in workspace_operations:
                    if not self._authorized() or not self._csrf_valid():
                        return self._json({'status': 'error', 'error_code': 'CONTROL_AUTH_REQUIRED', 'message': 'A local Control Center session is required', 'current_state': owner.supervisor.current_state()}, HTTPStatus.FORBIDDEN)
                    try:
                        length = int(self.headers.get('Content-Length', '0'))
                        body = json.loads(self.rfile.read(length) or b'')
                        if not isinstance(body, dict) or set(body) != {'workspace_root'} or not isinstance(body['workspace_root'], str) or not body['workspace_root'].strip():
                            raise ValueError
                    except Exception:
                        return self._json({'status': 'error', 'error_code': 'CONTROL_INVALID_REQUEST', 'message': 'workspace_root is required', 'current_state': owner.supervisor.current_state()}, HTTPStatus.BAD_REQUEST)
                    return self._json(workspace_operations[parsed.path](body['workspace_root']).as_dict())
                operation = commands.get(parsed.path)
                if operation is None:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                return self._command(operation)

            def do_PUT(self):
                parsed = urlparse(self.path)
                if parsed.path != '/api/v1/settings/codex/model-defaults':
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                if not self._authorized() or not self._csrf_valid():
                    return self._json({'status': 'error', 'error_code': 'CONTROL_AUTH_REQUIRED', 'message': 'A local Control Center session is required', 'current_state': owner.read_model.settings()}, HTTPStatus.FORBIDDEN)
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    values = json.loads(self.rfile.read(length) or b'{}')
                    result = owner.read_model.write_model_defaults(values)
                except CodexSettingsError as error:
                    return self._json({'status': 'error', 'error_code': error.error_code, 'message': error.message, 'current_state': owner.read_model.settings()}, HTTPStatus(error.status))
                except Exception:
                    return self._json({'status': 'error', 'error_code': 'CONTROL_INVALID_CODEX_SETTING', 'message': 'Model defaults request is invalid.', 'current_state': owner.read_model.settings()}, HTTPStatus.BAD_REQUEST)
                return self._json(result)

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, name='cfr-control-api', daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self.supervisor.stop_feishu()
