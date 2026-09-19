from __future__ import annotations

from pathlib import Path
import sqlite3

from cfr.codex.diagnostics import gate_metadata, normalize_gate_origin
from cfr.doctor import run_doctor

from .config import load_settings
from .connection import FeishuConnectionLease
from .transport import ChannelFeishuTransport
from .transport import _require_channel_sdk, channel_sdk_metadata
from .sdk_compat import close_idle_channel_sdk_loop, inspect_channel_sdk_loop


def _sdk_status():
    try:
        _require_channel_sdk()
        metadata = channel_sdk_metadata()
        diagnostic = inspect_channel_sdk_loop()
        close_idle_channel_sdk_loop()
        return True, metadata['version'], diagnostic
    except Exception:
        return False, None, None


def run_feishu_doctor(database='cfr.sqlite3', codex_home=None, codex_bin=None, live=False, gate_origin='unknown', timeout=15):
    gate_origin = normalize_gate_origin(gate_origin)
    settings = load_settings(database=database)
    sdk_ready, sdk_version, sdk_loop_diagnostic = _sdk_status()
    lease_key = f'feishu:{settings.app_namespace}'
    lease = None
    schema = 'NOT_PRESENT'
    db_path = Path(database)
    if db_path.exists() and str(database) != ':memory:':
        try:
            connection = sqlite3.connect(f'file:{db_path.resolve().as_posix()}?mode=ro', uri=True)
            try:
                table = connection.execute("select 1 from sqlite_master where type='table' and name='feishu_daemon_lease'").fetchone()
                lease = connection.execute('select * from feishu_daemon_lease where lease_key=?', (lease_key,)).fetchone() if table else None
            finally:
                connection.close()
            schema = 'PASS' if table else 'NOT_PRESENT'
        except Exception:
            schema = 'ERROR'
    result = {
        'Verdict': 'PASS',
        **gate_metadata(gate_origin),
        'FeishuSdkInstalled': sdk_ready,
        'FeishuSdkVersion': sdk_version,
        'TransportBackend': 'lark-channel-sdk',
        'LarkChannelSdkInstalled': sdk_ready,
        'LarkChannelSdkVersion': sdk_version,
        'ChannelSdkLoopCompatibilityMode': getattr(sdk_loop_diagnostic, 'compatibility_mode', 'NOT_RUN'),
        'ChannelSdkLegacyGlobalLoopDetected': bool(getattr(sdk_loop_diagnostic, 'loop_present', False)),
        'ChannelSdkLegacyLoopRunningAtBootstrap': bool(getattr(sdk_loop_diagnostic, 'loop_running', False)),
        'AppIdPresent': bool(settings.app_id),
        'AppSecretPresent': bool(settings.app_secret),
        'AppIdSource': settings.app_id_source,
        'AppSecretSource': settings.app_secret_source,
        'PersistentCredentialStore': 'PASS' if settings.persistent_store_available else 'UNAVAILABLE',
        'PersistentAppIdConfigured': settings.app_id_source == 'persistent',
        'PersistentAppSecretConfigured': settings.app_secret_source == 'persistent',
        'AppSecretConfigured': bool(settings.app_secret),
        'AppSecretUpdatedAt': settings.app_secret_updated_at,
        'OperatorAllowlistCount': len(settings.allowed_open_ids),
        'WorkspaceRootCount': len(settings.allowed_workspace_roots),
        'WorkspaceRootsCanonical': [str(path.resolve()) for path in settings.allowed_workspace_roots if path.exists()],
        'GroupChatEnabled': settings.enable_group_chats,
        'DatabasePath': str(settings.database),
        'DaemonLeaseSchema': schema,
        'CurrentDaemonLease': bool(lease and lease[5] > __import__('time').time()),
        'CodexDoctorSummary': run_doctor(database=database, codex_home=codex_home, codex_bin=codex_bin, live=False, gate_origin=gate_origin),
        'PinnedProtocolSHA': '9f7d498cb510be38baa10422b46860b2a4599898affe2ae56cdc73460f90908b',
        'FeishuConnectionReady': 'NOT_RUN',
        'LiveModelProbe': 'NOT_RUN',
    }
    if not sdk_ready:
        result['Verdict'] = 'WARN'
        result['RuntimeInterpretation'] = 'FEISHU_SDK_INSTALL_REQUIRED'
    if not settings.credentials_present or not settings.allowed_open_ids or not settings.allowed_workspace_roots:
        result['Verdict'] = 'WARN'
        result['RuntimeInterpretation'] = 'FEISHU_LIVE_SETUP_REQUIRED'
    if live and result['Verdict'] == 'PASS':
        connection_lease = None
        transport = ChannelFeishuTransport(settings, connect_timeout=timeout)
        try:
            settings.validate_connection()
            connection_lease = FeishuConnectionLease(settings.database, lease_key).acquire()
            transport.connect_until_ready(timeout=timeout)
            result['FeishuConnectionReady'] = 'PASS'
        except Exception as exc:
            result['FeishuConnectionReady'] = 'FAIL'
            result['FeishuConnectionError'] = getattr(exc, 'code', 'FEISHU_CHANNEL_UNKNOWN')
            result['Verdict'] = 'WARN'
        finally:
            transport.stop()
            if connection_lease is not None:
                connection_lease.release()
    return result
