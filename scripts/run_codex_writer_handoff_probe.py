import argparse
import json
import platform
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from cfr.codex.app_server import AppServerClient, AppServerRpcError
from cfr.codex.diagnostics import gate_metadata, normalize_gate_origin
from cfr.codex.launcher import CodexLauncher
from cfr.codex.threads import ThreadManager
from cfr.codex.turns import TurnManager
from cfr.config import child_process_env, resolve_cfr_codex_home
from cfr.network import proxy_child_env, resolve_proxy, sanitized_proxy_url


def now():
    return datetime.now(timezone.utc).isoformat()


def safe_error(value):
    text = str(value or '')
    for marker in ('Authorization:', 'Bearer ', 'api_key=', 'token=', 'secret='):
        if marker.lower() in text.lower():
            return f'{type(value).__name__ if value else "Error"}: [REDACTED]'
    return text[:500]


def append_log(path, message):
    with path.open('a', encoding='utf-8') as handle:
        handle.write(f'{now()} {message}\n')


def append_event(path, event):
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps({'at': now(), **event}, ensure_ascii=False, default=str) + '\n')


def fresh_client(process_env, codex_home, timeout):
    return AppServerClient(timeout=timeout, process_env=process_env, codex_home=codex_home)


def executable_snapshot():
    try:
        launcher = CodexLauncher()
        selected = launcher.discover()
        version = subprocess.run(launcher.build_command('--version'), capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
        return str(selected), (version.stdout or version.stderr).strip()
    except Exception:
        return None, None


def resume_once(client, thread_id):
    return ThreadManager(client).resume_thread(thread_id)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--codex-home', default=None)
    parser.add_argument('--gate-origin', choices=('desktop_agent', 'host_manual', 'unknown'), default='unknown')
    args = parser.parse_args()
    gate_origin = normalize_gate_origin(args.gate_origin)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    artifact_dir = ROOT / '.tmp' / 'writer-handoff' / run_id
    artifact_dir.mkdir(parents=True, exist_ok=False)
    workspace = artifact_dir / 'workspace'
    workspace.mkdir()
    result_path = artifact_dir / 'result.json'
    log_path = artifact_dir / 'writer_handoff.log'
    events_a = artifact_dir / 'app_server_a_events.jsonl'
    events_b = artifact_dir / 'app_server_b_events.jsonl'
    events_a.touch()
    events_b.touch()
    resolved_home = resolve_cfr_codex_home(args.codex_home)
    process_env = child_process_env(proxy_child_env(resolve_proxy()), resolved_home)
    executable, version = executable_snapshot()
    result = {
        'RunId': run_id,
        **gate_metadata(gate_origin),
        'started_at': now(),
        'OS': platform.platform(),
        'CodexExecutable': executable,
        'CodexVersion': version,
        'ResolvedCfrCodexHome': str(resolved_home.path),
        'CodexHomeSource': resolved_home.source,
        'ChildCodexHomeExplicit': True,
        'Proxy': sanitized_proxy_url(resolve_proxy().selected_proxy),
        'ThreadId': None,
        'AAppServerPID': None,
        'AFirstTurnCompleted': False,
        'AExactAck': False,
        'AProcessRunningBeforeClose': False,
        'BeforePrimaryClose': 'NOT_RUN',
        'ACloseStarted': False,
        'AProcessExited': False,
        'AExitCode': None,
        'AfterPrimaryExit': 'NOT_RUN',
        'BResumeThreadId': None,
        'WriterReleasePropagationRetry': 0,
        'GateInterpretation': 'NOT_REQUIRED',
        'Verdict': 'NOT_RUN',
    }
    client_a = None
    client_b = None
    try:
        append_log(log_path, 'APP_SERVER_A_START')
        client_a = fresh_client(process_env, resolved_home.path, args.timeout)
        client_a.start()
        result['AAppServerPID'] = client_a.process_id
        append_event(events_a, {'method': 'initialize', 'status': 'ok', 'pid': client_a.process_id})
        started = client_a.request('thread/start', {
            'cwd': str(workspace),
            'ephemeral': False,
            'threadSource': 'user',
            'historyMode': 'legacy',
            'sessionStartSource': 'startup',
        })
        thread = ThreadManager.ref_from_result(started, workspace)
        result['ThreadId'] = thread.thread_id
        append_event(events_a, {'method': 'thread/start', 'status': 'ok', 'thread_id': thread.thread_id})
        turn = TurnManager(client_a).run_turn(thread.thread_id, 'CFR_WRITER_HANDOFF_A: Do not call tools or modify files. Reply exactly CFR_WRITER_HANDOFF_A_ACK.', args.timeout)
        result['AFirstTurnCompleted'] = turn.status == 'completed'
        result['AExactAck'] = 'CFR_WRITER_HANDOFF_A_ACK' in turn.final_agent_message
        append_event(events_a, {'method': 'turn/completed', 'status': turn.status, 'final_ack': result['AExactAck'], 'error': safe_error(turn.error_message)})
        result['AProcessRunningBeforeClose'] = client_a.is_running
        if not result['AFirstTurnCompleted'] or not result['AExactAck']:
            result['Verdict'] = 'A_TURN_FAILED'
            return write_result(result, result_path)

        append_log(log_path, 'APP_SERVER_B_RESUME_WHILE_A_ALIVE')
        client_b = fresh_client(process_env, resolved_home.path, args.timeout)
        client_b.start()
        append_event(events_b, {'method': 'initialize', 'status': 'ok', 'pid': client_b.process_id})
        try:
            resumed = resume_once(client_b, thread.thread_id)
            resumed_thread = ThreadManager.ref_from_result(resumed, workspace)
            result['BeforePrimaryClose'] = 'UNEXPECTED_RESUME_SUCCESS'
            result['BResumeThreadId'] = resumed_thread.thread_id
            append_event(events_b, {'method': 'thread/resume', 'status': 'unexpected_success', 'thread_id': resumed_thread.thread_id})
        except AppServerRpcError as exc:
            if 'active writer' in exc.message.lower():
                result['BeforePrimaryClose'] = 'EXPECTED_ACTIVE_WRITER'
                append_event(events_b, {'method': 'thread/resume', 'status': 'expected_active_writer', 'error_code': exc.code})
            else:
                result['BeforePrimaryClose'] = 'RESUME_ERROR'
                append_event(events_b, {'method': 'thread/resume', 'status': 'error', 'error_code': exc.code})
        except Exception as exc:
            result['BeforePrimaryClose'] = 'RESUME_ERROR'
            append_event(events_b, {'method': 'thread/resume', 'status': 'error', 'error_class': type(exc).__name__})
        finally:
            client_b.close()
            client_b = None

        append_log(log_path, 'APP_SERVER_A_CLOSE_STARTED')
        client_a.close()
        lifecycle = client_a.lifecycle_snapshot()
        result['ACloseStarted'] = lifecycle['close_started']
        result['AProcessExited'] = not client_a.is_running and client_a.exit_code is not None
        result['AExitCode'] = client_a.exit_code
        append_log(log_path, f'APP_SERVER_A_EXITED pid={result["AAppServerPID"]} exit_code={result["AExitCode"]} mode={lifecycle["close_mode"]}')
        if not result['AProcessExited']:
            result['AfterPrimaryExit'] = 'FAIL_PROCESS_STILL_RUNNING'
            result['Verdict'] = 'WRITER_STATE_NOT_RELEASED_AFTER_PROCESS_EXIT'
            return write_result(result, result_path)

        append_log(log_path, 'APP_SERVER_B_RESUME_AFTER_A_EXIT')
        client_b = fresh_client(process_env, resolved_home.path, args.timeout)
        client_b.start()
        append_event(events_b, {'method': 'initialize', 'status': 'ok', 'pid': client_b.process_id, 'phase': 'after_exit'})
        try:
            resumed = resume_once(client_b, thread.thread_id)
            resumed_thread = ThreadManager.ref_from_result(resumed, workspace)
            result['AfterPrimaryExit'] = 'PASS'
            result['BResumeThreadId'] = resumed_thread.thread_id
            append_event(events_b, {'method': 'thread/resume', 'status': 'pass', 'thread_id': resumed_thread.thread_id, 'phase': 'after_exit'})
        except AppServerRpcError as exc:
            if 'active writer' in exc.message.lower():
                result['WriterReleasePropagationRetry'] = 1
                time.sleep(1)
                try:
                    resumed = resume_once(client_b, thread.thread_id)
                    resumed_thread = ThreadManager.ref_from_result(resumed, workspace)
                    result['AfterPrimaryExit'] = 'PASS'
                    result['BResumeThreadId'] = resumed_thread.thread_id
                    append_event(events_b, {'method': 'thread/resume', 'status': 'pass_after_retry', 'thread_id': resumed_thread.thread_id, 'phase': 'after_exit'})
                except Exception as retry_exc:
                    result['AfterPrimaryExit'] = 'WRITER_STATE_NOT_RELEASED_AFTER_PROCESS_EXIT'
                    append_event(events_b, {'method': 'thread/resume', 'status': 'failed_after_retry', 'error_class': type(retry_exc).__name__})
            else:
                result['AfterPrimaryExit'] = 'RESUME_ERROR'
        except Exception as exc:
            result['AfterPrimaryExit'] = 'RESUME_ERROR'
            append_event(events_b, {'method': 'thread/resume', 'status': 'error', 'error_class': type(exc).__name__})
        result['Verdict'] = 'PASS' if result['BeforePrimaryClose'] == 'EXPECTED_ACTIVE_WRITER' and result['AfterPrimaryExit'] == 'PASS' else result['AfterPrimaryExit']
        return write_result(result, result_path)
    except Exception as exc:
        result['Verdict'] = 'PROBE_ERROR'
        result['ErrorClass'] = type(exc).__name__
        result['Error'] = safe_error(exc)
        if re.search(r'(?i)(access denied|stdout eof|initialize sqlite state runtime)', result['Error']):
            result['GateInterpretation'] = 'HOST_RUNTIME_VALIDATION_REQUIRED' if gate_origin != 'host_manual' else 'HOST_CODEX_HOME_RUNTIME_INIT_FAILURE'
        return write_result(result, result_path)
    finally:
        if client_b:
            client_b.close()
        if client_a:
            client_a.close()


def write_result(result, path):
    result['completed_at'] = now()
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps({'RunId': result['RunId'], 'ArtifactDir': str(path.parent), 'Verdict': result['Verdict'], 'result': result}, ensure_ascii=False, default=str))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
