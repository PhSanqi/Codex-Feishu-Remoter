"""Persistent, redacted Feishu credential resolution.

App IDs are stored in the CFR configuration file. App Secrets are stored only
through the platform keyring; this module deliberately has no plaintext
secret-file fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import copy
import json
import os
from pathlib import Path
import platform
import tempfile
import threading
from typing import ClassVar, Mapping, Protocol

from cfr.core.models import StructuredError


APP_ID_ENV = 'CFR_FEISHU_APP_ID'
APP_SECRET_ENV = 'CFR_FEISHU_APP_SECRET'
KEYRING_SERVICE = 'CFR'
KEYRING_SECRET_KEY = 'feishu.app_secret'


def resolve_cfr_config_dir() -> Path:
    """Return the platform-native CFR configuration directory."""
    system = platform.system()
    if system == 'Windows':
        base = os.environ.get('LOCALAPPDATA')
        return Path(base) / 'CFR' if base else Path.home() / 'AppData' / 'Local' / 'CFR'
    if system == 'Darwin':
        return Path.home() / 'Library' / 'Application Support' / 'CFR'
    base = os.environ.get('XDG_CONFIG_HOME')
    return (Path(base) if base else Path.home() / '.config') / 'cfr'


class LocalSecretStore(Protocol):
    """The minimal secret-store contract used by the resolver and CLI."""

    @property
    def available(self) -> bool: ...

    def set_secret(self, key: str, value: str) -> None: ...
    def get_secret(self, key: str) -> str | None: ...
    def delete_secret(self, key: str) -> None: ...
    def has_secret(self, key: str) -> bool: ...


class KeyringSecretStore:
    """OS keyring backend. Import is lazy so core/contract tests stay offline."""

    def __init__(self, service: str = KEYRING_SERVICE):
        self.service = service
        self._keyring = None
        self._error: Exception | None = None
        try:
            import keyring
            self._keyring = keyring
            # get_keyring may raise when no backend is configured.
            backend = keyring.get_keyring()
            if backend.__class__.__module__.startswith('keyring.backends.fail'):
                self._error = RuntimeError('keyring backend unavailable')
        except Exception as exc:  # pragma: no cover - backend varies by host
            self._error = exc

    @property
    def available(self) -> bool:
        return self._keyring is not None and self._error is None

    @property
    def error(self) -> Exception | None:
        return self._error

    def _require(self):
        if not self.available:
            raise StructuredError(
                'CFR_SECURE_SECRET_STORE_UNAVAILABLE',
                'The platform secure secret store is unavailable; no plaintext fallback is allowed',
            )
        return self._keyring

    def set_secret(self, key: str, value: str) -> None:
        self._require().set_password(self.service, key, value)

    def get_secret(self, key: str) -> str | None:
        return self._require().get_password(self.service, key)

    def delete_secret(self, key: str) -> None:
        keyring = self._require()
        try:
            keyring.delete_password(self.service, key)
        except Exception as exc:
            # keyring uses different exception classes across backends for a
            # missing item; deleting an already absent secret is idempotent.
            if type(exc).__name__ != 'PasswordDeleteError':
                raise

    def has_secret(self, key: str) -> bool:
        return bool(self.get_secret(key))


class LocalConfigStore:
    """Non-secret CFR JSON configuration with atomic replacement writes."""

    _locks_guard: ClassVar[threading.Lock] = threading.Lock()
    _locks: ClassVar[dict[str, threading.RLock]] = {}

    def __init__(self, config_dir: Path | str | None = None):
        self.config_dir = Path(config_dir) if config_dir is not None else resolve_cfr_config_dir()
        self.path = self.config_dir / 'config.json'
        key = str(self.path.resolve())
        with self._locks_guard:
            self._lock = self._locks.setdefault(key, threading.RLock())
        self._cache_revision = None
        self._cache_value = None

    def _revision(self):
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size

    def load(self) -> dict:
        with self._lock:
            revision = self._revision()
            if self._cache_value is not None and revision == self._cache_revision:
                return copy.deepcopy(self._cache_value)
            if revision is None:
                value = {'version': 1}
            else:
                try:
                    value = json.loads(self.path.read_text(encoding='utf-8'))
                except (OSError, ValueError) as exc:
                    raise StructuredError('CFR_CONFIG_INVALID', 'CFR config.json could not be read') from exc
                if not isinstance(value, dict):
                    raise StructuredError('CFR_CONFIG_INVALID', 'CFR config.json must contain an object')
                # Refuse accidental secret material in the non-secret store while
                # allowing the non-secret ``app_secret_updated_at`` metadata field.
                def forbidden_keys(item):
                    if isinstance(item, dict):
                        for key, child in item.items():
                            if str(key).lower() in {'app_secret', 'tenant_access_token', 'user_access_token', 'authorization'}:
                                return True
                            if forbidden_keys(child):
                                return True
                    elif isinstance(item, list):
                        return any(forbidden_keys(child) for child in item)
                    return False
                if forbidden_keys(value):
                    raise StructuredError('CFR_PLAINTEXT_SECRET_DETECTED', 'CFR config.json contains forbidden secret material')
            self._cache_revision = revision
            self._cache_value = copy.deepcopy(value)
            return copy.deepcopy(value)

    def _write(self, value: dict) -> None:
        with self._lock:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=self.config_dir, prefix='.config.', suffix='.tmp', delete=False)
            temp_path = Path(handle.name)
            try:
                json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write('\n')
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass
                handle.close()
                os.replace(temp_path, self.path)
                self._cache_revision = self._revision()
                self._cache_value = copy.deepcopy(value)
            finally:
                if not handle.closed:
                    handle.close()
                if temp_path.exists():
                    try:
                        temp_path.unlink()
                    except OSError:
                        # The atomic destination is authoritative. Antivirus
                        # or indexing must not turn best-effort temp cleanup
                        # into a false configuration write failure.
                        pass

    def get_app_id(self) -> str | None:
        return (((self.load().get('feishu') or {}).get('app_id')) or None)

    def get_app_secret_updated_at(self) -> str | None:
        return (((self.load().get('feishu') or {}).get('app_secret_updated_at')) or None)

    def get_allowed_open_ids(self) -> tuple[str, ...]:
        values = ((self.load().get('feishu') or {}).get('allowed_open_ids') or ())
        return tuple(str(value).strip() for value in values if str(value).strip())

    def get_allowed_workspace_roots(self) -> tuple[str, ...]:
        values = ((self.load().get('feishu') or {}).get('allowed_workspace_roots') or ())
        return tuple(str(value).strip() for value in values if str(value).strip())

    def get_default_surface(self) -> str:
        value = str((self.load().get('runtime') or {}).get('default_surface') or 'code').strip().lower()
        return value if value in {'code', 'chat'} else 'code'

    def set_default_surface(self, surface: str) -> None:
        value = str(surface or '').strip().lower()
        if value not in {'code', 'chat'}:
            raise StructuredError('CFR_DEFAULT_SURFACE_INVALID', 'Default Surface must be code or chat')
        with self._lock:
            config = self.load()
            runtime = dict(config.get('runtime') or {})
            runtime['default_surface'] = value
            runtime['updated_at'] = datetime.now(timezone.utc).isoformat()
            config['version'] = 1
            config['runtime'] = runtime
            self._write(config)

    def get_chat_browser_backend(self) -> str:
        """Return the preferred Chat browser without invalidating legacy login state."""
        value = str((self.load().get('runtime') or {}).get('chat_browser_backend') or 'auto').strip().lower()
        return value if value in {'auto', 'embedded', 'dedicated', 'shared'} else 'auto'

    def set_chat_browser_backend(self, backend: str) -> None:
        value = str(backend or '').strip().lower()
        if value not in {'auto', 'embedded', 'dedicated', 'shared'}:
            raise StructuredError(
                'CFR_CHAT_BROWSER_BACKEND_INVALID',
                'Chat browser backend must be auto, embedded, dedicated, or shared',
            )
        with self._lock:
            config = self.load()
            runtime = dict(config.get('runtime') or {})
            runtime['chat_browser_backend'] = value
            runtime['updated_at'] = datetime.now(timezone.utc).isoformat()
            config['version'] = 1
            config['runtime'] = runtime
            self._write(config)

    def get_codex_desktop_launcher(self) -> str:
        """Return how CFR should restore Codex Desktop when a restart is requested."""
        value = str((self.load().get('runtime') or {}).get('codex_desktop_launcher') or 'auto').strip().lower()
        return value if value in {'auto', 'codexhost', 'stock'} else 'auto'

    def set_codex_desktop_launcher(self, launcher: str) -> None:
        value = str(launcher or '').strip().lower()
        if value not in {'auto', 'codexhost', 'stock'}:
            raise StructuredError(
                'CFR_CODEX_DESKTOP_LAUNCHER_INVALID',
                'Codex Desktop launcher must be auto, codexhost, or stock',
            )
        with self._lock:
            config = self.load()
            runtime = dict(config.get('runtime') or {})
            runtime['codex_desktop_launcher'] = value
            runtime['updated_at'] = datetime.now(timezone.utc).isoformat()
            config['version'] = 1
            config['runtime'] = runtime
            self._write(config)

    def get_network_policy(self) -> dict:
        value = dict(self.load().get('network') or {})
        mode = str(value.get('mode') or 'auto').strip().lower()
        if mode not in {'auto', 'direct', 'proxy'}:
            mode = 'auto'
        proxy_url = str(value.get('proxy_url') or '').strip() or None
        return {'mode': mode, 'proxy_url': proxy_url}

    def set_network_policy(self, mode: str, proxy_url: str | None = None) -> None:
        value = str(mode or '').strip().lower()
        if value not in {'auto', 'direct', 'proxy'}:
            raise StructuredError('CFR_NETWORK_MODE_INVALID', 'Network mode must be auto, direct, or proxy')
        with self._lock:
            config = self.load()
            network = dict(config.get('network') or {})
            network['mode'] = value
            if value == 'proxy':
                proxy = str(proxy_url or '').strip()
                if not proxy:
                    raise StructuredError('CFR_NETWORK_PROXY_REQUIRED', 'Proxy mode requires a proxy URL')
                network['proxy_url'] = proxy
            else:
                network.pop('proxy_url', None)
            network['updated_at'] = datetime.now(timezone.utc).isoformat()
            config['version'] = 1
            config['network'] = network
            self._write(config)

    def set_setup_preferences(
        self,
        surface: str,
        mode: str,
        proxy_url: str | None = None,
        *,
        chat_browser_backend: str | None = None,
        codex_desktop_launcher: str | None = None,
    ) -> None:
        """Persist desktop, browser, launcher and network choices atomically."""
        surface_value = str(surface or '').strip().lower()
        mode_value = str(mode or '').strip().lower()
        if surface_value not in {'code', 'chat'}:
            raise StructuredError('CFR_DEFAULT_SURFACE_INVALID', 'Default Surface must be code or chat')
        if mode_value not in {'auto', 'direct', 'proxy'}:
            raise StructuredError('CFR_NETWORK_MODE_INVALID', 'Network mode must be auto, direct, or proxy')
        proxy_value = str(proxy_url or '').strip() or None
        if mode_value == 'proxy' and not proxy_value:
            raise StructuredError('CFR_NETWORK_PROXY_REQUIRED', 'Proxy mode requires a proxy URL')
        browser_value = self.get_chat_browser_backend() if chat_browser_backend is None else str(chat_browser_backend).strip().lower()
        launcher_value = self.get_codex_desktop_launcher() if codex_desktop_launcher is None else str(codex_desktop_launcher).strip().lower()
        if browser_value not in {'auto', 'embedded', 'dedicated', 'shared'}:
            raise StructuredError('CFR_CHAT_BROWSER_BACKEND_INVALID', 'Chat browser backend must be auto, embedded, dedicated, or shared')
        if launcher_value not in {'auto', 'codexhost', 'stock'}:
            raise StructuredError('CFR_CODEX_DESKTOP_LAUNCHER_INVALID', 'Codex Desktop launcher must be auto, codexhost, or stock')
        with self._lock:
            config = self.load()
            updated_at = datetime.now(timezone.utc).isoformat()
            runtime = dict(config.get('runtime') or {})
            runtime.update({
                'default_surface': surface_value,
                'chat_browser_backend': browser_value,
                'codex_desktop_launcher': launcher_value,
                'updated_at': updated_at,
            })
            network = dict(config.get('network') or {})
            network.update({'mode': mode_value, 'updated_at': updated_at})
            if mode_value == 'proxy':
                network['proxy_url'] = proxy_value
            else:
                network.pop('proxy_url', None)
            config.update({'version': 1, 'runtime': runtime, 'network': network})
            self._write(config)

    def get_codex_validation(self) -> dict:
        value = dict((self.load().get('setup') or {}).get('codex_validation') or {})
        return {
            'schema': int(value.get('schema') or 0),
            'codex_version': str(value.get('codex_version') or '').strip() or None,
            'schema_fingerprint': str(value.get('schema_fingerprint') or '').strip() or None,
            'runtime_revision': str(value.get('runtime_revision') or '').strip() or None,
            'validated_at': str(value.get('validated_at') or '').strip() or None,
        }

    def set_codex_validation(
        self,
        codex_version: str,
        *,
        schema: int = 1,
        schema_fingerprint: str | None = None,
        runtime_revision: str | None = None,
    ) -> None:
        version = str(codex_version or '').strip()
        if not version:
            raise StructuredError('CFR_CODEX_VALIDATION_INVALID', 'Codex validation requires a version')
        fingerprint = str(schema_fingerprint or '').strip() or None
        revision = str(runtime_revision or '').strip() or None
        with self._lock:
            config = self.load()
            setup = dict(config.get('setup') or {})
            setup['codex_validation'] = {
                'schema': int(schema),
                'codex_version': version,
                'schema_fingerprint': fingerprint,
                'runtime_revision': revision,
                'validated_at': datetime.now(timezone.utc).isoformat(),
            }
            config['version'] = 1
            config['setup'] = setup
            self._write(config)

    def set_feishu_security(self, allowed_open_ids, allowed_workspace_roots) -> None:
        """Atomically persist the non-secret Feishu execution policy."""
        with self._lock:
            value = self.load()
            feishu = dict(value.get('feishu') or {})
            feishu['allowed_open_ids'] = list(dict.fromkeys(
                str(item).strip() for item in allowed_open_ids if str(item).strip()
            ))
            feishu['allowed_workspace_roots'] = list(dict.fromkeys(
                str(item).strip() for item in allowed_workspace_roots if str(item).strip()
            ))
            feishu['updated_at'] = datetime.now(timezone.utc).isoformat()
            value['version'] = 1
            value['feishu'] = feishu
            self._write(value)

    def set_app_id(self, app_id: str) -> None:
        if not app_id or not app_id.strip():
            raise StructuredError('CFR_FEISHU_APP_ID_INVALID', 'Feishu App ID must be non-empty')
        with self._lock:
            value = self.load()
            feishu = dict(value.get('feishu') or {})
            feishu['app_id'] = app_id.strip()
            feishu['updated_at'] = datetime.now(timezone.utc).isoformat()
            value['version'] = 1
            value['feishu'] = feishu
            self._write(value)

    def set_app_secret_updated_at(self, updated_at: str | None = None) -> None:
        with self._lock:
            value = self.load()
            feishu = dict(value.get('feishu') or {})
            feishu['app_secret_updated_at'] = updated_at or datetime.now(timezone.utc).isoformat()
            value['version'] = 1
            value['feishu'] = feishu
            self._write(value)


@dataclass(frozen=True)
class ResolvedFeishuCredentials:
    app_id: str | None
    app_secret: str | None
    app_id_source: str
    app_secret_source: str
    persistent_store_available: bool
    app_secret_updated_at: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.app_secret)

    def safe_dict(self) -> dict:
        return {
            'AppIdConfigured': bool(self.app_id),
            'AppIdSource': self.app_id_source,
            'AppSecretConfigured': bool(self.app_secret),
            'AppSecretSource': self.app_secret_source,
            'PersistentStoreAvailable': self.persistent_store_available,
            'AppSecretUpdatedAt': self.app_secret_updated_at,
        }


class FeishuCredentialResolver:
    """Resolve environment overrides, then persistent local credentials."""

    def __init__(self, environment: Mapping[str, str] | None = None, config_store: LocalConfigStore | None = None, secret_store: LocalSecretStore | None = None):
        self.environment = os.environ if environment is None else environment
        self.config_store = config_store or LocalConfigStore()
        self.secret_store = secret_store or KeyringSecretStore()

    def resolve(self) -> ResolvedFeishuCredentials:
        env_app_id = (self.environment.get(APP_ID_ENV) or '').strip()
        env_secret = (self.environment.get(APP_SECRET_ENV) or '').strip()
        persistent_app_id = self.config_store.get_app_id()
        secret = None
        store_available = bool(getattr(self.secret_store, 'available', False))
        if store_available:
            try:
                secret = self.secret_store.get_secret(KEYRING_SECRET_KEY)
            except StructuredError as exc:
                if exc.code != 'CFR_SECURE_SECRET_STORE_UNAVAILABLE':
                    raise
                store_available = False
        app_id = env_app_id or persistent_app_id
        app_secret = env_secret or secret
        return ResolvedFeishuCredentials(
            app_id=app_id or None,
            app_secret=app_secret or None,
            app_id_source='environment' if env_app_id else 'persistent' if persistent_app_id else 'missing',
            app_secret_source='environment' if env_secret else 'persistent' if secret else 'missing',
            persistent_store_available=store_available,
            app_secret_updated_at=self.config_store.get_app_secret_updated_at(),
        )

    def safe_status(self) -> dict:
        return self.resolve().safe_dict()


def _require_nonempty(value: str | None, code: str, label: str) -> str:
    if not value or not value.strip():
        raise StructuredError(code, f'{label} must be non-empty')
    return value.strip()


def import_environment_credentials(environment: Mapping[str, str] | None = None, config_store: LocalConfigStore | None = None, secret_store: LocalSecretStore | None = None) -> dict:
    env = os.environ if environment is None else environment
    app_id = _require_nonempty(env.get(APP_ID_ENV), 'CFR_FEISHU_APP_ID_REQUIRED', APP_ID_ENV)
    app_secret = _require_nonempty(env.get(APP_SECRET_ENV), 'CFR_FEISHU_APP_SECRET_REQUIRED', APP_SECRET_ENV)
    return set_persistent_credentials(app_id, app_secret, config_store, secret_store)


def set_persistent_credentials(
    app_id: str,
    app_secret: str,
    config_store: LocalConfigStore | None = None,
    secret_store: LocalSecretStore | None = None,
) -> dict:
    """Commit App ID plus keyring secret with rollback across both stores."""
    app_id = _require_nonempty(app_id, 'CFR_FEISHU_APP_ID_INVALID', 'Feishu App ID')
    app_secret = _require_nonempty(app_secret, 'CFR_FEISHU_APP_SECRET_INVALID', 'Feishu App Secret')
    config = config_store or LocalConfigStore()
    store = secret_store or KeyringSecretStore()
    previous_config = config.load()
    previous_secret = store.get_secret(KEYRING_SECRET_KEY)
    value = copy.deepcopy(previous_config)
    feishu = dict(value.get('feishu') or {})
    updated_at = datetime.now(timezone.utc).isoformat()
    feishu.update({
        'app_id': app_id,
        'app_secret_updated_at': updated_at,
        'updated_at': updated_at,
    })
    value.update({'version': 1, 'feishu': feishu})
    try:
        # Set the secret first, then commit non-secret metadata. Both stores
        # are restored if the local config transaction fails.
        store.set_secret(KEYRING_SECRET_KEY, app_secret)
        config._write(value)
    except Exception as exc:
        rollback_errors = []
        try:
            config._write(previous_config)
        except Exception as rollback_exc:
            rollback_errors.append(rollback_exc)
        try:
            if previous_secret is None:
                store.delete_secret(KEYRING_SECRET_KEY)
            else:
                store.set_secret(KEYRING_SECRET_KEY, previous_secret)
        except Exception as rollback_exc:
            rollback_errors.append(rollback_exc)
        if rollback_errors:
            raise StructuredError(
                'CFR_CREDENTIAL_PARTIAL_COMMIT_RISK',
                'Credential update failed and rollback was not fully confirmed',
            ) from exc
        raise
    resolved = FeishuCredentialResolver(environment={}, config_store=config, secret_store=store).resolve()
    return resolved.safe_dict()


def set_persistent_app_id(app_id: str, config_store: LocalConfigStore | None = None) -> dict:
    config = config_store or LocalConfigStore()
    config.set_app_id(_require_nonempty(app_id, 'CFR_FEISHU_APP_ID_INVALID', 'Feishu App ID'))
    return FeishuCredentialResolver(environment={}, config_store=config, secret_store=KeyringSecretStore()).safe_status()


def set_persistent_secret(secret: str, config_store: LocalConfigStore | None = None, secret_store: LocalSecretStore | None = None) -> dict:
    value = _require_nonempty(secret, 'CFR_FEISHU_APP_SECRET_INVALID', 'Feishu App Secret')
    config = config_store or LocalConfigStore()
    store = secret_store or KeyringSecretStore()
    previous_secret = store.get_secret(KEYRING_SECRET_KEY)
    try:
        store.set_secret(KEYRING_SECRET_KEY, value)
        config.set_app_secret_updated_at()
    except Exception:
        try:
            if previous_secret is None:
                store.delete_secret(KEYRING_SECRET_KEY)
            else:
                store.set_secret(KEYRING_SECRET_KEY, previous_secret)
        except Exception as rollback_exc:
            raise StructuredError(
                'CFR_SECRET_PARTIAL_COMMIT_RISK',
                'Feishu secret update failed and the previous secret could not be restored',
            ) from rollback_exc
        raise
    return FeishuCredentialResolver(environment={}, config_store=config, secret_store=store).safe_status()


def clear_persistent_secret(config_store: LocalConfigStore | None = None, secret_store: LocalSecretStore | None = None) -> dict:
    config = config_store or LocalConfigStore()
    store = secret_store or KeyringSecretStore()
    store.delete_secret(KEYRING_SECRET_KEY)
    return FeishuCredentialResolver(environment={}, config_store=config, secret_store=store).safe_status()


def clear_persistent_all(config_store: LocalConfigStore | None = None, secret_store: LocalSecretStore | None = None) -> dict:
    config = config_store or LocalConfigStore()
    store = secret_store or KeyringSecretStore()
    previous_secret = store.get_secret(KEYRING_SECRET_KEY)
    previous_config = config.load()
    value = copy.deepcopy(previous_config)
    feishu = dict(value.get('feishu') or {})
    feishu.pop('app_id', None)
    feishu.pop('app_secret_updated_at', None)
    if feishu:
        value['feishu'] = feishu
    else:
        value.pop('feishu', None)
    try:
        store.delete_secret(KEYRING_SECRET_KEY)
        config._write(value)
    except Exception as exc:
        rollback_errors = []
        try:
            config._write(previous_config)
        except Exception as rollback_exc:
            rollback_errors.append(rollback_exc)
        try:
            if previous_secret is not None:
                store.set_secret(KEYRING_SECRET_KEY, previous_secret)
        except Exception as rollback_exc:
            rollback_errors.append(rollback_exc)
        if rollback_errors:
            raise StructuredError(
                'CFR_CREDENTIAL_CLEAR_PARTIAL_COMMIT_RISK',
                'Credential clear failed and rollback was not fully confirmed',
            ) from exc
        raise
    return FeishuCredentialResolver(environment={}, config_store=config, secret_store=store).safe_status()
