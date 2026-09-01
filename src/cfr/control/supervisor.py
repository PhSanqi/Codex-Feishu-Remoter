from __future__ import annotations

from dataclasses import dataclass
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
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
        self._pairing_code_factory = pairing_code_factory or (lambda: f'{secrets.randbelow(1_000_000):06d}')
        self._pairing_cleanup_timeout = pairing_cleanup_timeout
        self._lock = threading.RLock()
        self._daemon: Any | None = None
        self._transport: Any | None = None
        self._gateway: Any | None = None
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
        try:
            return self._settings_loader(database=self.database, config_store=self._config_store)
        except TypeError:
            return self._settings_loader(database=self.database)

    @property
    def remote_execution_enabled(self) -> bool:
        return self._remote_execution_enabled

    @property
    def accept_new_tasks(self) -> bool:
        return self._accept_new_tasks

    def _is_feishu_running(self) -> bool:
        return bool(
            self._feishu_state == 'running'
            and self._daemon
            and getattr(self._daemon, '_started', False)
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
            return {
                'remote_execution_enabled': self._remote_execution_enabled,
                'accept_new_tasks': self._accept_new_tasks,
                'feishu': {
                    'state': self._feishu_state,
                    'running': running,
                    'last_error_code': self._last_error_code,
                    'last_error_message': self._last_error_message,
                    'pairing': self.pairing_state(),
                },
                'doctor': {
                    'status': (self._last_doctor or {}).get('Verdict', 'NOT_RUN'),
                    'last_result_available': self._last_doctor is not None,
                },
            }

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
                self._pairing_state != 'waiting'
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
            try:
                settings = self._load_settings()
                settings.validate_connection()
                code = self._pairing_code_factory()
                if not isinstance(code, str) or len(code) != 6 or not code.isdigit():
                    raise RuntimeError('pairing code factory returned an invalid code')
                lease = self._connection_lease_factory(settings.database, f'feishu:{settings.app_namespace}').acquire()
                transport = self._transport_factory(settings)
                self._pairing_transport = transport
                self._pairing_lease = lease
                self._pairing_code = code
                self._pairing_expires_at = time.time() + 300
                transport.connect_until_ready(self._handle_pairing_message, timeout=15)
                timer = threading.Timer(300, self._expire_feishu_pairing)
                timer.daemon = True
                self._pairing_timer = timer
                self._pairing_state = 'waiting'
                timer.start()
                self._record_activity('PAIRING_CONNECTION_READY', '配对连接已就绪，请发送配对消息。')
                return self._result('ok', '飞书配对已就绪，请在私聊中发送配对消息。')
            except StructuredError as exc:
                error_code, error_message = exc.code, exc.message
            except Exception:
                error_code, error_message = 'CONTROL_FEISHU_PAIRING_START_FAILED', 'Feishu pairing could not start'
            transport, lease = self._detach_pairing_locked('idle')
            self._last_error_code = error_code
            self._last_error_message = error_message
        self._close_pairing_connection(transport, lease)
        self._record_activity('FEISHU_ERROR', '飞书配对启动失败。')
        return self._result('error', error_message, error_code)

    def cancel_feishu_pairing(self) -> ControlCommandResult:
        with self._lock:
            if not self._pairing_active():
                return self._result('ok', 'Feishu pairing is already inactive')
            transport, lease = self._detach_pairing_locked('idle')
            self._record_activity('PAIRING_CANCELLED', '飞书配对已取消。')
        self._close_pairing_connection(transport, lease)
        return self._result('ok', 'Feishu pairing cancelled')

    def confirm_feishu_pairing(self, workspace_root: str) -> ControlCommandResult:
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
            self._config_store.set_feishu_security(open_ids, roots)
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

    def _workspace_change_allowed(self) -> ControlCommandResult | None:
        if self._is_feishu_running() or self._daemon is not None:
            return self._result('error', '请先停止飞书，再修改允许的工作区。', 'CONTROL_FEISHU_STOP_REQUIRED_FOR_WORKSPACE_CHANGE')
        if self._pairing_active():
            return self._result('error', '配对进行中，暂不能修改工作区。', 'CONTROL_FEISHU_PAIRING_ACTIVE')
        return None

    def add_workspace_root(self, workspace_root: str) -> ControlCommandResult:
        with self._lock:
            if blocked := self._workspace_change_allowed():
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
            return self._result('ok', '允许的工作区已保存。')

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
            return self._result('ok', '允许的工作区已移除。')

    def _stop_owned_runtime(self) -> bool:
        """Stop first; release ownership only once stop has actually returned."""
        if self._daemon is None:
            self._feishu_state = 'stopped'
            return True
        self._feishu_state = 'stopping'
        self._record_activity('FEISHU_STOPPING', '正在停止飞书运行时。', component='feishu', stage='stop')
        try:
            self._daemon.stop()
        except Exception:
            self._feishu_state = 'degraded'
            self._last_error_code = 'CONTROL_FEISHU_STOP_FAILED'
            self._last_error_message = 'Feishu could not be stopped cleanly'
            self._record_activity('FEISHU_ERROR', '飞书停止失败。', level='error', component='feishu', stage='stop', code=self._last_error_code)
            return False
        self._release_runtime()
        self._feishu_state = 'stopped'
        self._record_activity('FEISHU_STOPPED', '飞书已停止。', component='feishu', stage='stop')
        return True

    def start_feishu(self) -> ControlCommandResult:
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
                gateway = self._gateway_factory(settings, daemon.store, daemon)
                # Claim lifecycle ownership before any operation can partially start.
                self._daemon = daemon
                self._transport = transport
                self._gateway = gateway
                stage = 'daemon_start'
                self._record_activity('FEISHU_DAEMON_STARTING', '正在启动飞书后台运行时。', component='feishu', stage=stage)
                daemon.start(background_workers=True)
                stage = 'channel_connect'
                self._record_activity('FEISHU_CHANNEL_CONNECTING', '正在等待飞书连接。', component='feishu', stage=stage)
                transport.connect_until_ready(self._guarded_message_handler, self._guarded_card_action_handler, timeout=15)
                self._feishu_state = 'running'
                self._last_error_code = None
                self._last_error_message = None
                self._record_activity('FEISHU_RUNNING', '飞书运行时已启动。', component='feishu', stage='running')
                return self._result('ok', 'Feishu started')
            except StructuredError as exc:
                start_code, start_message = exc.code, exc.message
                activity_message = f'飞书启动失败：{start_code}。{start_message}'
            except Exception as exc:
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
            if self._daemon is not None and not self._stop_owned_runtime():
                return self._result('error', self._last_error_message or 'Feishu could not be stopped cleanly', self._last_error_code)
            self._feishu_state = 'stopped'
            self._last_error_code = start_code
            self._last_error_message = start_message
            return self._result('error', start_message, start_code)

    def stop_feishu(self) -> ControlCommandResult:
        with self._lock:
            if self._daemon is None:
                if self._pairing_active():
                    return self.cancel_feishu_pairing()
                self._feishu_state = 'stopped'
                return self._result('ok', 'Feishu is already stopped')
            if self._stop_owned_runtime():
                self._last_error_code = None
                self._last_error_message = None
                return self._result('ok', 'Feishu stopped')
            return self._result('error', self._last_error_message or 'Feishu could not be stopped cleanly', self._last_error_code)

    def reconnect_feishu(self) -> ControlCommandResult:
        with self._lock:
            if self._pairing_active():
                return self._result('error', 'Feishu pairing is active', 'CONTROL_FEISHU_PAIRING_ACTIVE')
            self._record_activity('FEISHU_RECONNECTING', '正在重新连接飞书。')
            stop = self.stop_feishu()
            if stop.status != 'ok':
                return stop
            return self.start_feishu()

    def run_doctor(self) -> ControlCommandResult:
        with self._lock:
            try:
                self._record_activity('DOCTOR_STARTING', '正在执行运行诊断。', component='control', stage='doctor')
                self._last_doctor = self._doctor_runner(project_root=self.project_root, database=self.database, live=False)
                self._record_activity('DOCTOR_COMPLETED', f"运行诊断完成：{self._last_doctor.get('Verdict', 'UNKNOWN')}。", component='control', stage='doctor')
                return self._result('ok', 'Doctor completed')
            except Exception:
                self._last_error_code = 'CONTROL_DOCTOR_FAILED'
                self._last_error_message = 'Doctor could not complete'
                self._record_activity('DOCTOR_ERROR', '运行诊断未能完成。', level='error', component='control', stage='doctor', code=self._last_error_code)
                return self._result('error', self._last_error_message, self._last_error_code)

    def doctor_result(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._last_doctor or {'Verdict': 'NOT_RUN'})
