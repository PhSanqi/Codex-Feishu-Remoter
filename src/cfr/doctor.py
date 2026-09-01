from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile

from .codex.app_server import AppServerClient
from .codex.diagnostics import config_schema_snapshot, gate_metadata, inspect_runtime, normalize_gate_origin, safe_error
from .codex.launcher import CodexLauncher
from .codex.runtime_lease import CfrThreadRuntimeLeaseManager
from .codex.threads import ThreadManager
from .codex.turns import TurnManager
from .config import resolve_cfr_codex_home
from .network import proxy_child_env, resolve_proxy
from .platform import codex_home_permissions, detect_platform_capabilities


PROTOCOL_SHA256 = '9f7d498cb510be38baa10422b46860b2a4599898affe2ae56cdc73460f90908b'


def _hash_file(path):
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _db_readonly_snapshot(path):
    if str(path) == ':memory:':
        return {
            'DatabasePath': ':memory:',
            'Exists': True,
            'DatabasePathSource': 'memory_test_only',
            'LegacyDefaultPath': False,
            'RuntimeLeaseSchema': 'NON_DURABLE_MEMORY',
            'ActiveCfrRuntimeLeases': 0,
        }
    path = Path(path)
    snapshot = {'DatabasePath': str(path), 'Exists': path.exists(), 'DatabasePathSource': 'legacy_default', 'LegacyDefaultPath': str(path) == 'cfr.sqlite3'}
    if not path.exists():
        snapshot['RuntimeLeaseSchema'] = 'NOT_PRESENT'
        snapshot['ActiveCfrRuntimeLeases'] = 0
        return snapshot
    try:
        uri = f'file:{path.resolve().as_posix()}?mode=ro'
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            table = connection.execute("select 1 from sqlite_master where type='table' and name='cfr_thread_runtime_leases'").fetchone()
            rows = connection.execute('select count(*) from cfr_thread_runtime_leases').fetchone()[0] if table else 0
        finally:
            connection.close()
        snapshot['RuntimeLeaseSchema'] = 'PASS' if table else 'NOT_PRESENT'
        snapshot['ActiveCfrRuntimeLeases'] = rows
    except Exception as exc:
        snapshot['RuntimeLeaseSchema'] = 'ERROR'
        snapshot['RuntimeLeaseErrorClass'] = type(exc).__name__
    return snapshot


def _live_probe(project_root, timeout, process_env, codex_home, codex_bin=None):
    with tempfile.TemporaryDirectory(prefix='cfr-doctor-live-') as directory:
        client = AppServerClient(
            launcher=CodexLauncher(executable=codex_bin, environment=process_env),
            timeout=timeout,
            process_env=process_env,
            codex_home=codex_home,
        )
        try:
            client.start()
            started = client.request('thread/start', {'cwd': directory, 'ephemeral': True, 'threadSource': 'user', 'historyMode': 'legacy', 'sessionStartSource': 'startup'})
            thread = ThreadManager.ref_from_result(started, Path(directory))
            turn = TurnManager(client).run_turn(thread.thread_id, 'CFR_DOCTOR_LIVE_001: Do not call tools or modify files. Reply exactly CFR_DOCTOR_LIVE_ACK_001.', timeout)
            return {
                'Status': 'PASS' if turn.status == 'completed' and 'CFR_DOCTOR_LIVE_ACK_001' in turn.final_agent_message else 'FAIL',
                'FinalAck': 'CFR_DOCTOR_LIVE_ACK_001' in turn.final_agent_message,
                'TurnStatus': turn.status,
                'Error': safe_error(turn.error_message),
            }
        except Exception as exc:
            return {'Status': 'FAIL', 'FinalAck': False, 'Error': safe_error(exc), 'ErrorClass': type(exc).__name__}
        finally:
            client.close()


