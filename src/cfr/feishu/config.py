from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from typing import Mapping

from cfr.core.models import StructuredError

from .credentials import FeishuCredentialResolver, LocalConfigStore


MAX_WORKER_CONCURRENCY = 32
MAX_APPROVAL_TIMEOUT_SECONDS = 24 * 60 * 60


def _split(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.replace(';', ',').split(',') if item.strip())


@dataclass(frozen=True, repr=False)
class FeishuSettings:
    app_id: str | None
    app_secret: str | None
    allowed_open_ids: tuple[str, ...]
    allowed_workspace_roots: tuple[Path, ...]
    enable_group_chats: bool = False
    approval_timeout_seconds: int = 180
    worker_concurrency: int = 4
    database: Path = Path('cfr.sqlite3')
    verbose: bool = False
    sdk_log_level: str = 'WARNING'
    app_id_source: str = 'unknown'
    app_secret_source: str = 'unknown'
    persistent_store_available: bool = False
    app_secret_updated_at: str | None = None
    operator_policy_source: str = 'missing'
    workspace_policy_source: str = 'missing'
    default_surface: str = 'code'

    @property
    def app_namespace(self) -> str:
        return hashlib.sha256((self.app_id or 'missing').encode()).hexdigest()[:16]

    @property
    def credentials_present(self) -> bool:
        return bool(self.app_id and self.app_secret)

    def __repr__(self):
        return (
            'FeishuSettings('
            f'app_id={_redact_identifier(self.app_id)!r}, '
            f'app_secret={"<REDACTED>" if self.app_secret else None!r}, '
            f'allowed_open_ids={len(self.allowed_open_ids)}, '
            f'allowed_workspace_roots={tuple(str(path) for path in self.allowed_workspace_roots)!r}, '
            f'enable_group_chats={self.enable_group_chats}, '
            f'approval_timeout_seconds={self.approval_timeout_seconds}, '
            f'worker_concurrency={self.worker_concurrency}, '
            f'sdk_log_level={self.sdk_log_level!r}, '
            f'app_id_source={self.app_id_source!r}, app_secret_source={self.app_secret_source!r})'
        )

    def validate_execution(self):
        self.validate_connection()
        if not self.allowed_open_ids:
            raise StructuredError('FEISHU_OPERATOR_ALLOWLIST_REQUIRED', 'Set at least one authorized Feishu open_id')
        if not self.allowed_workspace_roots:
            raise StructuredError('FEISHU_WORKSPACE_ALLOWLIST_REQUIRED', 'Set at least one allowed workspace root')
        if any(not root.exists() or not root.is_dir() for root in self.allowed_workspace_roots):
            raise StructuredError('FEISHU_WORKSPACE_ROOT_INVALID', 'All configured workspace roots must be existing directories')
        if not 0 < self.approval_timeout_seconds <= MAX_APPROVAL_TIMEOUT_SECONDS:
            raise StructuredError('FEISHU_CONFIG_INVALID', 'Approval timeout must be positive and no more than 24 hours')
        if not 1 <= self.worker_concurrency <= MAX_WORKER_CONCURRENCY:
            raise StructuredError('FEISHU_CONFIG_INVALID', f'Worker concurrency must be between 1 and {MAX_WORKER_CONCURRENCY}')

    def validate_connection(self):
        if not self.credentials_present:
            raise StructuredError('FEISHU_CREDENTIALS_REQUIRED', 'Configure Feishu credentials with the environment or cfr feishu credentials')


def _redact_identifier(value: str | None) -> str | None:
    if not value:
        return None
    return value if len(value) <= 8 else f'{value[:4]}****{value[-4:]}'


def load_settings(
    environment: Mapping[str, str] | None = None,
    database='cfr.sqlite3',
    verbose=False,
    config_store: LocalConfigStore | None = None,
) -> FeishuSettings:
    env = os.environ if environment is None else environment
    config = config_store or LocalConfigStore()
    credentials = FeishuCredentialResolver(environment=env, config_store=config).resolve()
    persistent_open_ids = config.get_allowed_open_ids()
    persistent_roots = config.get_allowed_workspace_roots()
    env_open_ids = _split(env.get('CFR_FEISHU_ALLOWED_OPEN_IDS'))
    env_roots = _split(env.get('CFR_FEISHU_ALLOWED_WORKSPACE_ROOTS'))
    roots = tuple(Path(value).expanduser() for value in (env_roots or persistent_roots))
    try:
        approval_timeout = int(env.get('CFR_FEISHU_APPROVAL_TIMEOUT_SECONDS', '180'))
        concurrency = int(env.get('CFR_FEISHU_WORKER_CONCURRENCY', '4'))
    except ValueError as exc:
        raise StructuredError('FEISHU_CONFIG_INVALID', 'Timeout and worker concurrency must be integers') from exc
    if not 1 <= approval_timeout <= MAX_APPROVAL_TIMEOUT_SECONDS:
        raise StructuredError('FEISHU_CONFIG_INVALID', 'Approval timeout must be between 1 second and 24 hours')
    if not 1 <= concurrency <= MAX_WORKER_CONCURRENCY:
        raise StructuredError('FEISHU_CONFIG_INVALID', f'Worker concurrency must be between 1 and {MAX_WORKER_CONCURRENCY}')
    sdk_log_level = env.get('CFR_FEISHU_SDK_LOG_LEVEL', 'WARNING').strip().upper()
    if sdk_log_level not in {'WARNING', 'ERROR', 'INFO'}:
        raise StructuredError('FEISHU_CONFIG_INVALID', 'CFR_FEISHU_SDK_LOG_LEVEL must be WARNING, ERROR, or INFO')
    return FeishuSettings(
        app_id=credentials.app_id,
        app_secret=credentials.app_secret,
        allowed_open_ids=env_open_ids or persistent_open_ids,
        allowed_workspace_roots=roots,
        enable_group_chats=env.get('CFR_FEISHU_ENABLE_GROUPS', 'false').strip().lower() in {'1', 'true', 'yes', 'on'},
        approval_timeout_seconds=approval_timeout,
        worker_concurrency=concurrency,
        database=Path(database),
        verbose=verbose,
        sdk_log_level=sdk_log_level,
        app_id_source=credentials.app_id_source,
        app_secret_source=credentials.app_secret_source,
        persistent_store_available=credentials.persistent_store_available,
        app_secret_updated_at=credentials.app_secret_updated_at,
        operator_policy_source='environment' if env_open_ids else ('persistent' if persistent_open_ids else 'missing'),
        workspace_policy_source='environment' if env_roots else ('persistent' if persistent_roots else 'missing'),
        default_surface=config.get_default_surface(),
    )
