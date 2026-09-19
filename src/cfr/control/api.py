from __future__ import annotations

from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import secrets
import threading
from urllib.parse import parse_qs, unquote, urlparse

from .codex_settings import CodexSettingsError
from .read_model import ControlReadModel
from .supervisor import ControlCommandResult
from cfr.core.models import StructuredError
from cfr.feishu.store import FeishuStore
from cfr.platform import open_codex_desktop_thread, restart_codex_desktop_thread
from cfr.storage.db import BindingStore


LOGGER = logging.getLogger(__name__)
MAX_CONTROL_REQUEST_BYTES = 1024 * 1024


class ControlHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True


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
        self._httpd: ControlHttpServer | None = None
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
                try:
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json; charset=utf-8')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Content-Length', str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True

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

            def _json_body(self):
                length = int(self.headers.get('Content-Length', '0'))
                if length < 0 or length > MAX_CONTROL_REQUEST_BYTES:
                    # Drain at most the bounded rejection window so Windows does
                    # not reset the socket before the client can read the JSON
                    # error. The body is never decoded or parsed.
                    if length > 0:
                        self.rfile.read(min(length, MAX_CONTROL_REQUEST_BYTES + 1))
                    self.close_connection = True
                    raise ValueError('request body is too large')
                return json.loads(self.rfile.read(length) or b'{}')

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
                if parsed.path == '/api/v1/operational':
                    return self._read(owner.read_model.operational, 'Operational dashboard')
                if parsed.path == '/api/v1/approvals':
                    return self._read(owner.read_model.approvals)
                if parsed.path == '/api/v1/models':
                    return self._read(owner.read_model.models)
                if parsed.path == '/api/v1/capabilities':
                    return self._read(owner.read_model.capabilities)
                if parsed.path == '/api/v1/surfaces':
                    return self._read(owner.read_model.surfaces)
                if parsed.path == '/api/v1/settings':
                    return self._read(owner.read_model.settings)
                if parsed.path == '/api/v1/setup':
                    return self._read(owner.supervisor.setup_state, 'Setup readiness')
                if parsed.path == '/api/v1/feishu':
                    return self._read(owner.read_model.feishu)
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
                try:
                    state = operation()
                except Exception:
                    LOGGER.exception('CONTROL_READ_FAILED path=%s operation=%s', self.path, getattr(operation, '__name__', type(operation).__name__))
                    return self._json({
                        'status': 'error',
                        'error_code': 'CONTROL_READ_FAILED',
                        'message': '控制状态暂时不可用，请稍后刷新。',
                        'current_state': None,
                    }, HTTPStatus.SERVICE_UNAVAILABLE)
                return self._json({'status': 'ok', 'error_code': None, 'message': message, 'current_state': state})

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
                try:
                    self.send_response(HTTPStatus.OK)
                    self.send_header('Content-Type', content_type)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True

            def do_POST(self):
                parsed = urlparse(self.path)
                if parsed.path == '/api/v1/surfaces/select':
                    if not self._authorized() or not self._csrf_valid():
                        return self._json({'status': 'error', 'error_code': 'CONTROL_AUTH_REQUIRED', 'message': 'A local Control Center session is required', 'current_state': owner.read_model.surfaces()}, HTTPStatus.FORBIDDEN)
                    try:
                        body = self._json_body()
                        requested = body['surface']
                        requested_chat_id = body.get('chat_id')
                        if not isinstance(requested, str) or not requested.strip():
                            raise ValueError
                        if requested_chat_id is not None and (not isinstance(requested_chat_id, str) or not requested_chat_id.strip()):
                            raise ValueError
                    except Exception:
                        return self._json({'status': 'error', 'error_code': 'CONTROL_INVALID_REQUEST', 'message': 'surface is required', 'current_state': owner.read_model.surfaces()}, HTTPStatus.BAD_REQUEST)
                    catalog = owner.read_model.surfaces()
                    surface = next((item for item in catalog['data'] if item['id'] == requested.strip().lower()), None)
                    if surface is None:
                        return self._json({'status': 'error', 'error_code': 'CONTROL_SURFACE_INVALID', 'message': 'Unknown execution surface', 'current_state': catalog}, HTTPStatus.BAD_REQUEST)
                    store = FeishuStore(owner.supervisor.database)
                    current = store.get_current_surface_state()
                    target_chat_id = requested_chat_id.strip() if requested_chat_id else (catalog.get('chat_id') or (current or {}).get('chat_id'))
                    if not target_chat_id or not store.chat_exists(target_chat_id):
                        return self._json({'status': 'error', 'error_code': 'CONTROL_SURFACE_CHAT_REQUIRED', 'message': 'No Feishu chat is available for Surface selection', 'current_state': catalog}, HTTPStatus.CONFLICT)
                    session = store.get_session(target_chat_id)
                    active_jobs = owner.read_model.jobs()
                    if any(
                        job.get('active')
                        and (
                            job.get('chat_id') == target_chat_id
                            or (session is not None and session.thread_id and job.get('thread_id') == session.thread_id)
                        )
                        for job in active_jobs
                    ):
                        return self._json({
                            'status': 'error',
                            'error_code': 'CONTROL_SURFACE_CHANGE_BUSY',
                            'message': 'Execution Surface cannot change while this Feishu chat has an active task',
                            'current_state': catalog,
                        }, HTTPStatus.CONFLICT)
                    if surface['id'] == 'chat' and not surface['available']:
                        owner.supervisor.start_browser_bridge()
                        catalog = owner.read_model.surfaces()
                        surface = next(item for item in catalog['data'] if item['id'] == 'chat')
                    if not surface['available']:
                        return self._json({'status': 'error', 'error_code': 'CONTROL_SURFACE_UNAVAILABLE', 'message': f'{surface["name"]} Surface is unavailable', 'current_state': catalog}, HTTPStatus.CONFLICT)
                    store.set_selected_surface(target_chat_id, surface['id'])
                    return self._json({'status': 'ok', 'error_code': None, 'message': f'Execution Surface switched to {surface["name"]}', 'current_state': owner.read_model.surfaces(target_chat_id)})
                if parsed.path in {'/api/v1/runtime/remote-execution', '/api/v1/runtime/accept-new-work'}:
                    if not self._authorized() or not self._csrf_valid():
                        return self._json({'status': 'error', 'error_code': 'CONTROL_AUTH_REQUIRED', 'message': 'A local Control Center session is required', 'current_state': owner.supervisor.current_state()}, HTTPStatus.FORBIDDEN)
                    try:
                        body = self._json_body()
                        enabled = body['enabled']
                        if not isinstance(enabled, bool):
                            raise ValueError
                    except Exception:
                        return self._json({'status': 'error', 'error_code': 'CONTROL_INVALID_REQUEST', 'message': 'enabled must be a boolean', 'current_state': owner.supervisor.current_state()}, HTTPStatus.BAD_REQUEST)
                    operation = owner.supervisor.set_remote_execution if parsed.path.endswith('remote-execution') else owner.supervisor.set_accept_new_tasks
                    return self._json(operation(enabled).as_dict())
                session_prefix = '/api/v1/sessions/'
                approval_suffix = '/approval-mode'
                if parsed.path.startswith(session_prefix) and parsed.path.endswith(approval_suffix):
                    chat_id = unquote(parsed.path[len(session_prefix):-len(approval_suffix)])
                    if not self._authorized() or not self._csrf_valid():
                        return self._json({'status': 'error', 'error_code': 'CONTROL_AUTH_REQUIRED', 'message': 'A local Control Center session is required'}, HTTPStatus.FORBIDDEN)
                    try:
                        body = self._json_body()
                        mode = body.get('mode') if isinstance(body, dict) else None
                        if mode not in {'ask', 'auto', 'full'}:
                            raise ValueError
                    except Exception:
                        return self._json({'status': 'error', 'error_code': 'CONTROL_INVALID_REQUEST', 'message': 'mode must be ask, auto, or full'}, HTTPStatus.BAD_REQUEST)
                    store = FeishuStore(owner.supervisor.database)
                    try:
                        store.set_approval_mode(chat_id, mode)
                    except StructuredError as exc:
                        code = 'CONTROL_SESSION_NOT_FOUND' if exc.code == 'FEISHU_NO_ACTIVE_SESSION' else 'CONTROL_APPROVAL_MODE_INVALID'
                        status = HTTPStatus.NOT_FOUND if exc.code == 'FEISHU_NO_ACTIVE_SESSION' else HTTPStatus.BAD_REQUEST
                        return self._json({'status': 'error', 'error_code': code, 'message': 'The Feishu Code session was not found' if status == HTTPStatus.NOT_FOUND else 'The Code approval mode is invalid'}, status)
                    except Exception:
                        LOGGER.exception('CONTROL_APPROVAL_MODE_WRITE_FAILED chat=%s', chat_id[-8:])
                        return self._json({'status': 'error', 'error_code': 'CONTROL_APPROVAL_MODE_WRITE_FAILED', 'message': 'Code approval mode could not be saved'}, HTTPStatus.INTERNAL_SERVER_ERROR)
                    return self._json({'status': 'ok', 'error_code': None, 'message': 'Code approval mode updated', 'current_state': owner.read_model.sessions()})
                session_suffix = '/unbind'
                if parsed.path.startswith(session_prefix) and parsed.path.endswith(session_suffix):
                    chat_id = unquote(parsed.path[len(session_prefix):-len(session_suffix)])

                    def unbind_session():
                        store = FeishuStore(owner.supervisor.database)
                        session = store.get_session(chat_id)
                        active_jobs = owner.read_model.jobs()
                        if any(
                            job.get('active')
                            and (
                                job.get('chat_id') == chat_id
                                or (session is not None and session.thread_id and job.get('thread_id') == session.thread_id)
                            )
                            for job in active_jobs
                        ):
                            return ControlCommandResult(
                                'error',
                                'CONTROL_SESSION_ACTIVE',
                                'Cannot unbind a Feishu session while its current task is active',
                                owner.supervisor.current_state(),
                            )
                        store.unbind_session(chat_id)
                        return ControlCommandResult('ok', None, 'Session unbound', owner.supervisor.current_state())

                    return self._command(unbind_session)
                thread_prefix = '/api/v1/threads/'
                thread_suffix = '/open-desktop'
                if parsed.path.startswith(thread_prefix) and parsed.path.endswith(thread_suffix):
                    thread_id = unquote(parsed.path[len(thread_prefix):-len(thread_suffix)])

                    def open_desktop():
                        body = self._json_body()
                        restart = body.get('restart') is True if isinstance(body, dict) else False
                        binding_store = BindingStore(owner.supervisor.database)
                        binding = binding_store.get_binding(thread_id)
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
                        if restart:
                            live_active = False
                            if registry is not None:
                                snapshot = getattr(registry, 'runtime_snapshot', lambda: {'active': ()})()
                                live_active = bool(snapshot.get('active'))
                            live_writer_busy = bool(
                                live_leases is not None
                                and getattr(live_leases, 'busy_threads', lambda: ())()
                            )
                            if binding_store.has_busy_bindings() or live_active or live_writer_busy:
                                return ControlCommandResult(
                                    'error',
                                    'CONTROL_DESKTOP_RESTART_BUSY',
                                    'Codex Desktop restart is blocked while any CFR thread or writer is active',
                                    state,
                                )
                        try:
                            launcher = owner.supervisor._config_store.get_codex_desktop_launcher()
                            if restart:
                                restart_codex_desktop_thread(binding.thread_id, launcher=launcher)
                            else:
                                open_codex_desktop_thread(binding.thread_id, launcher=launcher)
                        except NotImplementedError:
                            return ControlCommandResult('error', 'CODEX_DESKTOP_OPEN_UNSUPPORTED', 'Codex Desktop handoff is unsupported on this platform', state)
                        except Exception as error:
                            detail = str(error).strip()
                            return ControlCommandResult(
                                'error',
                                'CODEX_DESKTOP_OPEN_FAILED',
                                f'Codex Desktop could not open the thread{": " + detail if detail else ""}',
                                state,
                            )
                        return ControlCommandResult(
                            'ok', None,
                            'Codex Desktop restarted and open request sent' if restart else 'Codex Desktop open request sent',
                            state,
                        )

                    return self._command(open_desktop)
                commands = {
                    '/api/v1/runtime/drain': lambda: owner.supervisor.set_accept_new_tasks(False),
                    '/api/v1/feishu/start': owner.supervisor.start_feishu,
                    '/api/v1/feishu/stop': owner.supervisor.stop_feishu,
                    '/api/v1/feishu/reconnect': owner.supervisor.reconnect_feishu,
                    '/api/v1/feishu/pairing/start': owner.supervisor.start_feishu_pairing,
                    '/api/v1/feishu/pairing/cancel': owner.supervisor.cancel_feishu_pairing,
                    '/api/v1/doctor': owner.supervisor.run_doctor,
                    '/api/v1/setup/codex/validate': owner.supervisor.validate_codex_setup,
                    '/api/v1/setup/codex/login': owner.supervisor.start_codex_login,
                    '/api/v1/setup/chat/start': owner.supervisor.start_chat_setup,
                    '/api/v1/setup/chat/manual-login/start': owner.supervisor.start_manual_chat_login,
                    '/api/v1/setup/chat/manual-login/verify': owner.supervisor.finish_manual_chat_login,
                    '/api/v1/setup/chat/show': owner.supervisor.show_embedded_chat,
                }
                if parsed.path == '/api/v1/setup/preferences':
                    if not self._authorized() or not self._csrf_valid():
                        return self._json({'status': 'error', 'error_code': 'CONTROL_AUTH_REQUIRED', 'message': 'A local Control Center session is required'}, HTTPStatus.FORBIDDEN)
                    try:
                        body = self._json_body()
                        surface = body['default_surface']
                        mode = body['network_mode']
                        proxy_url = body.get('proxy_url')
                        chat_browser_backend = body.get('chat_browser_backend')
                        codex_desktop_launcher = body.get('codex_desktop_launcher')
                        if chat_browser_backend is not None and not isinstance(chat_browser_backend, str):
                            raise ValueError
                        if codex_desktop_launcher is not None and not isinstance(codex_desktop_launcher, str):
                            raise ValueError
                    except Exception:
                        return self._json({'status': 'error', 'error_code': 'CONTROL_INVALID_REQUEST', 'message': 'default_surface and network_mode are required'}, HTTPStatus.BAD_REQUEST)
                    return self._json(owner.supervisor.save_setup_preferences(
                        surface,
                        mode,
                        proxy_url,
                        chat_browser_backend,
                        codex_desktop_launcher,
                    ).as_dict())
                if parsed.path == '/api/v1/setup/feishu/credentials':
                    if not self._authorized() or not self._csrf_valid():
                        return self._json({'status': 'error', 'error_code': 'CONTROL_AUTH_REQUIRED', 'message': 'A local Control Center session is required'}, HTTPStatus.FORBIDDEN)
                    try:
                        body = self._json_body()
                        app_id = body['app_id']
                        app_secret = body['app_secret']
                        if not isinstance(app_id, str) or not isinstance(app_secret, str):
                            raise ValueError
                    except Exception:
                        return self._json({'status': 'error', 'error_code': 'CONTROL_INVALID_REQUEST', 'message': 'app_id and app_secret are required'}, HTTPStatus.BAD_REQUEST)
                    return self._json(owner.supervisor.save_feishu_credentials(app_id, app_secret).as_dict())
                if parsed.path == '/api/v1/feishu/pairing/confirm':
                    if not self._authorized() or not self._csrf_valid():
                        return self._json({'status': 'error', 'error_code': 'CONTROL_AUTH_REQUIRED', 'message': 'A local Control Center session is required', 'current_state': owner.supervisor.current_state()}, HTTPStatus.FORBIDDEN)
                    try:
                        body = self._json_body()
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
                        body = self._json_body()
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
                    values = self._json_body()
                    result = owner.read_model.write_model_defaults(values)
                except CodexSettingsError as error:
                    return self._json({'status': 'error', 'error_code': error.error_code, 'message': error.message, 'current_state': owner.read_model.settings()}, HTTPStatus(error.status))
                except Exception:
                    return self._json({'status': 'error', 'error_code': 'CONTROL_INVALID_CODEX_SETTING', 'message': 'Model defaults request is invalid.', 'current_state': owner.read_model.settings()}, HTTPStatus.BAD_REQUEST)
                return self._json(result)

        self._httpd = ControlHttpServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, name='cfr-control-api', daemon=True)
        self._thread.start()
        return self

    def stop(self):
        cleanup_error = None
        try:
            if self._httpd is not None:
                self._httpd.shutdown()
                self._httpd.server_close()
                self._httpd = None
            if self._thread is not None:
                self._thread.join(timeout=2)
                self._thread = None
        except Exception as error:
            cleanup_error = error
        try:
            self.supervisor.stop_feishu()
        except Exception as error:
            cleanup_error = cleanup_error or error
        try:
            close_browser_bridge = getattr(self.supervisor, 'close_browser_bridge', None)
            if close_browser_bridge is not None:
                close_browser_bridge()
        except Exception as error:
            cleanup_error = cleanup_error or error
        try:
            self.read_model.close()
        except Exception as error:
            cleanup_error = cleanup_error or error
        if cleanup_error is not None:
            raise cleanup_error
