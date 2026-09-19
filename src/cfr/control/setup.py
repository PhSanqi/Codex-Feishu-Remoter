from __future__ import annotations

from pathlib import Path
import hashlib
import os
import subprocess
import threading
import time
from typing import Any

from cfr.chat import chat_setup_snapshot
from cfr.chrome_use import chrome_use_setup_snapshot
from cfr.codex.diagnostics import login_status
from cfr.codex.launcher import CodexLauncher
from cfr.core.models import StructuredError
from cfr.feishu.credentials import KeyringSecretStore, LocalConfigStore, set_persistent_credentials
from cfr.feishu.transport import channel_sdk_metadata
from cfr.network import resolve_cfr_proxy, sanitized_proxy_url, validate_proxy_url
from cfr.platform import codex_desktop_runtime_snapshot, hidden_subprocess_kwargs


SETUP_SCHEMA_VERSION = 2


def _check(key: str, label: str, *, required: bool, ready: bool, state: str, detail: str, action: str | None = None) -> dict[str, Any]:
    return {
        'key': key,
        'label': label,
        'required': required,
        'ready': ready,
        'state': state,
        'detail': detail,
        'action': action,
    }


def _codex_version(launcher: CodexLauncher) -> str | None:
    try:
        completed = subprocess.run(
            launcher.build_command('--version'),
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=10,
            check=False,
            **hidden_subprocess_kwargs(),
        )
    except Exception:
        return None
    value = (completed.stdout or completed.stderr or '').strip()
    return value or None


def _codex_runtime_revision(resolved_path: str | Path | None) -> str | None:
    """Identify installed Codex payload content, not only its version text."""
    if not resolved_path:
        return None
    launcher_path = Path(resolved_path)
    candidates = [launcher_path]
    package_root = launcher_path.parent / 'node_modules' / '@openai' / 'codex'
    if package_root.is_dir():
        candidates.extend((package_root / 'package.json', package_root / 'bin' / 'codex.js'))
        platform_packages = package_root / 'node_modules' / '@openai'
        if platform_packages.is_dir():
            for package in sorted(platform_packages.glob('codex-*')):
                try:
                    candidates.extend(sorted(package.glob('vendor/*/bin/codex.exe')))
                except OSError:
                    continue
    digest = hashlib.sha256()
    observed = False
    for path in candidates:
        try:
            if not path.is_file():
                continue
            resolved = path.resolve()
            with path.open('rb') as handle:
                digest.update(str(resolved).encode('utf-8', errors='replace'))
                digest.update(b'\0')
                for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                    digest.update(chunk)
                digest.update(b'\0')
        except OSError:
            continue
        observed = True
    return digest.hexdigest() if observed else None


