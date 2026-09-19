from __future__ import annotations

from dataclasses import dataclass
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
import logging
import os
import secrets
import threading
import time
from typing import Any, Callable

from cfr.core.models import StructuredError
from cfr.doctor import run_doctor
from cfr.feishu.config import load_settings
from cfr.feishu.connection import FeishuConnectionLease
from cfr.feishu.credentials import LocalConfigStore
from cfr.feishu.commands import CommandParser
from cfr.feishu.daemon import FeishuDaemon
from cfr.feishu.gateway import FeishuGateway
from cfr.feishu.models import FeishuInboundMessage
from cfr.feishu.security import authorize_sender, canonical_workspace, safe_identifier, validate_message_scope
from cfr.feishu.transport import ChannelFeishuTransport
from .setup import SetupManager


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ControlCommandResult:
    status: str
    error_code: str | None
    message: str
    current_state: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            'status': self.status,
            'error_code': self.error_code,
            'message': self.message,
            'current_state': self.current_state,
        }


class CfrSupervisor:
    """Single in-process owner for the M3A Feishu runtime lifecycle."""

    def __init__(
        self,
        *,
        project_root: Path | str | None = None,
        database: Path | str = 'cfr.sqlite3',
        settings_loader: Callable[..., Any] = load_settings,
        transport_factory: Callable[..., Any] = ChannelFeishuTransport,
        daemon_factory: Callable[..., Any] = FeishuDaemon,
        gateway_factory: Callable[..., Any] = FeishuGateway,
        doctor_runner: Callable[..., dict[str, Any]] = run_doctor,
        connection_lease_factory: Callable[..., Any] = FeishuConnectionLease,
        config_store: LocalConfigStore | None = None,
        pairing_code_factory: Callable[[], str] | None = None,
        pairing_cleanup_timeout: float = 5,
    ):
        self.project_root = Path(project_root or Path.cwd())
        self.database = Path(database)
        self._settings_loader = settings_loader
        self._transport_factory = transport_factory
        self._daemon_factory = daemon_factory
        self._gateway_factory = gateway_factory
        self._doctor_runner = doctor_runner
        self._connection_lease_factory = connection_lease_factory
        self._config_store = config_store or LocalConfigStore()
        self._setup = SetupManager(config_store=self._config_store, project_root=self.project_root)
        self._settings_cache_enabled = settings_loader is load_settings
        self._settings_cache: Any | None = None
        self._settings_cache_at = 0.0
        self._settings_cache_ttl = 5.0
        self._pairing_code_factory = pairing_code_factory or (lambda: f'{secrets.randbelow(1_000_000):06d}')
        self._pairing_cleanup_timeout = pairing_cleanup_timeout
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._browser_lifecycle_lock = threading.RLock()
        self._doctor_lock = threading.Lock()
        self._daemon: Any | None = None
        self._transport: Any | None = None
        self._gateway: Any | None = None
        self._chat_adapter: Any | None = None
        self._chat_backend: str | None = None
        self._embedded_chat_host: Any | None = None
        self._chat_login_monitor: threading.Thread | None = None
        self._chat_login_monitor_stop = threading.Event()
        self._feishu_state = 'stopped'
        self._last_error_code: str | None = None
        self._last_error_message: str | None = None
        self._last_doctor: dict[str, Any] | None = None
        self._remote_execution_enabled = True
        self._accept_new_tasks = True
        self._pairing_state = 'idle'
        self._pairing_code: str | None = None
        self._pairing_expires_at: float | None = None
        self._pairing_candidate_open_id: str | None = None
        self._pairing_transport: Any | None = None
        self._pairing_lease: Any | None = None
        self._pairing_timer: threading.Timer | None = None
        self._pairing_cleanup_done = threading.Event()
        self._activity: deque[dict[str, str]] = deque(maxlen=100)
        self._activity_lock = threading.Lock()

    def _record_activity(
        self,
        event: str,
        message: str,
        *,
        level: str = 'info',
        component: str | None = None,
        stage: str | None = None,
        code: str | None = None,
    ) -> None:
        """Keep a process-local, deliberately non-sensitive operator timeline."""
        component = component or event.partition('_')[0].lower()
        timestamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
        item = {
            'at': timestamp,
            'timestamp': timestamp,
            'level': level,
            'component': component,
            'event': event,
            'message': message,
        }
        if stage:
            item['stage'] = stage
        if code:
            item['code'] = code
        with self._activity_lock:
            self._activity.append(item)

    def activity(self) -> list[dict[str, str]]:
        with self._activity_lock:
            return list(self._activity)[-50:]

    def _load_settings(self):
        if self._settings_cache_enabled:
            with self._lock:
                if self._settings_cache is not None and time.monotonic() - self._settings_cache_at < self._settings_cache_ttl:
                    return self._settings_cache
        try:
            settings = self._settings_loader(database=self.database, config_store=self._config_store)
        except TypeError:
            settings = self._settings_loader(database=self.database)
        if self._settings_cache_enabled:
            with self._lock:
                self._settings_cache = settings
                self._settings_cache_at = time.monotonic()
        return settings

    def _invalidate_settings(self) -> None:
        with self._lock:
            self._settings_cache = None
            self._settings_cache_at = 0.0

    @property
    def remote_execution_enabled(self) -> bool:
        return self._remote_execution_enabled

    @property
    def accept_new_tasks(self) -> bool:
        return self._accept_new_tasks

    def _is_feishu_running(self) -> bool:
        daemon_running = bool(
            self._daemon
            and (
                getattr(self._daemon, 'is_running', None)
                if getattr(self._daemon, 'is_running', None) is not None
                else getattr(self._daemon, '_started', False)
            )
        )
        return bool(
            self._feishu_state == 'running'
            and daemon_running
            and self._transport
            and getattr(self._transport, 'is_running', False)
        )

    def _pairing_active(self) -> bool:
        return self._pairing_state in {'starting', 'waiting', 'pending_confirmation'}

    def pairing_state(self) -> dict[str, Any]:
        with self._lock:
            return {
                'state': self._pairing_state,
                'code': self._pairing_code if self._pairing_state in {'starting', 'waiting'} else None,
                'expires_at': self._pairing_expires_at if self._pairing_state in {'starting', 'waiting'} else None,
                'candidate': safe_identifier(self._pairing_candidate_open_id) if self._pairing_candidate_open_id else None,
                'suggested_workspace_root': str(self.project_root.resolve()),
            }

    def current_state(self) -> dict[str, Any]:
        with self._lock:
            running = self._is_feishu_running()
            fatal = getattr(getattr(self, '_daemon', None), 'fatal_error', None)
            disconnected = self._feishu_state == 'running' and self._daemon is not None and not running
            effective_state = 'degraded' if fatal is not None or disconnected else self._feishu_state
            runtime_error_code = getattr(fatal, 'code', None)
            runtime_error_message = getattr(fatal, 'message', None)
            if disconnected and not runtime_error_code:
                runtime_error_code = 'FEISHU_CHANNEL_DISCONNECTED'
                runtime_error_message = '飞书长连接已退出；请使用“重新连接”。'
            return {
                'remote_execution_enabled': self._remote_execution_enabled,
                'accept_new_tasks': self._accept_new_tasks,
                'feishu': {
                    'state': effective_state,
                    'running': running,
                    'last_error_code': runtime_error_code or self._last_error_code,
                    'last_error_message': runtime_error_message or self._last_error_message,
                    'pairing': self.pairing_state(),
                },
                'doctor': {
                    'status': (self._last_doctor or {}).get('Verdict', 'NOT_RUN'),
                    'last_result_available': self._last_doctor is not None,
                },
            }

    def setup_state(self) -> dict[str, Any]:
        with self._lock:
            host = self._embedded_chat_host
        self._setup.set_embedded_chat_available(bool(host and getattr(host, 'available', False)))
        state = dict(self._setup.snapshot())
        chat = dict(state.get('chat') or {})
        with self._lock:
            chat['runtime_backend'] = self._chat_backend
        state['chat'] = chat
        return state

    def attach_embedded_chat_host(self, host) -> None:
        """Attach the desktop-owned WebView2 window without importing desktop code here."""
        with self._lock:
            self._embedded_chat_host = host
        self._setup.set_embedded_chat_available(bool(host and getattr(host, 'available', False)))

    def save_setup_preferences(
        self,
        default_surface: str,
        network_mode: str,
        proxy_url: str | None = None,
        chat_browser_backend: str | None = None,
        codex_desktop_launcher: str | None = None,
    ) -> ControlCommandResult:
        previous_surface = self._config_store.get_default_surface()
        previous_network = self._config_store.get_network_policy()
        previous_backend = self._config_store.get_chat_browser_backend()
        was_running = self._is_feishu_running()
        try:
            self._setup.save_preferences(
                default_surface=default_surface,
                network_mode=network_mode,
                proxy_url=proxy_url,
                chat_browser_backend=chat_browser_backend,
                codex_desktop_launcher=codex_desktop_launcher,
            )
        except StructuredError as exc:
            return self._result('error', exc.message, exc.code)
        except ValueError as exc:
            return self._result('error', str(exc), 'CONTROL_SETUP_NETWORK_INVALID')
        self._invalidate_settings()
        selected_backend = self._config_store.get_chat_browser_backend()
        runtime_changed = (
            self._config_store.get_default_surface() != previous_surface
            or self._config_store.get_network_policy() != previous_network
            or selected_backend != previous_backend
        )
        if was_running and runtime_changed:
            stopped = self.stop_feishu()
            if stopped.status != 'ok':
                return self._result(
                    'error',
                    '配置已保存，但飞书旧连接未能停止；请处理运行时错误后重新连接。',
                    stopped.error_code,
                )
        if selected_backend != previous_backend:
            self.close_browser_bridge()
        if was_running and runtime_changed:
            started = self.start_feishu()
            if started.status != 'ok':
                return self._result(
                    'error',
                    f'配置已保存，但飞书自动重新连接失败：{started.message}',
                    started.error_code,
                )
            return self._result('ok', '配置已保存，飞书已自动重新连接。')
        return self._result('ok', '配置已保存。')

    def save_feishu_credentials(self, app_id: str, app_secret: str) -> ControlCommandResult:
        was_running = self._is_feishu_running()
        try:
            self._setup.save_feishu_credentials(app_id=app_id, app_secret=app_secret)
        except StructuredError as exc:
            return self._result('error', exc.message, exc.code)
        except Exception:
            return self._result('error', 'Feishu credentials could not be saved', 'CONTROL_FEISHU_CREDENTIAL_SAVE_FAILED')
        self._invalidate_settings()
        if was_running:
            stopped = self.stop_feishu()
            if stopped.status != 'ok':
                return self._result(
                    'error',
                    '飞书 App 凭据已保存，但旧连接未能停止；请处理运行时错误后重新连接。',
                    stopped.error_code,
                )
            started = self.start_feishu()
            if started.status != 'ok':
                return self._result(
                    'error',
                    f'飞书 App 凭据已保存，但自动重新连接失败：{started.message}',
                    started.error_code,
                )
            return self._result('ok', '飞书 App 凭据已保存，飞书已自动重新连接。')
        return self._result('ok', '飞书 App 凭据已保存。')

    def validate_codex_setup(self) -> ControlCommandResult:
        if not self._doctor_lock.acquire(blocking=False):
            return self._result('error', 'Codex validation is already running', 'CONTROL_DOCTOR_BUSY')
        self._record_activity('SETUP_CODEX_VALIDATING', '正在验证 Codex 登录与接口兼容性。', component='setup', stage='codex_validation')
        try:
            try:
                try:
                    result = self._doctor_runner(project_root=self.project_root, database=self.database, live=False, gate_origin='host_manual')
                except TypeError:
                    result = self._doctor_runner(project_root=self.project_root, database=self.database, live=False)
            except Exception:
                return self._result('error', 'Codex setup validation failed', 'CONTROL_CODEX_SETUP_VALIDATION_FAILED')
            with self._lock:
                self._last_doctor = result
            if result.get('Verdict') != 'PASS' or result.get('LoginStatus') != 'LOGGED_IN' or result.get('CodexInterfaceCompatibility') != 'PASS':
                return self._result('error', 'Codex is not ready; inspect Doctor details', 'CONTROL_CODEX_SETUP_NOT_READY')
            version = str(result.get('CodexVersion') or '').strip()
            if not version:
                return self._result('error', 'Codex version could not be confirmed', 'CONTROL_CODEX_VERSION_UNKNOWN')
            schema_fingerprint = str(result.get('CodexInterfaceSchemaFingerprint') or '').strip()
            if not schema_fingerprint:
                return self._result('error', 'Codex schema fingerprint could not be confirmed', 'CONTROL_CODEX_SCHEMA_FINGERPRINT_UNKNOWN')
            self._setup.mark_codex_validated(version, schema_fingerprint=schema_fingerprint)
            self._record_activity('SETUP_CODEX_VALIDATED', 'Codex 登录与接口兼容性已验证。', component='setup', stage='codex_validation')
            return self._result('ok', 'Codex setup validated')
        finally:
            self._doctor_lock.release()

    def start_codex_login(self) -> ControlCommandResult:
        try:
            self._setup.launch_codex_login()
        except FileNotFoundError:
            return self._result('error', 'Codex executable was not found', 'CONTROL_CODEX_NOT_INSTALLED')
        except Exception:
            return self._result('error', 'Codex login could not be started', 'CONTROL_CODEX_LOGIN_START_FAILED')
        return self._result('ok', 'Codex login started in a separate terminal')

    def start_chat_setup(self) -> ControlCommandResult:
        force_backend = None
        with self._lock:
            host_available = bool(self._embedded_chat_host and getattr(self._embedded_chat_host, 'available', False))
        if host_available and self._config_store.get_chat_browser_backend() in {'auto', 'embedded'}:
            force_backend = 'embedded'
        state = self.start_browser_bridge(force_backend=force_backend)
        if state.get('available') or state.get('status') == 'waiting_user':
            if state.get('status') == 'waiting_user':
                self._start_chat_login_monitor()
            return self._result('ok', state.get('description') or 'Chat setup started')
        return self._result('error', state.get('description') or 'Chat setup could not start', state.get('error_code') or 'CONTROL_CHAT_SETUP_FAILED')

    def start_manual_chat_login(self) -> ControlCommandResult:
        """Open the effective human-owned Chrome login flow with automation stopped."""
        with self._browser_lifecycle_lock:
            try:
                backend = self._desired_chat_backend()
                if backend not in {'dedicated', 'shared'}:
                    return self._result(
                        'error',
                        '当前 Chat 浏览器后端不使用外部 Chrome 人工登录。',
                        'CONTROL_CHAT_MANUAL_LOGIN_UNSUPPORTED',
                    )
                with self._lock:
                    adapter = self._shared_chat_adapter(force_backend=backend)
                state = adapter.start_manual_login()
            except StructuredError as error:
                return self._result('error', error.message, error.code)
            except Exception:
                return self._result(
                    'error',
                    '无法启动人工 ChatGPT 登录窗口。',
                    'CONTROL_CHAT_MANUAL_LOGIN_START_FAILED',
                )
        self._setup.invalidate()
        self._record_activity(
            'CHAT_MANUAL_LOGIN_STARTED',
            '已打开无自动化的 ChatGPT 人工登录窗口。',
            component='chat',
            stage='manual_login',
        )
        return self._result('ok', state.get('description') or 'ChatGPT manual login started')

    def finish_manual_chat_login(self) -> ControlCommandResult:
        """Attach automation after the human login step is complete."""
        with self._browser_lifecycle_lock:
            try:
                backend = self._desired_chat_backend()
                if backend not in {'dedicated', 'shared'}:
                    return self._result(
                        'error',
                        '当前 Chat 浏览器后端不需要外部 Chrome 登录检测。',
                        'CONTROL_CHAT_MANUAL_LOGIN_UNSUPPORTED',
                    )
                with self._lock:
                    adapter = self._shared_chat_adapter(force_backend=backend)
                state = adapter.finish_manual_login()
            except StructuredError as error:
                return self._result('error', error.message, error.code)
            except Exception:
                return self._result(
                    'error',
                    'ChatGPT 登录状态检测失败。',
                    'CONTROL_CHAT_MANUAL_LOGIN_VERIFY_FAILED',
                )
        self._setup.invalidate()
        if state.get('status') == 'waiting_user':
            return self._result(
                'error',
                state.get('description') or '请先完成并关闭人工登录窗口。',
                state.get('error_code') or 'CONTROL_CHAT_MANUAL_LOGIN_WAITING',
            )
        if state.get('available'):
            self._record_activity(
                'CHAT_MANUAL_LOGIN_VERIFIED',
                'ChatGPT 人工登录已确认，自动化后端已重新接管。',
                component='chat',
                stage='browser_ready',
            )
            return self._result('ok', 'ChatGPT 登录已确认；CFR 可以开始使用 Chat Surface。')
        return self._result(
            'error',
            state.get('description') or 'ChatGPT 登录尚未确认。',
            state.get('error_code') or 'CONTROL_CHAT_MANUAL_LOGIN_NOT_READY',
        )

    def show_embedded_chat(self) -> ControlCommandResult:
        with self._lock:
            host = self._embedded_chat_host
            adapter = self._chat_adapter
        if host is None or not getattr(host, 'available', False):
            return self._result('error', '内置 ChatGPT 窗口只在 CFR Windows EXE 中可用。', 'CONTROL_EMBEDDED_CHAT_UNAVAILABLE')
        try:
            state = adapter.health_snapshot() if adapter is not None and hasattr(adapter, 'health_snapshot') else None
        except Exception:
            state = None
        if not state or state.get('status') != 'waiting_user':
            return self._result(
                'error',
                'ChatGPT WebView2 是 CFR 的隐藏自动化页面，只在需要人工登录时临时显示。',
                'CONTROL_CHAT_AUTOMATION_HIDDEN',
            )
        try:
            host.show()
        except Exception:
            return self._result('error', '内置 ChatGPT 窗口无法显示。', 'CONTROL_EMBEDDED_CHAT_SHOW_FAILED')
        return self._result('ok', '已临时显示 ChatGPT 登录页面')

    def _result(self, status: str, message: str, error_code: str | None = None) -> ControlCommandResult:
        return ControlCommandResult(status, error_code, message, self.current_state())

    def set_remote_execution(self, enabled: bool) -> ControlCommandResult:
        with self._lock:
            self._remote_execution_enabled = bool(enabled)
            return self._result('ok', 'Remote Execution updated')

    def set_accept_new_tasks(self, enabled: bool) -> ControlCommandResult:
        with self._lock:
            self._accept_new_tasks = bool(enabled)
            return self._result('ok', 'Accept New Tasks updated')

    def _new_execution_is_admitted(self, message: Any) -> bool:
        """Commands are operational controls; plain text is a new Codex task."""
        if getattr(message, 'message_type', None) != 'text':
            return True
        if CommandParser().parse(getattr(message, 'text', None)) is not None:
            return True
        return self._remote_execution_enabled and self._accept_new_tasks

    def _reject_new_execution(self, message: Any) -> dict[str, Any]:
        message_id = str(getattr(message, 'message_id', '') or '')
        with self._lock:
            transport = self._transport
        if transport is not None and message_id and getattr(message, 'chat_id', None):
            try:
                transport.reply_text(
                    message_id,
                    str(message.chat_id),
                    'CFR 当前暂停接受新任务。',
                    f'control-admission-{message_id}',
                )
            except Exception:
                # Admission is fail-closed even when the optional user feedback cannot be sent.
                pass
        return {
            'status': 'rejected',
            'reason': 'CONTROL_NOT_ACCEPTING_NEW_TASKS',
            'message_id': message_id,
        }

    def _guarded_message_handler(self, message: Any, event_id: Any = None) -> dict[str, Any]:
        """Apply admission only after the existing M2 authorization boundary."""
        with self._lock:
            gateway = self._gateway
        if gateway is None:
            return {'status': 'ignored', 'reason': 'CONTROL_RUNTIME_NOT_READY'}
        if (
            not isinstance(message, FeishuInboundMessage)
            or not message.is_user
            or not authorize_sender(message, gateway.settings)
            or not validate_message_scope(message, gateway.settings)
        ):
            # Gateway remains the authority for invalid, unauthorized, and out-of-scope events.
            return gateway.handle_message_event(message, event_id)
        command = CommandParser().parse(message.text)
        with self._lock:
            admitted = self._new_execution_is_admitted(message)
        if not admitted:
            return self._reject_new_execution(message)
        if command is not None:
            self._record_activity('FEISHU_COMMAND_RECEIVED', f'收到控制命令 /{command.name}。', component='feishu', stage='command_dispatch')
        try:
            result = gateway.handle_message_event(message, event_id)
        except Exception as exc:
            event = 'FEISHU_COMMAND_ERROR' if command is not None else 'FEISHU_MESSAGE_RUNTIME_ERROR'
            message_text = f'控制命令处理失败：{type(exc).__name__}。' if command is not None else '飞书消息处理失败。'
            self._record_activity(event, message_text, level='error', component='feishu', stage='command_dispatch', code='FEISHU_COMMAND_RUNTIME_ERROR')
            raise
        if command is not None:
            if isinstance(result, dict) and result.get('status') == 'queued_command':
                self._record_activity('FEISHU_COMMAND_QUEUED', f'控制命令 /{command.name} 已按会话顺序排队。', component='feishu', stage='command_dispatch')
            else:
                self._record_activity('FEISHU_COMMAND_COMPLETED', f'控制命令 /{command.name} 已处理。', component='feishu', stage='command_dispatch')
        elif isinstance(result, dict) and result.get('status') == 'queued':
            self._record_activity('FEISHU_TASK_QUEUED', '飞书任务已排队。', component='feishu', stage='task_dispatch')
        return result

    def _guarded_card_action_handler(self, payload: Any) -> Any:
        """Approval terminal callbacks never depend on new-task admission flags."""
        with self._lock:
            gateway = self._gateway
        if gateway is None:
            raise StructuredError('FEISHU_DAEMON_REQUIRED', 'Card actions require the running Feishu daemon')
        return gateway.handle_card_action(payload)

    def _release_runtime(self) -> None:
        self._daemon = None
        self._transport = None
        self._gateway = None

    @property
    def chat_adapter(self):
        return self._chat_adapter

    def chat_status_snapshot(self):
        """Read cached Chat health without starting Chrome or querying the DOM."""
        with self._lock:
            adapter = self._chat_adapter
        if adapter is None:
            return None
        snapshot = getattr(adapter, 'health_snapshot', None)
        if snapshot is None:
            return None
        try:
            return snapshot()
        except Exception:
            return {
                'available': False,
                'status': 'not_connected',
                'error_code': 'CHAT_HEALTH_SNAPSHOT_FAILED',
                'description': 'CFR Chat 浏览器状态不可用。',
            }

    def _desired_chat_backend(self, force_backend=None):
        from cfr.chat import linux_shared_tab_mode
        if linux_shared_tab_mode():
            return 'shared'
        requested = str(force_backend or self._config_store.get_chat_browser_backend()).strip().lower()
        with self._lock:
            host = self._embedded_chat_host
        embedded_available = bool(host and getattr(host, 'available', False))
        if requested == 'embedded':
            if not embedded_available:
                # A migrated Linux config can still contain the old Windows
                # WebView2 preference. Treat it as stale platform state and
                # use the shared system Chrome instead of blocking Feishu.
                if os.name != 'nt':
                    return 'shared'
                raise StructuredError('CHAT_EMBEDDED_BROWSER_UNAVAILABLE', '当前运行环境没有 CFR 内置 ChatGPT 窗口。')
            return 'embedded'
        if requested == 'dedicated':
            return 'dedicated'
        if requested == 'shared':
            return 'shared'
        from cfr.chat import chat_setup_snapshot
        state = chat_setup_snapshot(browser_backend='auto', embedded_available=embedded_available)
        return state['effective_backend']

    def _shared_chat_adapter(self, force_backend=None):
        backend = self._desired_chat_backend(force_backend)
        if self._chat_adapter is not None and self._chat_backend in {None, backend}:
            self._chat_backend = backend
            return self._chat_adapter
        if self._chat_adapter is not None and (self._daemon is not None or self._is_feishu_running()):
            raise StructuredError(
                'CONTROL_CHAT_BACKEND_STOP_REQUIRED',
                '请先停止飞书，再切换 Chat 浏览器后端。',
            )
        old, self._chat_adapter = self._chat_adapter, None
        self._chat_backend = None
        close = getattr(old, 'close', None)
        if close is not None:
            close()
        from cfr.chat import ChromeChatAdapter, ChromeDevToolsMcp
        from cfr.chrome_use import ChromeUseBridge
        if backend == 'embedded':
            host = self._embedded_chat_host
            state_dir = Path(host.state_dir)
            adapter = ChromeChatAdapter(
                ChromeDevToolsMcp(
                    mode='embedded',
                    browser_url=host.browser_url,
                    user_data_dir=state_dir / 'profile',
                ),
                show_browser=host.show,
            )
        elif backend == 'shared':
            adapter = ChromeChatAdapter(ChromeUseBridge())
        else:
            adapter = ChromeChatAdapter()
        self._chat_adapter = adapter
        self._chat_backend = backend
        return adapter

    def start_browser_bridge(self, force_backend=None):
        """Own one browser runtime for the full CFR control-plane lifetime."""
        def start_adapter(adapter):
            try:
                return adapter.start()
            except StructuredError as error:
                return {
                    'available': False,
                    'status': 'not_connected',
                    'error_code': error.code,
                    'description': error.message,
                }
            except Exception:
                return {
                    'available': False,
                    'status': 'not_connected',
                    'error_code': 'CHAT_BROWSER_START_FAILED',
                    'description': 'CFR Chat 浏览器运行时启动失败。',
                }

        with self._browser_lifecycle_lock:
            with self._lock:
                adapter = self._shared_chat_adapter(force_backend=force_backend)
            state = start_adapter(adapter)

            # Windows desktop auto remains embedded-first. Linux auto resolves
            # to shared/system Chrome so CFR reuses the human browser identity
            # instead of creating another partially-authenticated profile.
        if state.get('status') == 'waiting_user':
            self._record_activity('CHAT_BROWSER_WAITING_USER', 'CFR Chat 浏览器等待人工完成 ChatGPT 登录或连接授权。', component='chat', stage='browser_waiting_user')
        elif not state.get('available'):
            self._record_activity(
                'CHAT_BROWSER_UNAVAILABLE',
                state.get('description') or 'CFR Chat 专用浏览器当前不可用。',
                level='error',
                component='chat',
                stage=state.get('status') or 'browser_unavailable',
                code=state.get('error_code'),
            )
        else:
            self._record_activity('CHAT_BROWSER_READY', 'CFR ChatGPT 浏览器运行时已启动。', component='chat', stage='browser_ready')
        return state

    def close_browser_bridge(self) -> None:
        with self._browser_lifecycle_lock:
            monitor_stop = getattr(self, '_chat_login_monitor_stop', None)
            if monitor_stop is not None:
                monitor_stop.set()
            with self._lock:
                adapter, self._chat_adapter = self._chat_adapter, None
                self._chat_backend = None
                monitor = getattr(self, '_chat_login_monitor', None)
            close = getattr(adapter, 'close', None)
            if close is not None:
                close()
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join(timeout=2.0)
        with self._lock:
            if getattr(self, '_chat_login_monitor', None) is monitor:
                self._chat_login_monitor = None

    def shutdown(self) -> dict[str, Any]:
        """Best-effort, idempotent process shutdown for desktop ownership."""
        errors = []
        with self._lock:
            self._accept_new_tasks = False
        try:
            result = self.stop_feishu()
            if result.status != 'ok':
                errors.append(result.error_code or 'CONTROL_FEISHU_STOP_FAILED')
        except Exception:
            errors.append('CONTROL_FEISHU_STOP_FAILED')
        try:
            self.close_browser_bridge()
        except Exception:
            errors.append('CONTROL_CHAT_BROWSER_STOP_FAILED')
        return {'status': 'ok' if not errors else 'degraded', 'errors': errors}

    def _start_chat_login_monitor(self):
        with self._lock:
            current = self._chat_login_monitor
            adapter = self._chat_adapter
            if current is not None and current.is_alive():
                return
            self._chat_login_monitor_stop = threading.Event()
            stop = self._chat_login_monitor_stop

        def monitor():
            deadline = time.monotonic() + 10 * 60
            while not stop.wait(1.5) and time.monotonic() < deadline:
                with self._lock:
                    if self._chat_adapter is not adapter:
                        return
                try:
                    state = adapter.health()
                except Exception:
                    continue
                if state.get('available'):
                    self._setup.invalidate()
                    with self._lock:
                        backend = self._chat_backend
                        host = self._embedded_chat_host
                    event = 'CHAT_EMBEDDED_LOGIN_READY' if backend == 'embedded' else 'CHAT_BROWSER_LOGIN_READY'
                    message = '内置 ChatGPT 登录已确认。' if backend == 'embedded' else 'ChatGPT 登录已确认。'
                    self._record_activity(event, message, component='chat', stage='browser_ready')
                    if backend == 'embedded' and host is not None:
                        try:
                            hide_automation = getattr(host, 'hide_automation_page', None)
                            if hide_automation is not None:
                                hide_automation()
                            else:
                                host.show_control_center()
                        except Exception:
                            LOGGER.exception('embedded ChatGPT login page could not be hidden after login')
                    return
                if state.get('status') not in {'waiting_user', 'page_not_ready', 'starting'}:
                    return

        thread = threading.Thread(target=monitor, name='cfr-chat-login-monitor', daemon=True)
        with self._lock:
            self._chat_login_monitor = thread
        thread.start()

    def _close_pairing_connection(self, transport: Any | None, lease: Any | None) -> None:
        try:
            if transport is not None:
                transport.stop()
        finally:
            if lease is not None:
                lease.release()
            self._pairing_cleanup_done.set()
            self._record_activity('PAIRING_CONNECTION_RELEASED', '配对连接已释放。')

    def _detach_pairing_locked(self, state: str) -> tuple[Any | None, Any | None]:
        timer = self._pairing_timer
        self._pairing_timer = None
        if timer is not None:
            timer.cancel()
        transport, lease = self._pairing_transport, self._pairing_lease
        self._pairing_transport = None
        self._pairing_lease = None
        self._pairing_code = None
        self._pairing_expires_at = None
        self._pairing_candidate_open_id = None
        self._pairing_state = state
        return transport, lease

    def _expire_feishu_pairing(self) -> None:
        with self._lifecycle_lock:
            with self._lock:
                if self._pairing_state not in {'starting', 'waiting'}:
                    return
                transport, lease = self._detach_pairing_locked('expired')
                self._record_activity('PAIRING_EXPIRED', '配对码已过期。')
        threading.Thread(
            target=self._close_pairing_connection,
            args=(transport, lease),
            name='cfr-feishu-pairing-cleanup',
            daemon=True,
        ).start()

    def _handle_pairing_message(self, message: Any, _event_id: Any = None) -> dict[str, Any]:
        """Capture one private sender identity without touching the Gateway."""
        with self._lock:
            code = self._pairing_code
            invalid = (
                self._pairing_state not in {'starting', 'waiting'}
                or not isinstance(message, FeishuInboundMessage)
                or not message.is_user
                or message.chat_type != 'p2p'
                or message.message_type != 'text'
                or not message.sender_open_id
                or message.text is None
                or not code
                or message.text.strip() != f'绑定 {code}'
            )
            if invalid:
                if isinstance(message, FeishuInboundMessage) and message.is_user and message.chat_type == 'p2p' and message.message_type == 'text':
                    self._record_activity('PAIRING_MESSAGE_IGNORED', '配对期间收到不匹配的私聊文本。')
                return {'status': 'ignored'}
            self._pairing_candidate_open_id = message.sender_open_id
            self._pairing_code = None
            self._pairing_expires_at = None
            timer = self._pairing_timer
            self._pairing_timer = None
            if timer is not None:
                timer.cancel()
            transport, lease = self._pairing_transport, self._pairing_lease
            self._pairing_transport = None
            self._pairing_lease = None
            self._pairing_state = 'pending_confirmation'
            self._record_activity('PAIRING_CANDIDATE_DETECTED', f'检测到候选账号 {safe_identifier(message.sender_open_id)}。')
        threading.Thread(
            target=self._close_pairing_connection,
            args=(transport, lease),
            name='cfr-feishu-pairing-cleanup',
            daemon=True,
        ).start()
        return {'status': 'pending_confirmation'}

    def start_feishu_pairing(self) -> ControlCommandResult:
        with self._lifecycle_lock:
            with self._lock:
                if self._daemon is not None:
                    return self._result(
                        'error', 'Stop Feishu before pairing a new account', 'CONTROL_FEISHU_MUST_STOP_BEFORE_PAIRING'
                    )
                if self._pairing_active():
                    return self._result('error', 'Feishu pairing is already active', 'CONTROL_FEISHU_PAIRING_ACTIVE')
                self._pairing_state = 'starting'
                self._pairing_cleanup_done.clear()
            self._record_activity('PAIRING_STARTED', '正在建立安全配对连接。')
            transport = None
            lease = None
            try:
                settings = self._load_settings()
                settings.validate_connection()
                code = self._pairing_code_factory()
                if not isinstance(code, str) or len(code) != 6 or not code.isdigit():
                    raise RuntimeError('pairing code factory returned an invalid code')
                lease = self._connection_lease_factory(settings.database, f'feishu:{settings.app_namespace}').acquire()
                transport = self._transport_factory(settings)
                with self._lock:
                    self._pairing_transport = transport
                    self._pairing_lease = lease
                    self._pairing_code = code
                    self._pairing_expires_at = time.time() + 300
                transport.connect_until_ready(self._handle_pairing_message, timeout=15)
                with self._lock:
                    candidate_detected = self._pairing_state == 'pending_confirmation'
                    if not candidate_detected:
                        if self._pairing_state != 'starting':
                            raise StructuredError('CONTROL_FEISHU_PAIRING_STATE_CHANGED', 'Feishu pairing state changed during connection setup')
                        timer = threading.Timer(300, self._expire_feishu_pairing)
                        timer.daemon = True
                        self._pairing_timer = timer
                        self._pairing_state = 'waiting'
                    else:
                        timer = None
                if timer is not None:
                    timer.start()
                if candidate_detected:
                    return self._result('ok', '已检测到飞书账号，请在控制中心确认工作区。')
                self._record_activity('PAIRING_CONNECTION_READY', '配对连接已就绪，请发送配对消息。')
                return self._result('ok', '飞书配对已就绪，请在私聊中发送配对消息。')
            except StructuredError as exc:
                error_code, error_message = exc.code, exc.message
            except Exception:
                error_code, error_message = 'CONTROL_FEISHU_PAIRING_START_FAILED', 'Feishu pairing could not start'
            with self._lock:
                owned_transport, owned_lease = self._detach_pairing_locked('idle')
                self._last_error_code = error_code
                self._last_error_message = error_message
            self._close_pairing_connection(owned_transport or transport, owned_lease or lease)
            self._record_activity('FEISHU_ERROR', '飞书配对启动失败。')
            return self._result('error', error_message, error_code)

    def cancel_feishu_pairing(self) -> ControlCommandResult:
        with self._lifecycle_lock:
            with self._lock:
                if not self._pairing_active():
                    return self._result('ok', 'Feishu pairing is already inactive')
                transport, lease = self._detach_pairing_locked('idle')
                self._record_activity('PAIRING_CANCELLED', '飞书配对已取消。')
            self._close_pairing_connection(transport, lease)
            return self._result('ok', 'Feishu pairing cancelled')

    def confirm_feishu_pairing(self, workspace_root: str) -> ControlCommandResult:
        with self._lifecycle_lock:
            with self._lock:
                if self._pairing_state == 'expired':
                    return self._result('error', 'Feishu pairing has expired', 'CONTROL_FEISHU_PAIRING_EXPIRED')
                if self._pairing_state != 'pending_confirmation':
                    return self._result('error', 'Feishu pairing confirmation is required', 'CONTROL_FEISHU_PAIRING_CONFIRM_REQUIRED')
                candidate = self._pairing_candidate_open_id
                if not candidate:
                    return self._result('error', 'Feishu pairing candidate is missing', 'CONTROL_FEISHU_PAIRING_CANDIDATE_MISSING')
            try:
                root = canonical_workspace(workspace_root)
            except StructuredError:
                return self._result('error', 'Workspace must be an existing absolute directory', 'CONTROL_FEISHU_PAIRING_WORKSPACE_INVALID')
            open_ids = tuple(dict.fromkeys((*self._config_store.get_allowed_open_ids(), candidate)))
            roots = tuple(dict.fromkeys((*self._config_store.get_allowed_workspace_roots(), str(root))))
            try:
                self._config_store.set_feishu_security(open_ids, roots)
            except Exception:
                return self._result('error', 'Feishu pairing policy could not be saved', 'CONTROL_FEISHU_PAIRING_SAVE_FAILED')
            self._invalidate_settings()
            with self._lock:
                if self._pairing_state != 'pending_confirmation' or self._pairing_candidate_open_id != candidate:
                    return self._result('error', 'Feishu pairing state changed during confirmation', 'CONTROL_FEISHU_PAIRING_STATE_CHANGED')
                self._detach_pairing_locked('idle')
                self._record_activity('PAIRING_CONFIRMED', '飞书账号绑定已保存，正在交接到正式运行时。')
                if self._last_error_code in {
                    'FEISHU_OPERATOR_ALLOWLIST_REQUIRED',
                    'FEISHU_WORKSPACE_ALLOWLIST_REQUIRED',
                    'FEISHU_WORKSPACE_ROOT_INVALID',
                }:
                    self._last_error_code = None
                    self._last_error_message = None
            if not self._pairing_cleanup_done.wait(self._pairing_cleanup_timeout):
                self._record_activity('PAIRING_HANDOFF_TIMEOUT', '配对连接仍在退出，未启动第二个连接。')
                return self._result('error', '飞书账号已绑定，但配对连接退出超时；请稍后手动启动。', 'CONTROL_FEISHU_PAIRING_HANDOFF_TIMEOUT')
            started = self.start_feishu()
            if started.status == 'ok':
                return self._result('ok', '飞书账号已绑定并启动。')
            return self._result('error', f'飞书账号已绑定，但启动失败：{started.message}', started.error_code)

    def _workspace_change_allowed(self, *, allow_running: bool = False) -> ControlCommandResult | None:
        if self._pairing_active():
            return self._result('error', '配对进行中，暂不能修改工作区。', 'CONTROL_FEISHU_PAIRING_ACTIVE')
        if self._daemon is not None:
            if allow_running and self._is_feishu_running():
                return None
            if self._is_feishu_running():
                return self._result('error', '请先停止飞书，再移除允许的工作区。', 'CONTROL_FEISHU_STOP_REQUIRED_FOR_WORKSPACE_CHANGE')
            return self._result('error', '飞书运行时正在切换状态，请稍后再修改工作区。', 'CONTROL_FEISHU_RUNTIME_TRANSITION')
        return None

    def _apply_workspace_roots_to_running_runtime(self, roots) -> None:
        if not self._is_feishu_running() or self._daemon is None:
            return
        updater = getattr(self._daemon, 'update_workspace_roots', None)
        if updater is None:
            return
        settings = updater(roots)
        if self._gateway is not None:
            self._gateway.settings = settings

    @staticmethod
    def _runtime_requires_chat(settings, daemon) -> bool:
        if getattr(settings, 'default_surface', 'code') == 'chat':
            return True
        list_states = getattr(getattr(daemon, 'store', None), 'list_surface_states', None)
        if list_states is None:
            return False
        return any(
            isinstance(item, dict) and item.get('selected_surface') == 'chat'
            for item in list_states()
        )

    def add_workspace_root(self, workspace_root: str) -> ControlCommandResult:
        with self._lock:
            if blocked := self._workspace_change_allowed(allow_running=True):
                return blocked
            settings = self._load_settings()
            if getattr(settings, 'workspace_policy_source', 'persistent') == 'environment':
                return self._result('error', '工作区由环境变量管理，控制中心不能修改。', 'CONTROL_FEISHU_WORKSPACE_POLICY_ENVIRONMENT')
            try:
                root = canonical_workspace(workspace_root)
            except StructuredError:
                return self._result('error', '工作区必须是存在的绝对目录。', 'CONTROL_FEISHU_WORKSPACE_INVALID')
            roots = tuple(dict.fromkeys((*self._config_store.get_allowed_workspace_roots(), str(root))))
            self._config_store.set_feishu_security(self._config_store.get_allowed_open_ids(), roots)
            self._invalidate_settings()
            self._apply_workspace_roots_to_running_runtime(roots)
            return self._result('ok', '允许的工作区已保存并立即生效。')

    def remove_workspace_root(self, workspace_root: str) -> ControlCommandResult:
        with self._lock:
            if blocked := self._workspace_change_allowed():
                return blocked
            settings = self._load_settings()
            if getattr(settings, 'workspace_policy_source', 'persistent') == 'environment':
                return self._result('error', '工作区由环境变量管理，控制中心不能修改。', 'CONTROL_FEISHU_WORKSPACE_POLICY_ENVIRONMENT')
            path = Path(workspace_root).expanduser()
            if not path.is_absolute():
                return self._result('error', '工作区必须是绝对目录。', 'CONTROL_FEISHU_WORKSPACE_INVALID')
            root = str(path.resolve())
            roots = tuple(value for value in self._config_store.get_allowed_workspace_roots() if str(Path(value).expanduser().resolve()) != root)
            self._config_store.set_feishu_security(self._config_store.get_allowed_open_ids(), roots)
            self._invalidate_settings()
            self._apply_workspace_roots_to_running_runtime(roots)
            return self._result('ok', '允许的工作区已移除并立即生效。')

    def _stop_owned_runtime(self) -> bool:
        """Stop first; release ownership only once stop has actually returned."""
        with self._lock:
            daemon = self._daemon
            if daemon is None:
                self._feishu_state = 'stopped'
                return True
            self._feishu_state = 'stopping'
        self._record_activity('FEISHU_STOPPING', '正在停止飞书运行时。', component='feishu', stage='stop')
        try:
            daemon.stop()
        except Exception:
            with self._lock:
                self._feishu_state = 'degraded'
                self._last_error_code = 'CONTROL_FEISHU_STOP_FAILED'
                self._last_error_message = 'Feishu could not be stopped cleanly'
            self._record_activity('FEISHU_ERROR', '飞书停止失败。', level='error', component='feishu', stage='stop', code=self._last_error_code)
            return False
        with self._lock:
            if self._daemon is daemon:
                self._release_runtime()
            self._feishu_state = 'stopped'
        self._record_activity('FEISHU_STOPPED', '飞书已停止。', component='feishu', stage='stop')
        return True

    def start_feishu(self) -> ControlCommandResult:
        with self._lifecycle_lock:
            with self._lock:
                if self._pairing_active():
                    return self._result('error', 'Feishu pairing is active', 'CONTROL_FEISHU_PAIRING_ACTIVE')
                if self._is_feishu_running():
                    return self._result('ok', 'Feishu is already running')
                if self._daemon is not None:
                    return self._result(
                        'error',
                        'Feishu runtime ownership is unresolved; stop it before starting again',
                        'CONTROL_FEISHU_RUNTIME_OWNERSHIP_UNRESOLVED',
                    )
                self._feishu_state = 'starting'
            stage = 'settings_load'
            daemon = None
            self._record_activity('FEISHU_STARTING', '正在读取飞书运行配置。', component='feishu', stage=stage)
            try:
                settings = self._load_settings()
                stage = 'settings_validate'
                self._record_activity('FEISHU_SETTINGS_VALIDATING', '正在检查飞书运行配置。', component='feishu', stage=stage)
                settings.validate_execution()
                stage = 'transport_create'
                self._record_activity('FEISHU_TRANSPORT_CREATING', '正在创建飞书连接。', component='feishu', stage=stage)
                transport = self._transport_factory(settings)
                stage = 'daemon_create'
                self._record_activity('FEISHU_DAEMON_CREATING', '正在创建飞书后台运行时。', component='feishu', stage=stage)
                daemon = self._daemon_factory(settings, transport)
                if isinstance(daemon, FeishuDaemon):
                    if self._runtime_requires_chat(settings, daemon):
                        stage = 'chat_browser_start'
                        self._record_activity(
                            'CHAT_BROWSER_STARTING',
                            'Chat Surface 已被选中，正在确认 ChatGPT 浏览器运行时。',
                            component='chat',
                            stage=stage,
                        )
                        browser_state = self.start_browser_bridge()
                        if not browser_state.get('available'):
                            raise StructuredError(
                                browser_state.get('error_code') or 'CHAT_BROWSER_NOT_READY',
                                browser_state.get('description') or 'ChatGPT browser runtime is not ready',
                            )
                    with self._lock:
                        daemon.chat_adapter = self._shared_chat_adapter()
                    daemon._owns_chat_adapter = False
                gateway = self._gateway_factory(settings, daemon.store, daemon)
                # Claim ownership before any start operation can partially succeed.
                with self._lock:
                    if self._pairing_active() or self._daemon is not None:
                        raise StructuredError('CONTROL_FEISHU_RUNTIME_OWNERSHIP_UNRESOLVED', 'Feishu runtime ownership changed during startup')
                    self._daemon = daemon
                    self._transport = transport
                    self._gateway = gateway
                stage = 'daemon_start'
                self._record_activity('FEISHU_DAEMON_STARTING', '正在启动飞书后台运行时。', component='feishu', stage=stage)
                daemon.start(background_workers=True)
                stage = 'channel_connect'
                self._record_activity('FEISHU_CHANNEL_CONNECTING', '正在等待飞书连接。', component='feishu', stage=stage)
                transport.connect_until_ready(self._guarded_message_handler, self._guarded_card_action_handler, timeout=15)
                with self._lock:
                    self._feishu_state = 'running'
                    self._last_error_code = None
                    self._last_error_message = None
                self._record_activity('FEISHU_RUNNING', '飞书运行时已启动。', component='feishu', stage='running')
                return self._result('ok', 'Feishu started')
            except StructuredError as exc:
                start_code, start_message = exc.code, exc.message
                activity_message = f'飞书启动失败：{start_code}。{start_message}'
            except Exception:
                start_code, start_message = 'CONTROL_FEISHU_START_FAILED', 'Feishu could not be started'
                activity_message = f'飞书启动失败：{start_code}。'
            self._record_activity(
                'FEISHU_ERROR',
                activity_message,
                level='error',
                component='feishu',
                stage=stage,
                code=start_code,
            )
            with self._lock:
                owns_runtime = daemon is not None and self._daemon is daemon
            if owns_runtime and not self._stop_owned_runtime():
                return self._result('error', self._last_error_message or 'Feishu could not be stopped cleanly', self._last_error_code)
            with self._lock:
                self._feishu_state = 'stopped'
                self._last_error_code = start_code
                self._last_error_message = start_message
            return self._result('error', start_message, start_code)

    def stop_feishu(self) -> ControlCommandResult:
        with self._lifecycle_lock:
            with self._lock:
                daemon_missing = self._daemon is None
                pairing_active = self._pairing_active()
            if daemon_missing:
                if pairing_active:
                    return self.cancel_feishu_pairing()
                with self._lock:
                    self._feishu_state = 'stopped'
                return self._result('ok', 'Feishu is already stopped')
            if self._stop_owned_runtime():
                with self._lock:
                    self._last_error_code = None
                    self._last_error_message = None
                return self._result('ok', 'Feishu stopped')
            return self._result('error', self._last_error_message or 'Feishu could not be stopped cleanly', self._last_error_code)

    def reconnect_feishu(self) -> ControlCommandResult:
        with self._lifecycle_lock:
            with self._lock:
                if self._pairing_active():
                    return self._result('error', 'Feishu pairing is active', 'CONTROL_FEISHU_PAIRING_ACTIVE')
            self._record_activity('FEISHU_RECONNECTING', '正在重新连接飞书。')
            stop = self.stop_feishu()
            if stop.status != 'ok':
                return stop
            return self.start_feishu()

    def run_doctor(self) -> ControlCommandResult:
        if not self._doctor_lock.acquire(blocking=False):
            return self._result('error', 'Doctor is already running', 'CONTROL_DOCTOR_BUSY')
        self._record_activity('DOCTOR_STARTING', '正在执行运行诊断。', component='control', stage='doctor')
        try:
            try:
                result = self._doctor_runner(project_root=self.project_root, database=self.database, live=False)
                with self._lock:
                    self._last_doctor = result
                self._record_activity('DOCTOR_COMPLETED', f"运行诊断完成：{result.get('Verdict', 'UNKNOWN')}。", component='control', stage='doctor')
                return self._result('ok', 'Doctor completed')
            except Exception:
                with self._lock:
                    self._last_error_code = 'CONTROL_DOCTOR_FAILED'
                    self._last_error_message = 'Doctor could not complete'
                self._record_activity('DOCTOR_ERROR', '运行诊断未能完成。', level='error', component='control', stage='doctor', code=self._last_error_code)
                return self._result('error', self._last_error_message, self._last_error_code)
        finally:
            self._doctor_lock.release()

    def doctor_result(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._last_doctor or {'Verdict': 'NOT_RUN'})
