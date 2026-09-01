"""Persistent, redacted Feishu credential resolution.

App IDs are stored in the CFR configuration file. App Secrets are stored only
through the platform keyring; this module deliberately has no plaintext
secret-file fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import tempfile
from typing import Mapping, Protocol

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

    def __init__(self, config_dir: Path | str | None = None):
        self.config_dir = Path(config_dir) if config_dir is not None else resolve_cfr_config_dir()
        self.path = self.config_dir / 'config.json'

    def load(self) -> dict:
        if not self.path.exists():
            return {'version': 1}
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
        return value

    def _write(self, value: dict) -> None:
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
        finally:
            if not handle.closed:
                handle.close()
            if temp_path.exists():
                temp_path.unlink()

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

    def set_feishu_security(self, allowed_open_ids, allowed_workspace_roots) -> None:
        """Atomically persist the non-secret Feishu execution policy."""
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
        value = self.load()
        feishu = dict(value.get('feishu') or {})
        feishu['app_id'] = app_id.strip()
        feishu['updated_at'] = datetime.now(timezone.utc).isoformat()
        value['version'] = 1
        value['feishu'] = feishu
        self._write(value)

    def set_app_secret_updated_at(self, updated_at: str | None = None) -> None:
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
    config = config_store or LocalConfigStore()
    store = secret_store or KeyringSecretStore()
    previous_config = config.load()
    previous_secret = store.get_secret(KEYRING_SECRET_KEY)
    try:
        # Set the secret first, then commit non-secret metadata. Both stores
        # are restored if the local config transaction fails.
        store.set_secret(KEYRING_SECRET_KEY, app_secret)
        config.set_app_id(app_id)
        config.set_app_secret_updated_at()
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
                'IMPORT_ENV_PARTIAL_COMMIT_RISK',
                'Credential import failed and rollback was not fully confirmed',
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
    store.set_secret(KEYRING_SECRET_KEY, value)
    config.set_app_secret_updated_at()
    return FeishuCredentialResolver(environment={}, config_store=config, secret_store=store).safe_status()


def clear_persistent_secret(config_store: LocalConfigStore | None = None, secret_store: LocalSecretStore | None = None) -> dict:
    config = config_store or LocalConfigStore()
    store = secret_store or KeyringSecretStore()
    store.delete_secret(KEYRING_SECRET_KEY)
    return FeishuCredentialResolver(environment={}, config_store=config, secret_store=store).safe_status()


def clear_persistent_all(config_store: LocalConfigStore | None = None, secret_store: LocalSecretStore | None = None) -> dict:
    config = config_store or LocalConfigStore()
    store = secret_store or KeyringSecretStore()
    store.delete_secret(KEYRING_SECRET_KEY)
    value = config.load()
    feishu = dict(value.get('feishu') or {})
    feishu.pop('app_id', None)
    feishu.pop('app_secret_updated_at', None)
    if feishu:
        value['feishu'] = feishu
    else:
        value.pop('feishu', None)
    config._write(value)
    return FeishuCredentialResolver(environment={}, config_store=config, secret_store=store).safe_status()