class SetupManager:
    """Small persistent setup/readiness authority for desktop and browser UI."""

    def __init__(self, *, config_store: LocalConfigStore | None = None, project_root: Path | str | None = None):
        self.config_store = config_store or LocalConfigStore()
        self.project_root = Path(project_root or Path.cwd())
        self._snapshot_lock = threading.RLock()
        self._snapshot_cache: tuple[float, tuple[Any, ...], dict[str, Any]] | None = None
        self._snapshot_ttl = 5.0
        self._embedded_chat_available = False

    def _config_revision(self) -> tuple[Any, ...]:
        try:
            stat = self.config_store.path.stat()
            return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size
        except OSError:
            return (None,)

    def snapshot(self) -> dict[str, Any]:
        with self._snapshot_lock:
            now = time.monotonic()
            revision = self._config_revision()
            if self._snapshot_cache and now - self._snapshot_cache[0] < self._snapshot_ttl and self._snapshot_cache[1] == revision:
                return self._snapshot_cache[2]
            snapshot = self._snapshot_uncached()
            self._snapshot_cache = (time.monotonic(), self._config_revision(), snapshot)
            return snapshot

    def _invalidate_snapshot(self) -> None:
        with self._snapshot_lock:
            self._snapshot_cache = None

    def invalidate(self) -> None:
        self._invalidate_snapshot()

    def set_embedded_chat_available(self, available: bool) -> None:
        with self._snapshot_lock:
            self._embedded_chat_available = bool(available)
            self._snapshot_cache = None

    def _snapshot_uncached(self) -> dict[str, Any]:
        config = self.config_store
        selected_surface = config.get_default_surface()
        network_policy = config.get_network_policy()
        network_error = None
        try:
            resolution = resolve_cfr_proxy(config_store=config)
        except ValueError as exc:
            network_error = str(exc)
            resolution = None
        chat = chat_setup_snapshot(
            browser_backend=config.get_chat_browser_backend(),
            embedded_available=self._embedded_chat_available,
        )
        chrome_use = chrome_use_setup_snapshot(self.project_root)
        chat['chrome_use'] = chrome_use
        if chat.get('effective_backend') == 'shared' and os.name != 'nt':
            chrome_use_ready = bool(
                chrome_use.get('installed')
                and chrome_use.get('host_installed')
                and chrome_use.get('host_healthy')
                and chrome_use.get('relay_up')
            )
            chat['ready'] = bool(chat.get('authenticated') and chrome_use_ready)
            if isinstance(chat.get('shared'), dict):
                chat['shared']['ready'] = chat['ready']
        desktop_runtime = codex_desktop_runtime_snapshot()

        launcher = CodexLauncher()
        codex_path = None
        codex_version = None
        codex_runtime_revision = None
        login = {'LoginStatus': 'NOT_AVAILABLE', 'AuthMode': 'UNKNOWN'}
        try:
            resolved = launcher.resolve()
            codex_path = str(resolved.path)
            codex_version = _codex_version(launcher)
            codex_runtime_revision = _codex_runtime_revision(resolved.path)
            login = login_status(launcher)
        except Exception:
            pass
        validation = config.get_codex_validation()
        codex_logged_in = login.get('LoginStatus') == 'LOGGED_IN'
        codex_validated = bool(
            codex_version
            and validation.get('schema') == SETUP_SCHEMA_VERSION
            and validation.get('codex_version') == codex_version
            and validation.get('schema_fingerprint')
            and validation.get('runtime_revision') == codex_runtime_revision
        )

        app_id = config.get_app_id()
        secret_store = KeyringSecretStore()
        secret_configured = False
        if secret_store.available:
            try:
                secret_configured = secret_store.has_secret('feishu.app_secret')
            except Exception:
                secret_configured = False
        open_ids = config.get_allowed_open_ids()
        roots = config.get_allowed_workspace_roots()
        invalid_roots = [root for root in roots if not Path(root).expanduser().is_dir()]
        sdk = channel_sdk_metadata()

        checks = [
            _check('codex_installed', 'Codex 已安装', required=True, ready=bool(codex_path), state='ready' if codex_path else 'missing', detail=codex_path or '未找到 codex 可执行文件。', action='install_codex' if not codex_path else None),
            _check('codex_login', 'Codex 已登录', required=True, ready=codex_logged_in, state=str(login.get('LoginStatus') or 'UNKNOWN').lower(), detail=f"Auth: {login.get('AuthMode') or 'UNKNOWN'}", action='codex_login' if codex_path and not codex_logged_in else None),
            _check('codex_validation', 'Codex 兼容性已验证', required=True, ready=codex_validated, state='ready' if codex_validated else 'required', detail='当前 Codex 版本已通过 CFR doctor 校验。' if codex_validated else '首次安装或 Codex 版本变化后需要运行一次兼容性校验。', action='validate_codex' if codex_logged_in and not codex_validated else None),
            _check('feishu_sdk', '飞书运行依赖', required=True, ready=bool(sdk.get('installed')), state='ready' if sdk.get('installed') else 'missing', detail=f"lark-channel-sdk {sdk.get('version')}" if sdk.get('installed') else '缺少 lark-channel-sdk。'),
            _check('feishu_credentials', '飞书 App 凭据', required=True, ready=bool(app_id and secret_configured), state='ready' if app_id and secret_configured else 'missing', detail='App ID 与 App Secret 已配置。' if app_id and secret_configured else '需要配置自己的飞书 App ID / App Secret。', action='configure_feishu'),
            _check('feishu_operator', '飞书账号已绑定', required=True, ready=bool(open_ids), state='ready' if open_ids else 'missing', detail=f'{len(open_ids)} 个授权账号。' if open_ids else '需要完成一次本机配对。', action='pair_feishu'),
            _check('workspace_roots', '允许的工作区', required=True, ready=bool(roots) and not invalid_roots, state='ready' if roots and not invalid_roots else 'invalid' if invalid_roots else 'missing', detail=f'{len(roots)} 个有效工作区。' if roots and not invalid_roots else ('存在失效路径：' + ', '.join(invalid_roots) if invalid_roots else '至少添加一个允许的工作区。'), action='configure_workspace'),
            _check(
                'chat_surface',
                'Chat Surface',
                required=selected_surface == 'chat',
                ready=bool(chat.get('ready')),
                state='ready' if chat.get('ready') else 'login_required',
                detail=(
                    f"Chat 使用 {chat.get('effective_backend')} 后端，登录状态已确认；原有专用 Chrome Profile 保留。"
                    if chat.get('ready')
                    else '请完成当前 Chat 浏览器后端的登录。切换到内置浏览器不会删除原有 Chrome 登录。'
                ),
                action='chat_login' if selected_surface == 'chat' and not chat.get('ready') else None,
            ),
            _check(
                'network', '网络路由', required=True, ready=resolution is not None,
                state=resolution.mode if resolution is not None else 'invalid',
                detail=f"{network_policy['mode']} → {resolution.mode} ({resolution.source})" if resolution is not None else f'网络配置无效：{network_error}',
                action='configure_network' if resolution is None else None,
            ),
        ]
        blocking = [item['key'] for item in checks if item['required'] and not item['ready']]
        return {
            'schema': SETUP_SCHEMA_VERSION,
            'ready': not blocking,
            'first_run': not config.path.exists(),
            'blocking': blocking,
            'selected_surface': selected_surface,
            'network': {
                'mode': network_policy['mode'],
                'proxy_url': sanitized_proxy_url(network_policy.get('proxy_url')),
                'effective_mode': resolution.mode if resolution is not None else 'invalid',
                'effective_source': resolution.source if resolution is not None else 'invalid',
                'effective_proxy': sanitized_proxy_url(resolution.selected_proxy) if resolution is not None else None,
            },
            'codex': {
                'executable': codex_path,
                'version': codex_version,
                'login_status': login.get('LoginStatus'),
                'auth_mode': login.get('AuthMode'),
                'validated': codex_validated,
                'validated_at': validation.get('validated_at'),
                'schema_fingerprint': validation.get('schema_fingerprint') if codex_validated else None,
                'runtime_revision': codex_runtime_revision,
                'desktop_launcher_preference': config.get_codex_desktop_launcher(),
                'desktop_runtime_mode': desktop_runtime.get('mode'),
                'desktop_running': desktop_runtime.get('running'),
                'codexhost_available': desktop_runtime.get('codexhost_available'),
                'codexhost_command': desktop_runtime.get('codexhost_command'),
                'codexhost_version': desktop_runtime.get('codexhost_version'),
            },
            'chat': chat,
            'feishu': {
                'app_id_configured': bool(app_id),
                'app_secret_configured': secret_configured,
                'operator_count': len(open_ids),
                'workspace_roots': list(roots),
                'invalid_workspace_roots': invalid_roots,
                'sdk_installed': bool(sdk.get('installed')),
                'sdk_version': sdk.get('version'),
            },
            'checks': checks,
        }

    def save_preferences(
        self,
        *,
        default_surface: str,
        network_mode: str,
        proxy_url: str | None = None,
        chat_browser_backend: str | None = None,
        codex_desktop_launcher: str | None = None,
    ) -> dict[str, Any]:
        if default_surface not in {'code', 'chat'}:
            raise StructuredError('CONTROL_SETUP_SURFACE_INVALID', 'Default Surface must be code or chat')
        if network_mode == 'proxy':
            proxy_url = validate_proxy_url(proxy_url)
        self.config_store.set_setup_preferences(
            default_surface,
            network_mode,
            proxy_url,
            chat_browser_backend=chat_browser_backend,
            codex_desktop_launcher=codex_desktop_launcher,
        )
        self._invalidate_snapshot()
        return {
            'selected_surface': self.config_store.get_default_surface(),
            'network': self.config_store.get_network_policy(),
            'chat_browser_backend': self.config_store.get_chat_browser_backend(),
            'codex_desktop_launcher': self.config_store.get_codex_desktop_launcher(),
        }

    def save_feishu_credentials(self, *, app_id: str, app_secret: str) -> dict[str, Any]:
        set_persistent_credentials(app_id, app_secret, self.config_store)
        self._invalidate_snapshot()
        return {'app_id_configured': True, 'app_secret_configured': True}

    def mark_codex_validated(self, codex_version: str, *, schema_fingerprint: str, runtime_revision: str | None = None) -> dict[str, Any]:
        if not str(schema_fingerprint or '').strip():
            raise StructuredError('CFR_CODEX_VALIDATION_INVALID', 'Codex validation requires a schema fingerprint')
        if runtime_revision is None:
            try:
                runtime_revision = _codex_runtime_revision(CodexLauncher().resolve().path)
            except Exception:
                runtime_revision = None
        if not runtime_revision:
            raise StructuredError('CFR_CODEX_VALIDATION_INVALID', 'Codex validation requires an installed runtime revision')
        self.config_store.set_codex_validation(
            codex_version,
            schema=SETUP_SCHEMA_VERSION,
            schema_fingerprint=schema_fingerprint,
            runtime_revision=runtime_revision,
        )
        self._invalidate_snapshot()
        return self.config_store.get_codex_validation()

    def launch_codex_login(self) -> None:
        launcher = CodexLauncher()
        command = launcher.build_command('login')
        kwargs: dict[str, Any] = {}
        if os.name == 'nt':
            kwargs['creationflags'] = subprocess.CREATE_NEW_CONSOLE
        subprocess.Popen(command, **kwargs)
