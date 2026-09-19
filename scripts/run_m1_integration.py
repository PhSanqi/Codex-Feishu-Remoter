import argparse
import asyncio
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from cfr.codex.binding import CodexAdapter
from cfr.codex.app_server import AppServerClient
from cfr.codex.diagnostics import gate_metadata, normalize_gate_origin
from cfr.codex.rollout import RolloutWatcher, canonical_path_key
from cfr.codex.threads import ThreadManager
from cfr.config import resolve_cfr_codex_home
from cfr.core.projector import EventProjector
from cfr.network import proxy_child_env, resolve_proxy
from cfr.storage.db import BindingStore


def plain(value):
    if is_dataclass(value):
        return {key: plain(item) for key, item in asdict(value).items()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def canonical_path(value):
    return canonical_path_key(value)


def same_path(left, right):
    return canonical_path(left) == canonical_path(right)


def compute_m1_verdict(result):
    accepted_network = {'PASS_DIRECT', 'PASS_PROXY', 'HTTP_FALLBACK_WORKAROUND_CONFIRMED'}
    mandatory = {
        'IntegrationA_Create': 'INTEGRATION_A_FAILED',
        'IntegrationA_RuntimeRelease': 'INTEGRATION_A_RUNTIME_RELEASE_FAILED',
        'IntegrationB_Resume': 'INTEGRATION_B_FAILED',
        'IntegrationC_RolloutOffset': 'INTEGRATION_C_FAILED',
        'IntegrationD_Dedupe': 'INTEGRATION_D_FAILED',
        'IntegrationE_ActiveWriter': 'INTEGRATION_E_FAILED',
        'IntegrationF_Stop': 'INTEGRATION_F_STOP_FAILED',
        'BindingRecovery': 'BINDING_RECOVERY_FAILED',
    }
    if result.get('NetworkPreflight') not in accepted_network:
        result['BlockingIssues'].append('NETWORK_PREFLIGHT_FAILED')
    if result.get('WriterHandoffAfterPrimaryExit') != 'PASS':
        result['BlockingIssues'].append('WRITER_HANDOFF_FAILED')
    for field, blocker in mandatory.items():
        status = result.get(field)
        if status == 'NOT_OBSERVED' and field == 'IntegrationF_Stop':
            result['BlockingIssues'].append('INTEGRATION_F_NOT_OBSERVED')
        elif status != 'PASS':
            result['BlockingIssues'].append('BINDING_RECOVERY_CURSOR_MISMATCH' if field == 'BindingRecovery' and status == 'FAIL' else blocker)
    if result.get('LocalAcceptance') != 'PASS':
        result['BlockingIssues'].append('LOCAL_ACCEPTANCE_FAILED')
    if result.get('LocalBindingAcceptance') != 'PASS':
        result['BlockingIssues'].append('LOCAL_BINDING_ACCEPTANCE_FAILED')
    result['BlockingIssues'] = list(dict.fromkeys(result['BlockingIssues']))
    result['M1Verdict'] = 'CFR_M1_CODEX_CORE_COMPLETE' if not result['BlockingIssues'] else 'CFR_M1_CODEX_CORE_PARTIAL'
    return result['M1Verdict']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--workspace', type=Path, default=ROOT / '.tmp' / 'm1_integration_workspace')
    parser.add_argument('--codex-home', default=None)
    parser.add_argument('--gate-origin', choices=('desktop_agent', 'host_manual', 'unknown'), default='unknown')
    args = parser.parse_args()
    gate_origin = normalize_gate_origin(args.gate_origin)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    artifact_dir = ROOT / '.tmp' / 'm1-integration' / run_id
    artifact_dir.mkdir(parents=True, exist_ok=False)
    args.workspace.mkdir(parents=True, exist_ok=True)
    resolved_home = resolve_cfr_codex_home(args.codex_home)
    result = {
        'RunId': run_id,
        **gate_metadata(gate_origin),
        'started_at': datetime.now(timezone.utc).isoformat(),
        'CodexExecutable': 'codex.cmd/codex discovered by CodexLauncher',
        'CodexVersion': 'unknown',
        'Workspace': str(args.workspace),
        'IntegrationA_Create': 'NOT_RUN',
        'IntegrationB_Resume': 'NOT_RUN',
        'IntegrationC_RolloutOffset': 'NOT_RUN',
        'IntegrationD_Dedupe': 'NOT_RUN',
        'IntegrationE_ActiveWriter': 'NOT_RUN',
        'IntegrationF_Stop': 'NOT_RUN',
        'BindingRecovery': 'NOT_RUN',
        'IntegrationA_RuntimeRelease': 'NOT_RUN',
        'WriterHandoffBeforePrimaryClose': 'NOT_RUN',
        'WriterHandoffAfterPrimaryExit': 'NOT_RUN',
        'WriterReleasePropagationRetry': None,
        'ResolvedCfrCodexHome': str(resolved_home.path),
        'CodexHomeSource': resolved_home.source,
        'ChildCodexHomeExplicit': True,
        'BlockingIssues': [],
        'M1Verdict': 'CFR_M1_CODEX_CORE_PARTIAL',
        'events': [],
    }
    log_path = artifact_dir / 'integration.log'
    events_path = artifact_dir / 'events.jsonl'

    def log(message):
        with log_path.open('a', encoding='utf-8') as handle:
            handle.write(message + '\n')

    def record(name, status, **data):
        result[name] = status
        event = {'name': name, 'status': status, **plain(data)}
        result['events'].append(event)
        with events_path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(event, ensure_ascii=False, default=str) + '\n')
        log(f'{name}: {status} {data}')

    local = run_local_acceptance()
    result['LocalAcceptance'] = local['status']
    result['LocalBindingAcceptance'] = local['binding_status']
    result['IntegrationE_ActiveWriter'] = local['writer_status']

    preflight = run_network_preflight(args.timeout, args.codex_home, gate_origin)
    result['NetworkPreflight'] = preflight['status']
    result['NetworkPreflightRunId'] = preflight.get('run_id')
    result['NetworkPreflightArtifactDir'] = preflight.get('artifact_dir')
    result['CodexExecutable'] = preflight.get('codex', {}).get('selected') or result['CodexExecutable']
    result['CodexVersion'] = preflight.get('codex', {}).get('version') or result['CodexVersion']
    result['SelectedNetworkMode'] = preflight.get('selected_network_mode', 'none')
    result['ProxySource'] = preflight.get('proxy_source')
    result['ProxyHost'] = preflight.get('proxy_host')
    result['ProxyPort'] = preflight.get('proxy_port')
    result['AuthMode'] = preflight.get('auth_mode')
    result['LoginStatus'] = preflight.get('login_status')
    result['PlanType'] = preflight.get('plan_type')
    result['ExpectedBackendClass'] = preflight.get('expected_backend_class')
    result['RoutingConsistency'] = preflight.get('routing_consistency')
    result['TransportConfigOverrides'] = preflight.get('transport_config_overrides', [])
    result['ResolvedCfrCodexHome'] = preflight.get('resolved_cfr_codex_home') or str(resolved_home.path)
    result['CodexHomeSource'] = preflight.get('codex_home_source') or resolved_home.source
    result['ChildCodexHomeExplicit'] = preflight.get('child_codex_home_explicit', True)
    result['ConfigReadCodexHome'] = preflight.get('config_read_codex_home')
    result['ChildCodexHome'] = preflight.get('child_codex_home')
    result['CfrCodexHomeBoundary'] = preflight.get('cfr_codex_home_boundary', 'UNKNOWN')
    result['GateInterpretation'] = preflight.get('gate_interpretation', 'NOT_REQUIRED')
    process_env = proxy_child_env(resolve_proxy()) if result['SelectedNetworkMode'] == 'proxy' else None
    config_overrides = result['TransportConfigOverrides'] or None
    record('LocalAcceptance', local['status'], tests_run=local['tests_run'])
    record('LocalBindingAcceptance', local['binding_status'])
    record('IntegrationE_ActiveWriter', local['writer_status'])

    writer = run_writer_handoff(args.timeout, resolved_home.path, gate_origin) if preflight['status'] in ('PASS_DIRECT', 'PASS_PROXY', 'HTTP_FALLBACK_WORKAROUND_CONFIRMED') else {
        'verdict': 'NOT_RUN_DEPENDS_ON_NETWORK_AUTH',
        'before_primary_close': 'NOT_RUN',
        'after_primary_exit': 'NOT_RUN',
        'retry': 0,
    }
    result['WriterHandoffBeforePrimaryClose'] = writer.get('before_primary_close', 'NOT_RUN')
    result['WriterHandoffAfterPrimaryExit'] = writer.get('after_primary_exit', 'NOT_RUN')
    result['WriterReleasePropagationRetry'] = writer.get('retry', 0)
    result['WriterHandoffRunId'] = writer.get('run_id')
    result['WriterHandoffArtifactDir'] = writer.get('artifact_dir')
    record('WriterHandoffProbe', writer.get('verdict', 'NOT_RUN'), before_primary_close=result['WriterHandoffBeforePrimaryClose'], after_primary_exit=result['WriterHandoffAfterPrimaryExit'], retry=result['WriterReleasePropagationRetry'])

    async def run():
        database = artifact_dir / 'binding.sqlite3'
        store = BindingStore(database)
        adapter = CodexAdapter(store=store, timeout=args.timeout, process_env=process_env, config_overrides=config_overrides, codex_home=resolved_home.path)
        thread_id = None
        rollout_path = None
        try:
            if preflight['status'] not in ('PASS_DIRECT', 'PASS_PROXY', 'HTTP_FALLBACK_WORKAROUND_CONFIRMED'):
                result['IntegrationA_Create'] = f"BLOCKED_{preflight['status']}"
                result['IntegrationB_Resume'] = 'NOT_RUN_DEPENDS_ON_A'
                result['IntegrationC_RolloutOffset'] = 'NOT_RUN_DEPENDS_ON_A_B'
                result['IntegrationD_Dedupe'] = 'NOT_RUN_DEPENDS_ON_C'
                result['IntegrationF_Stop'] = 'NOT_RUN_MODEL_NETWORK_BLOCKED'
                result['BindingRecovery'] = 'NOT_RUN_DEPENDS_ON_A'
                result['BlockingIssues'].append(preflight.get('reason') or preflight['status'])
                return
            if writer.get('after_primary_exit') != 'PASS':
                result['IntegrationA_Create'] = 'NOT_RUN_DEPENDS_ON_WRITER_HANDOFF'
                result['IntegrationA_RuntimeRelease'] = 'NOT_RUN_DEPENDS_ON_WRITER_HANDOFF'
                result['IntegrationB_Resume'] = 'NOT_RUN_DEPENDS_ON_A_RUNTIME_RELEASE'
                result['IntegrationC_RolloutOffset'] = 'NOT_RUN_DEPENDS_ON_A_RUNTIME_RELEASE'
                result['IntegrationD_Dedupe'] = 'NOT_RUN_DEPENDS_ON_A_RUNTIME_RELEASE'
                result['IntegrationF_Stop'] = 'NOT_RUN_MODEL_NETWORK_BLOCKED'
                result['BindingRecovery'] = 'NOT_RUN_DEPENDS_ON_A_RUNTIME_RELEASE'
                result['BlockingIssues'].append(writer.get('verdict') or 'WRITER_HANDOFF_FAILED')
                return
            try:
                from cfr.codex.launcher import CodexLauncher
                executable = CodexLauncher().discover()
                result['CodexExecutable'] = str(executable)
                command = [str(executable), '--version']
                if executable.suffix.lower() in ('.cmd', '.bat'):
                    command = [os.environ.get('COMSPEC', r'C:\Windows\System32\cmd.exe'), '/d', '/s', '/c', str(executable), '--version']
                version = subprocess.run(command, capture_output=True, text=True, timeout=10)
                result['CodexVersion'] = (version.stdout or version.stderr).strip()
            except Exception as exc:
                log(f'Codex version discovery failed: {exc}')
            try:
                created = await adapter.create_conversation(
                    args.workspace,
                    'CFR-M1-INTEGRATION',
                    'CFR_M1_INTEGRATION_A: do not call tools or modify files; reply exactly CFR_M1_INTEGRATION_A_ACK.',
                )
                thread_id = created.thread_id
                rollout_path = created.rollout_path
                binding = store.get_binding(thread_id)
                a_checks = {
                    'ThreadCreated': bool(thread_id),
                    'TurnStatus': created.initial_turn.status == 'completed',
                    'FinalAck': 'CFR_M1_INTEGRATION_A_ACK' in created.initial_turn.final_agent_message,
                    'BindingSaved': binding is not None,
                    'ThreadName': created.thread.name == 'CFR-M1-INTEGRATION',
                    'RolloutPathPresent': bool(created.thread.rollout_path),
                    'RolloutFileExists': bool(created.thread.rollout_path and created.thread.rollout_path.exists()),
                }
                lifecycle = adapter.last_client_lifecycle or {}
                result['AAppServerPID'] = lifecycle.get('pid')
                result['AAppServerExitedBeforeB'] = bool(lifecycle.get('exited'))
                runtime_release = 'PASS' if result['AAppServerExitedBeforeB'] else 'FAIL'
                record('IntegrationA_Create', 'PASS' if all(a_checks.values()) else 'FAIL', thread_id=thread_id, rollout_path=rollout_path, turn=created.initial_turn, checks=a_checks)
                record('IntegrationA_RuntimeRelease', runtime_release, pid=result['AAppServerPID'], exited_before_b=result['AAppServerExitedBeforeB'], lifecycle=lifecycle)
                if runtime_release != 'PASS':
                    result['IntegrationB_Resume'] = 'NOT_RUN_DEPENDS_ON_A_RUNTIME_RELEASE'
                    result['IntegrationC_RolloutOffset'] = 'NOT_RUN_DEPENDS_ON_A_RUNTIME_RELEASE'
                    result['IntegrationD_Dedupe'] = 'NOT_RUN_DEPENDS_ON_A_RUNTIME_RELEASE'
                    result['IntegrationF_Stop'] = 'NOT_RUN_MODEL_NETWORK_BLOCKED'
                    result['BindingRecovery'] = 'NOT_RUN_DEPENDS_ON_A_RUNTIME_RELEASE'
                    result['BlockingIssues'].append('WRITER_STATE_NOT_RELEASED_AFTER_PROCESS_EXIT')
                    return
            except Exception as exc:
                detail = str(exc)
                if getattr(exc, 'data', None) is not None:
                    detail += ' | turn_error=' + str(getattr(exc.data, 'error_message', '') or '')
                blocked = any(marker in detail.lower() for marker in ('api.openai.com', 'stream disconnected', 'error sending request'))
                record('IntegrationA_Create', 'BLOCKED_EXTERNAL_NETWORK' if blocked else 'FAIL', error=detail, traceback=traceback.format_exc())
                result['BlockingIssues'].append(str(exc))
                return

            binding = store.get_binding(thread_id)
            result['ThreadId'] = thread_id
            result['RolloutPath'] = str(rollout_path or '')
            result['BindingSaved'] = bool(binding)
            result['RolloutFileExists'] = bool(binding and binding.rollout_path and binding.rollout_path.exists())

            store.close()
            store = BindingStore(database)
            adapter = CodexAdapter(store=store, timeout=args.timeout, process_env=process_env, config_overrides=config_overrides, codex_home=resolved_home.path)
            try:
                second = await adapter.send_message(thread_id, 'CFR_M1_INTEGRATION_B: confirm CFR_M1_INTEGRATION_A, then reply exactly CFR_M1_INTEGRATION_B_ACK.')
                binding_b = store.get_binding(thread_id)
                resumed_ref = adapter.last_resume.get(thread_id)
                history_text = json.dumps(adapter.last_resume_payload.get(thread_id, {}), ensure_ascii=False)
                b_checks = {
                    'ResumeSucceeded': resumed_ref is not None,
                    'SameThreadId': bool(resumed_ref and resumed_ref.thread_id == thread_id),
                    'HistoryContainsA': 'CFR_M1_INTEGRATION_A' in history_text,
                    'TurnCompleted': second.status == 'completed',
                    'CorrectAck': 'CFR_M1_INTEGRATION_B_ACK' in second.final_agent_message,
                    'SameRollout': bool(binding_b and resumed_ref and binding_b.rollout_path == resumed_ref.rollout_path),
                }
                record('IntegrationB_Resume', 'PASS' if all(b_checks.values()) else 'FAIL', turn=second, resumed_thread_id=resumed_ref.thread_id if resumed_ref else None, rollout_path=resumed_ref.rollout_path if resumed_ref else None, checks=b_checks)
            except Exception as exc:
                detail = str(exc)
                if getattr(exc, 'data', None) is not None:
                    detail += ' | turn_error=' + str(getattr(exc.data, 'error_message', '') or '')
                blocked = any(marker in detail.lower() for marker in ('api.openai.com', 'stream disconnected', 'error sending request'))
                record('IntegrationB_Resume', 'BLOCKED_EXTERNAL_NETWORK' if blocked else 'FAIL', error=detail, traceback=traceback.format_exc())
                result['BlockingIssues'].append(str(exc))
                return

            binding = store.get_binding(thread_id)
            watcher = RolloutWatcher(thread_id=thread_id, path=binding.rollout_path, byte_offset=0)
            historical = watcher.poll()
            historical_end_offset = watcher.byte_offset
            store.update_rollout_offset(thread_id, historical_end_offset)
            store.close()
            store = BindingStore(database)
            restored = store.get_binding(thread_id)
            restarted = RolloutWatcher(thread_id=thread_id, path=restored.rollout_path, byte_offset=restored.last_rollout_byte_offset)
            recovered_historical_replay = restarted.poll()
            no_replay = recovered_historical_replay == []
            try:
                third = await CodexAdapter(store=store, timeout=args.timeout, process_env=process_env, config_overrides=config_overrides, codex_home=resolved_home.path).send_message(thread_id, 'CFR_M1_INTEGRATION_C: reply exactly CFR_M1_INTEGRATION_C_ACK and do not modify files.')
                new_events = restarted.poll()
                projector = EventProjector(store)
                rollout_event = new_events[0] if new_events else None
                app_event = None
                if rollout_event:
                    from cfr.core.events import CfrEvent, EventSource
                    app_event = CfrEvent(rollout_event.thread_id, rollout_event.turn_id, rollout_event.item_id, rollout_event.event_type, EventSource.CODEX, rollout_event.text, rollout_event.timestamp, 'app_server')
                projected = projector.project_event(app_event) if app_event else None
                duplicate = projector.project_event(rollout_event) if rollout_event else None
                d_checks = {'FirstProjected': projected is not None, 'SecondProjected': duplicate is not None, 'DuplicateSuppressed': projected is not None and duplicate is None, 'SameLogicalKey': bool(app_event and rollout_event and app_event.key == rollout_event.key)}
                record('IntegrationD_Dedupe', 'PASS' if d_checks['FirstProjected'] and d_checks['DuplicateSuppressed'] and d_checks['SameLogicalKey'] else 'FAIL', checks=d_checks)

                expected_recovery_offset = restarted.byte_offset
                store.update_rollout_offset(thread_id, expected_recovery_offset)
                persisted_after_c = store.get_binding(thread_id)
                final_persisted_offset = persisted_after_c.last_rollout_byte_offset if persisted_after_c else None
                c_checks = {
                    'InitialHistoricalCount': len(historical),
                    'HistoricalEndOffset': historical_end_offset,
                    'RecoveredHistoricalEndOffset': restored.last_rollout_byte_offset,
                    'NoHistoricalReplay': no_replay,
                    'NewEventsObserved': bool(new_events),
                    'OffsetMonotonic': restarted.byte_offset >= historical_end_offset,
                    'TurnCompleted': third.status == 'completed',
                    'PostCTurnObservedOffset': expected_recovery_offset,
                    'PostCTurnPersistedOffset': final_persisted_offset,
                    'FinalObservedByteOffset': expected_recovery_offset,
                    'FinalPersistedByteOffset': final_persisted_offset,
                    'FinalOffsetPersisted': final_persisted_offset == expected_recovery_offset,
                }
                c_pass = all(c_checks[key] for key in ('NoHistoricalReplay', 'NewEventsObserved', 'OffsetMonotonic', 'TurnCompleted', 'FinalOffsetPersisted'))
                record('IntegrationC_RolloutOffset', 'PASS' if c_pass else 'FAIL', **c_checks)

                expected_rollout_path = Path(restarted.path)
                expected_thread_name = binding.thread_name
                expected_cwd = Path(binding.cwd)
                restarted = None
                projector = None
                store.close()
                store = BindingStore(database)
                recovered = store.get_binding(thread_id)
                recovery_watcher = RolloutWatcher(
                    thread_id=thread_id,
                    path=recovered.rollout_path if recovered else None,
                    byte_offset=recovered.last_rollout_byte_offset if recovered else 0,
                )
                recovery_events = recovery_watcher.poll()
                resumed_after_restart = await asyncio.to_thread(_fresh_resume, thread_id, process_env, config_overrides, resolved_home.path)
                recovery_checks = {
                    'BindingFoundAfterRestart': recovered is not None,
                    'ThreadNameRecovered': bool(recovered and recovered.thread_name == expected_thread_name),
                    'CwdRecovered': bool(recovered and same_path(recovered.cwd, expected_cwd)),
                    'RolloutPathRecovered': bool(recovered and same_path(recovered.rollout_path, expected_rollout_path)),
                    'ExpectedByteOffset': expected_recovery_offset,
                    'RecoveredByteOffset': recovered.last_rollout_byte_offset if recovered else None,
                    'ByteOffsetRecovered': bool(recovered and recovered.last_rollout_byte_offset == expected_recovery_offset),
                    'RecoveryNoHistoricalReplay': recovery_events == [],
                    'ThreadResumeAfterRestart': bool(resumed_after_restart and resumed_after_restart.thread_id == thread_id),
                    'SameThreadAfterRestart': bool(resumed_after_restart and resumed_after_restart.thread_id == thread_id),
                }
                recovery_pass = all(recovery_checks[key] for key in (
                    'BindingFoundAfterRestart',
                    'ThreadNameRecovered',
                    'CwdRecovered',
                    'RolloutPathRecovered',
                    'ByteOffsetRecovered',
                    'RecoveryNoHistoricalReplay',
                    'ThreadResumeAfterRestart',
                    'SameThreadAfterRestart',
                ))
                result['BindingRecovery'] = 'PASS' if recovery_pass else 'FAIL'
                record('BindingRecovery', result['BindingRecovery'], checks=recovery_checks)

                long_adapter = CodexAdapter(store=store, timeout=max(args.timeout, 90), process_env=process_env, config_overrides=config_overrides, codex_home=resolved_home.path)
                long_task = asyncio.create_task(long_adapter.send_message(thread_id, 'CFR_M1_INTEGRATION_F: perform a long, read-only analysis of the supplied repository architecture; do not call tools or modify files.'))
                active_seen = False
                deadline = asyncio.get_running_loop().time() + 10
                while asyncio.get_running_loop().time() < deadline and not long_adapter.registry.get(thread_id):
                    await asyncio.sleep(0.05)
                active_seen = long_adapter.registry.get(thread_id) is not None
                if active_seen:
                    stop_result = await long_adapter.stop(thread_id)
                    stopped_turn = await long_task
                    fresh_resume = await asyncio.to_thread(_fresh_resume, thread_id, process_env, config_overrides, resolved_home.path)
                    record('IntegrationF_Stop', 'PASS' if (stopped_turn.status == 'interrupted' and fresh_resume and fresh_resume.thread_id == thread_id) else 'FAIL', stop_result=stop_result, stopped_turn=stopped_turn, thread_still_exists=bool(fresh_resume))
                else:
                    await long_task
                    record('IntegrationF_Stop', 'NOT_OBSERVED', active_turn_seen=False)
            except Exception as exc:
                detail = str(exc)
                if getattr(exc, 'data', None) is not None:
                    detail += ' | turn_error=' + str(getattr(exc.data, 'error_message', '') or '')
                blocked = any(marker in detail.lower() for marker in ('api.openai.com', 'stream disconnected', 'error sending request'))
                record('IntegrationC_RolloutOffset', 'BLOCKED_EXTERNAL_NETWORK' if blocked else 'FAIL', error=detail, traceback=traceback.format_exc())
                result['BlockingIssues'].append(str(exc))
        finally:
            store.close()

    asyncio.run(run())
    compute_m1_verdict(result)
    result['completed_at'] = datetime.now(timezone.utc).isoformat()
    (artifact_dir / 'result.json').write_text(json.dumps(plain(result), ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps({'RunId': run_id, 'ArtifactDir': str(artifact_dir), 'result': result}, ensure_ascii=False, default=str))
    return 0


def _fresh_resume(thread_id, process_env=None, config_overrides=None, codex_home=None):
    with AppServerClient(timeout=30, process_env=process_env, config_overrides=config_overrides, codex_home=codex_home) as client:
        resumed = ThreadManager(client).resume_thread(thread_id)
        return ThreadManager.ref_from_result(resumed)


def run_writer_handoff(timeout, codex_home, gate_origin='unknown'):
    script = Path(__file__).with_name('run_codex_writer_handoff_probe.py')
    try:
        completed = subprocess.run(
            [sys.executable, str(script), '--timeout', str(timeout), '--codex-home', str(codex_home), '--gate-origin', gate_origin],
            capture_output=True,
            text=True,
            timeout=max(timeout * 2 + 30, 90),
        )
        payload = json.loads((completed.stdout or '').strip().splitlines()[-1])
        probe = payload.get('result', {})
        return {
            'verdict': payload.get('Verdict') or probe.get('Verdict', 'UNKNOWN'),
            'run_id': payload.get('RunId'),
            'artifact_dir': payload.get('ArtifactDir'),
            'before_primary_close': probe.get('BeforePrimaryClose', 'UNKNOWN'),
            'after_primary_exit': probe.get('AfterPrimaryExit', 'UNKNOWN'),
            'retry': probe.get('WriterReleasePropagationRetry', 0),
        }
    except Exception as exc:
        return {'verdict': 'PROBE_ERROR', 'before_primary_close': 'UNKNOWN', 'after_primary_exit': 'UNKNOWN', 'retry': 0, 'reason': str(exc)}


def run_network_preflight(timeout, codex_home=None, gate_origin='unknown'):
    script = Path(__file__).with_name('run_codex_network_preflight.py')
    try:
        command = [sys.executable, str(script), '--timeout', str(timeout)]
        if codex_home:
            command.extend(['--codex-home', str(codex_home)])
        command.extend(['--gate-origin', gate_origin])
        completed = subprocess.run(command, capture_output=True, text=True, timeout=max(timeout * 2 + 30, 90))
        payload = json.loads((completed.stdout or '').strip().splitlines()[-1])
        preflight_result = payload.get('result', {})
        attempts = preflight_result.get('Attempts', [])
        reason = attempts[-1].get('probe', {}).get('TurnError') if attempts else None
        if not reason and preflight_result.get('Classification') in ('AUTH_STATE_UNKNOWN', 'AUTH_MODE_DECISION_REQUIRED', 'CHATGPT_AUTH_PROVIDER_ROUTE_MISMATCH'):
            reason = preflight_result.get('Classification')
        if preflight_result.get('GateInterpretation') == 'HOST_RUNTIME_VALIDATION_REQUIRED':
            reason = 'HOST_RUNTIME_VALIDATION_REQUIRED'
        elif preflight_result.get('RuntimeError') and preflight_result.get('Classification') == 'AUTH_STATE_UNKNOWN':
            reason = f"CFR_CODEX_HOME_RUNTIME_INIT_FAILURE: {preflight_result.get('RuntimeError')}"
        reason = reason or preflight_result.get('TcpError') or preflight_result.get('TlsError') or preflight_result.get('DnsError')
        return {
            'status': payload.get('Preflight', 'UNKNOWN'),
            'run_id': payload.get('RunId'),
            'artifact_dir': payload.get('ArtifactDir'),
            'reason': reason,
            'codex': preflight_result.get('Codex', {}),
            'selected_network_mode': preflight_result.get('SelectedNetworkMode', 'none'),
            'proxy_source': preflight_result.get('SystemProxySource'),
            'proxy_host': preflight_result.get('ProxyHost'),
            'proxy_port': preflight_result.get('ProxyPort'),
            'auth_mode': preflight_result.get('AuthMode'),
            'login_status': preflight_result.get('LoginStatus'),
            'plan_type': preflight_result.get('PlanType'),
            'expected_backend_class': preflight_result.get('ExpectedBackendClass'),
            'routing_consistency': preflight_result.get('RoutingConsistency'),
            'transport_config_overrides': preflight_result.get('TransportConfigOverrides', []),
            'resolved_cfr_codex_home': preflight_result.get('ResolvedCfrCodexHome'),
            'codex_home_source': preflight_result.get('CodexHomeSource'),
            'child_codex_home_explicit': preflight_result.get('ChildCodexHomeExplicit'),
            'config_read_codex_home': preflight_result.get('ConfigReadCodexHome'),
            'child_codex_home': preflight_result.get('ChildCodexHome'),
            'cfr_codex_home_boundary': preflight_result.get('CfrCodexHomeBoundary'),
            'gate_origin': preflight_result.get('GateOrigin', gate_origin),
            'runtime_execution_context': preflight_result.get('RuntimeExecutionContext', 'UNKNOWN'),
            'gate_interpretation': preflight_result.get('GateInterpretation', 'NOT_REQUIRED'),
        }
    except Exception as exc:
        return {'status': 'UNKNOWN', 'reason': str(exc)}


def run_local_acceptance():
    import unittest
    tests_dir = str(ROOT / 'tests' / 'unit')
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    suite = unittest.defaultTestLoader.loadTestsFromName('test_m1_completion.M1CompletionTests')
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    writer_names = {'test_active_writer_maps_to_structured_error_and_recovers', 'test_same_process_stop_interrupts_and_finishes_turn'}
    writer_suite = unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromName(f'test_m1_completion.M1CompletionTests.{name}') for name in writer_names])
    writer_outcome = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(writer_suite)
    binding_names = {'test_binding_migration_and_cursor_preservation', 'test_upsert_preserves_cursor_and_returns_model', 'test_projector_suppresses_duplicate', 'test_rollout_explicit_thread_and_partial_utf8_line'}
    binding_suite = unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromName(f'test_m1_completion.M1CompletionTests.{name}') for name in binding_names])
    binding_outcome = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(binding_suite)
    return {
        'status': 'PASS' if not outcome.failures and not outcome.errors else 'FAIL',
        'writer_status': 'PASS' if not writer_outcome.failures and not writer_outcome.errors else 'FAIL',
        'binding_status': 'PASS' if not binding_outcome.failures and not binding_outcome.errors else 'FAIL',
        'tests_run': outcome.testsRun,
    }


if __name__ == '__main__':
    raise SystemExit(main())