def run_doctor(project_root=None, database='cfr.sqlite3', timeout=60, live=False, codex_home=None, codex_bin=None, gate_origin='unknown'):
    project_root = Path(project_root or Path.cwd())
    gate_origin = normalize_gate_origin(gate_origin)
    resolved_home = resolve_cfr_codex_home(codex_home)
    process_env = {**os.environ, **(proxy_child_env(resolve_proxy()) or {})}
    process_env['CODEX_HOME'] = str(resolved_home.path)
    runtime = inspect_runtime(project_root, process_env=process_env, timeout=min(timeout, 30), codex_home=resolved_home.path, codex_bin=codex_bin)
    home = codex_home_permissions(resolved_home.path)
    protocol_path = project_root / 'docs' / 'reference' / 'CFR_AND_BROKER_COEXISTENCE_ROUTING_CONTRACT_v1.2.md'
    protocol_hash = _hash_file(protocol_path)
    db_snapshot = _db_readonly_snapshot(database)
    capabilities = detect_platform_capabilities(desktop_continuity_live_validated=True if gate_origin == 'host_manual' else None)
    executable = runtime.get('CodexExecutable') or {'Path': None, 'Source': None, 'Kind': None}
    launcher = CodexLauncher(executable=codex_bin, environment=process_env)
    version = None
    try:
        completed = subprocess.run(
            launcher.build_command('--version'),
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=10,
            env=process_env,
        )
        version = (completed.stdout or completed.stderr).strip()
    except Exception:
        pass
    interface = config_schema_snapshot(launcher, version)
    consistency = (
        runtime.get('ResolvedCfrCodexHome') == runtime.get('ConfigReadCodexHome') == str(resolved_home.path)
        and runtime.get('ChildCodexHome') == str(resolved_home.path)
        and runtime.get('ChildCodexHomeExplicit') is True
    )
    result = {
        'Verdict': 'PASS',
        **gate_metadata(gate_origin),
        'Platform': {
            'OSFamily': capabilities.os_family,
            'PlatformName': capabilities.platform_name,
            'PythonVersion': capabilities.python_version,
            'HomeDirectory': str(capabilities.home_directory),
            'DefaultCfrDataDirectory': str(capabilities.default_cfr_data_directory),
            'CodexLauncherSupported': capabilities.codex_launcher_supported,
            'AppServerStdioSupported': capabilities.app_server_stdio_supported,
            'RolloutWatchSupported': capabilities.rollout_watch_supported,
            'DesktopContinuityLiveValidated': capabilities.desktop_continuity_live_validated,
        },
        'CfrInstanceId': CfrThreadRuntimeLeaseManager(database).instance_id,
        'CfrDatabase': db_snapshot,
        'ResolvedCfrCodexHome': str(resolved_home.path),
        'CodexHomeSource': resolved_home.source,
        'CodexHomeExists': home['Exists'],
        'CodexHomeReadable': home['Readable'],
        'CodexHomeWritable': home['Writable'],
        'CodexExecutable': executable.get('Path'),
        'CodexExecutableSource': executable.get('Source'),
        'CodexExecutableKind': executable.get('Kind'),
        'CodexVersion': version,
        'CodexInterfaceCompatibility': (
            'PASS' if interface.get('RequiredMethodsAvailable') is True
            else 'FAIL' if interface.get('GeneratedSchema') is True
            else 'UNKNOWN'
        ),
        'CodexInterfaceMissingRequiredMethods': interface.get('MissingRequiredMethods', []),
        'CodexInterfaceSchema': interface,
        'LoginStatus': runtime.get('LoginStatus'),
        'AuthMode': runtime.get('AuthMode'),
        'PlanType': runtime.get('PlanType'),
        'RoutingConsistency': runtime.get('RoutingConsistency'),
        'NetworkBasic': 'NOT_RUN_READ_ONLY',
        'SelectedNetworkMode': 'none',
        'AppServerInitialize': 'PASS' if runtime.get('AccountReadSucceeded') or runtime.get('ConfigReadSucceeded') else 'FAIL',
        'RuntimeLeaseSchema': db_snapshot.get('RuntimeLeaseSchema'),
        'RuntimeLeaseHealth': 'WARN_NON_DURABLE_DATABASE' if db_snapshot.get('RuntimeLeaseSchema') == 'NON_DURABLE_MEMORY' else ('PASS' if db_snapshot.get('RuntimeLeaseSchema') in ('PASS', 'NOT_PRESENT') else 'FAIL'),
        'RolloutSessionsDirectory': str(resolved_home.path / 'sessions'),
        'ReferenceProtocol': 'v1.2',
        'ReferenceProtocolSHA256': 'PASS' if protocol_hash == PROTOCOL_SHA256 else 'FAIL',
        'ReferenceProtocolHash': protocol_hash,
        'DesktopAgentVsHostContext': result_context(gate_origin),
        'DiagnosticsCodexHomeConsistency': 'PASS' if consistency else 'FAIL',
        'CfrCodexHomeBoundary': 'PASS' if consistency else 'FAIL',
    }
    if runtime.get('RuntimeError') and gate_origin != 'host_manual':
        result['Verdict'] = 'WARN'
        result['RuntimeInterpretation'] = 'HOST_VALIDATION_REQUIRED'
    elif runtime.get('RuntimeError'):
        result['Verdict'] = 'FAIL'
        result['RuntimeInterpretation'] = 'HOST_RUNTIME_FAILURE'
    if result['ReferenceProtocolSHA256'] == 'FAIL' or result['DiagnosticsCodexHomeConsistency'] == 'FAIL':
        result['Verdict'] = 'FAIL'
    if result['CodexInterfaceCompatibility'] == 'FAIL':
        result['Verdict'] = 'FAIL'
    if live:
        result['LiveModelProbe'] = _live_probe(project_root, timeout, process_env, resolved_home.path, codex_bin) if runtime.get('AccountReadSucceeded') and runtime.get('ConfigReadSucceeded') else {'Status': 'NOT_RUN_RUNTIME_PREREQUISITE'}
        if result['LiveModelProbe'].get('Status') not in ('PASS', 'NOT_RUN_RUNTIME_PREREQUISITE'):
            result['Verdict'] = 'FAIL'
    else:
        result['LiveModelProbe'] = 'NOT_RUN'
    return result


def result_context(gate_origin):
    return {'host_manual': 'HOST', 'desktop_agent': 'CODEX_AGENT_SANDBOX', 'unknown': 'UNKNOWN'}[gate_origin]
