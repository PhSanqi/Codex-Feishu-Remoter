"""Fresh-process Feishu shutdown probe.

The child owns the SDK connection. The parent waits for the child process to
exit before evaluating warnings, so interpreter-teardown warnings cannot be
mistaken for a clean shutdown.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import inspect
import json
from pathlib import Path
import re
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cfr.feishu.config import load_settings
from cfr.feishu.connection import FeishuConnectionLease
from cfr.feishu.log_sanitize import sanitize_feishu_log_text
from cfr.feishu.transport import ChannelFeishuTransport


BLOCKER_TOKENS = (
    'Task was destroyed',
    'was never awaited',
    'RuntimeWarning',
    'ResourceWarning',
    'This event loop is already running',
    'Cannot run the event loop while another loop is running',
)
SENSITIVE_ASSIGNMENT = re.compile(
    r'(?i)(?:access_key|ticket|app_secret|tenant_access_token|user_access_token|authorization)'
    r'\s*(?:=|:)\s*(?!<redacted>|\*+|None\b)[^\s&,}]+',
)


def sdk_install_diagnostic() -> dict:
    result = {
        'InstalledSdkVersion': None,
        'InstalledChannelStopImplementationHash': None,
        'InstalledChannelStopImplementationDiagnostic': 'NOT_AVAILABLE',
        'InstalledWsClientShape': {},
        'InstalledExpiringCacheShape': {},
    }
    try:
        result['InstalledSdkVersion'] = importlib.metadata.version('lark-channel-sdk')
        from lark_channel import FeishuChannel
        stop = getattr(FeishuChannel, 'stop', None)
        source = inspect.getsource(stop) if stop else ''
        result['InstalledChannelStopImplementationHash'] = hashlib.sha256(source.encode()).hexdigest() if source else None
        result['InstalledChannelStopImplementationDiagnostic'] = 'PUBLIC_STOP_PRESENT' if stop else 'PUBLIC_STOP_MISSING'
        from lark_channel.ws.client import Client
        client_source = inspect.getsource(Client)
        result['InstalledWsClientShape'] = {
            'class': f'{Client.__module__}.{Client.__qualname__}',
            'methods': [name for name in ('start', 'stop', 'close', 'disconnect', '_disconnect') if hasattr(Client, name)],
            'loop_attribute': 'self._loop' in client_source,
        }
        from lark_channel.core.cache.expiring_cache import ExpiringCache
        cache_source = inspect.getsource(ExpiringCache)
        result['InstalledExpiringCacheShape'] = {
            'class': f'{ExpiringCache.__module__}.{ExpiringCache.__qualname__}',
            'cron_attribute': 'self._cron' in cache_source,
        }
    except Exception as exc:
        result['InstalledChannelStopImplementationDiagnostic'] = f'INSPECTION_ERROR:{type(exc).__name__}'
    return result


def build_contract(run_id):
    return {
        'RunId': run_id,
        'ShutdownProbeArchitecture': 'PARENT_CHILD',
        'ChannelReady': 'NOT_RUN',
        'DisconnectCompleted': 'NOT_RUN',
        'TransportThreadExited': 'NOT_RUN',
        'SdkShutdownCompatibilityMode': 'NOT_RUN',
        'DeviceFlowCloseScheduled': 'NOT_RUN',
        'DeviceFlowCloseCompleted': 'NOT_RUN',
        'DeviceFlowCaptured': False,
        'DeviceFlowHttpPresentBefore': None,
        'DeviceFlowHttpOwnedBefore': None,
        'DeviceFlowHttpClosedBefore': None,
        'DeviceFlowPreCloseAttempted': False,
        'DeviceFlowPreCloseScheduled': False,
        'DeviceFlowPreCloseCompleted': False,
        'DeviceFlowPreCloseTimedOut': False,
        'DeviceFlowPreCloseCancelledAfterTimeout': False,
        'DeviceFlowPreCloseCancellationObserved': False,
        'DeviceFlowPreCloseElapsedMs': None,
        'DeviceFlowHttpPresentAfter': None,
        'DeviceFlowHttpClosedAfter': None,
        'DeviceFlowPreCloseErrorCode': None,
        'DeviceFlowShutdownCompatibilityMode': 'NOT_REQUIRED',
        'DeviceFlowObjectObserved': False,
        'DeviceFlowObjectSameAcrossPreCloseAndPublicStop': None,
        'DeviceFlowCloseInvocationCountObserved': False,
        'DeviceFlowPreCloseInvocationCount': None,
        'DeviceFlowPublicStopCloseInvocationCount': None,
        'DeviceFlowCloseKind': 'NOT_OBSERVED',
        'DeviceFlowCloseCoroutineCreated': False,
        'DeviceFlowCloseCoroutineScheduled': False,
        'DeviceFlowCloseTaskCaptured': False,
        'DeviceFlowCloseTaskDone': False,
        'DeviceFlowCloseTaskCancelled': False,
        'DeviceFlowCloseAwaitedToTerminal': False,
        'DeviceFlowRawCoroutineLeak': None,
        'DeviceFlowCloseOwnerLoopObserved': False,
        'DeviceFlowCloseOwnerLoopRunning': None,
        'DeviceFlowCloseOwnerLoopClosed': None,
        'DeviceFlowCloseAwaited': False,
        'DeviceFlowCloseDone': False,
        'DeviceFlowCloseCancelled': False,
        'DeviceFlowCloseTimedOut': False,
        'DeviceFlowCloseTerminalEvidence': 'NOT_OBSERVED',
        'DeviceFlowCloseCleanupMechanism': 'NOT_OBSERVED',
        'DeviceFlowCloseOwnerLoopIsWsLoop': False,
        'DeviceFlowCloseOwnerLoopIsBgLoop': False,
        'DeviceFlowCloseOwnerLoopIsCacheLoop': False,
        'DeviceFlowCloseOwnerLoopIsTransportLoop': False,
        'SdkWsTasksDrained': 'NOT_RUN',
        'SdkCacheTasksDrained': 'NOT_RUN',
        'SdkWsTasksBefore': 'NOT_RUN',
        'SdkWsTasksRemaining': 'NOT_RUN',
        'SdkCacheTasksBefore': 'NOT_RUN',
        'SdkCacheTasksRemaining': 'NOT_RUN',
        'SdkDeviceFlowTasksBefore': 'NOT_RUN',
        'SdkDeviceFlowTasksDrained': 'NOT_RUN',
        'SdkDeviceFlowTasksRemaining': 'NOT_RUN',
        'RemainingTaskNames': [],
        'PreShutdownCapture': 'NOT_RUN',
        'StartFutureCaptured': False,
        'StartFutureWrapperCancelled': False,
        'StartFutureExited': False,
        'StartWorkerTerminalAuthority': 'NOT_OBSERVED',
        'StartWorkerTerminalWaitAttempted': False,
        'StartWorkerExited': False,
        'StartWorkerTerminalTimedOut': False,
        'StartWorkerTerminalEvidence': 'NOT_RUN',
        'BgOwnershipHandoffAttempted': False,
        'BgSchedulingBlockedBeforeDetach': False,
        'BgOwnershipHandoffCompleted': False,
        'BgOwnershipDetachedFromSdk': False,
        'BgProducerQuiescenceContract': 'NOT_RUN',
        'LateBgTaskCreatedAfterHandoff': False,
        'StartWorkerTerminalBeforeBgDrain': False,
        'BgFinalDrainAttempted': False,
        'BgFinalDrainTasksBefore': [],
        'BgFinalDrainTasksRemaining': [],
        'BgFinalDrainTerminalEvidence': 'NOT_RUN',
        'BgCapturedLoopStopAllowed': False,
        'Host072914LateSleepRaceRegression': 'NOT_RUN',
        'WsClientCaptured': False,
        'WsLoopCaptured': False,
        'WsLoopRunningBeforePublicStop': None,
        'WsLoopClosedBeforePublicStop': None,
        'WsTasksBeforePublicStop': [],
        'WsTasksAfterPublicStop': [],
        'CacheCronCaptured': False,
        'CacheLoopRunningBefore': None,
        'CacheLoopClosedBefore': None,
        'CacheLoopSameAsWsLoop': None,
        'CacheCronTaskDoneBefore': None,
        'CacheLoopClosedByCfr': False,
        'WsTaskObservationAvailable': None,
        'CacheTaskObservationAvailable': None,
        'BgLoopCaptured': False,
        'BgThreadCaptured': False,
        'BgLoopRunningBeforePublicStop': None,
        'BgLoopClosedBeforePublicStop': None,
        'BgThreadAliveBeforePublicStop': None,
        'BgThreadExited': None,
        'BgTaskObservationAvailable': None,
        'BgTasksBeforePublicStop': [],
        'BgTasksAfterPublicStop': [],
        'BgTasksDrained': 'NOT_RUN',
        'BgTasksRemaining': 'NOT_RUN',
        'BgRemainingTaskNames': [],
        'BgAsyncGeneratorsShutdown': 'NOT_RUN',
        'BgDefaultExecutorShutdown': 'NOT_RUN',
        'BgLoopClosedByCfr': False,
        'BgLoopClosedAfterPublicStop': None,
        'BgLoopClosureState': 'NOT_CAPTURED',
        'BgTaskObservationStatus': 'NOT_REQUIRED',
        'BgTaskTerminalEvidence': 'NOT_YET_EVALUATED',
        'AsyncGeneratorsShutdown': 'NOT_RUN',
        'InstalledSdkVersion': None,
        'InstalledChannelStopImplementationHash': None,
        'InstalledChannelStopImplementationDiagnostic': 'NOT_RUN',
        'InstalledWsClientShape': {},
        'InstalledExpiringCacheShape': {},
        'RuntimeWarnings': [],
        'PendingTaskWarnings': [],
        'AsyncioSleepPendingWarningDetected': False,
        'DeviceFlowPublicStopTimeoutWarningDetected': False,
        'DeviceFlowPostExitPendingWarningDetected': False,
        'DeviceFlowPostExitNeverAwaitedWarningDetected': False,
        'AccessKeyLeak': False,
        'TicketLeak': False,
        'SecretsRecorded': False,
        'ChildExitCode': None,
        'Verdict': 'NOT_RUN',
        'BlockingIssues': ['HOST_SHUTDOWN_PROBE_NOT_RUN'],
        'NonBlockingDiagnostics': [],
    }


def warning_lines(text):
    lines = (text or '').splitlines()
    runtime = [line for line in lines if any(token.lower() in line.lower() for token in BLOCKER_TOKENS)]
    pending = [line for line in lines if re.search(r'Task was destroyed|pending task|was never awaited', line, re.I)]
    return runtime, pending


def final_clean_shutdown_authority(result):
    return (
        result['ChildExitCode'] == 0
        and result['RuntimeWarnings'] == []
        and result['PendingTaskWarnings'] == []
        and result['RemainingTaskNames'] == []
        and result['SdkWsTasksRemaining'] == 0
        and result['SdkCacheTasksRemaining'] == 0
        and result['SdkDeviceFlowTasksRemaining'] == 0
        and result['BgThreadExited'] is True
        and result['BgLoopClosedAfterPublicStop'] is True
        and result['BgLoopClosureState'] == 'CLOSED_BY_UPSTREAM_CLEANLY'
        and result['BgTaskTerminalEvidence'] == 'POST_EXIT_PROCESS_CLEAN'
        and result['StartWorkerExited'] is True
        and result['BgProducerQuiescenceContract'] == 'PASS'
        and result['BgFinalDrainTasksRemaining'] == []
    )


def _child_result(run_id, artifact_dir, timeout):
    result = build_contract(run_id)
    result.pop('Verdict', None)  # the parent is the only process that judges.
    result['BlockingIssues'] = []
    result.update(sdk_install_diagnostic())
    lease = None
    transport = None
    try:
        settings = load_settings(database=ROOT / 'cfr.sqlite3')
        settings.validate_connection()
        lease = FeishuConnectionLease(settings.database, f'feishu:{settings.app_namespace}', ttl=30, owner_instance_id=f'm2-shutdown-child-{run_id}').acquire()
        transport = ChannelFeishuTransport(settings, connect_timeout=timeout, disconnect_timeout=5.0)
        try:
            transport.connect_until_ready(timeout=timeout)
            result['ChannelReady'] = 'PASS' if transport.connection_state == 'ready' else 'FAIL'
        except Exception as exc:
            result['ChannelReady'] = 'FAIL'
            result['BlockingIssues'].append(getattr(exc, 'code', 'FEISHU_CHANNEL_UNKNOWN'))
        finally:
            transport.stop()
            result['DisconnectCompleted'] = 'PASS' if transport._disconnect_completed.is_set() else 'FAIL'
            result['TransportThreadExited'] = 'PASS' if transport.wait_until_stopped(0) and not (transport._thread and transport._thread.is_alive()) else 'FAIL'
            diagnostic = transport.shutdown_diagnostic
            result['SdkShutdownCompatibilityMode'] = diagnostic.compatibility_mode
            result['DeviceFlowCloseScheduled'] = 'PASS' if transport._device_flow_close_scheduled else 'NOT_REQUIRED'
            result['DeviceFlowCloseCompleted'] = diagnostic.device_flow_close_completed
            result['DeviceFlowCaptured'] = diagnostic.device_flow_captured
            result['DeviceFlowHttpPresentBefore'] = diagnostic.device_flow_http_present_before
            result['DeviceFlowHttpOwnedBefore'] = diagnostic.device_flow_http_owned_before
            result['DeviceFlowHttpClosedBefore'] = diagnostic.device_flow_http_closed_before
            result['DeviceFlowPreCloseAttempted'] = diagnostic.device_flow_preclose_attempted
            result['DeviceFlowPreCloseScheduled'] = diagnostic.device_flow_preclose_scheduled
            result['DeviceFlowPreCloseCompleted'] = diagnostic.device_flow_preclose_completed
            result['DeviceFlowPreCloseTimedOut'] = diagnostic.device_flow_preclose_timed_out
            result['DeviceFlowPreCloseCancelledAfterTimeout'] = diagnostic.device_flow_preclose_cancelled_after_timeout
            result['DeviceFlowPreCloseCancellationObserved'] = diagnostic.device_flow_preclose_cancellation_observed
            result['DeviceFlowPreCloseElapsedMs'] = diagnostic.device_flow_preclose_elapsed_ms
            result['DeviceFlowHttpPresentAfter'] = diagnostic.device_flow_http_present_after
            result['DeviceFlowHttpClosedAfter'] = diagnostic.device_flow_http_closed_after
            result['DeviceFlowPreCloseErrorCode'] = diagnostic.device_flow_preclose_error_code
            result['DeviceFlowShutdownCompatibilityMode'] = diagnostic.device_flow_shutdown_compatibility_mode
            result['DeviceFlowObjectObserved'] = diagnostic.device_flow_object_observed
            result['DeviceFlowObjectSameAcrossPreCloseAndPublicStop'] = diagnostic.device_flow_object_same_across_preclose_and_public_stop
            result['DeviceFlowCloseInvocationCountObserved'] = diagnostic.device_flow_close_invocation_count_observed
            result['DeviceFlowPreCloseInvocationCount'] = diagnostic.device_flow_preclose_invocation_count
            result['DeviceFlowPublicStopCloseInvocationCount'] = diagnostic.device_flow_public_stop_close_invocation_count
            result['DeviceFlowCloseKind'] = diagnostic.device_flow_close_kind
            result['DeviceFlowCloseCoroutineCreated'] = diagnostic.device_flow_close_coroutine_created
            result['DeviceFlowCloseCoroutineScheduled'] = diagnostic.device_flow_close_coroutine_scheduled
            result['DeviceFlowCloseTaskCaptured'] = diagnostic.device_flow_close_task_captured
            result['DeviceFlowCloseTaskDone'] = diagnostic.device_flow_close_task_done
            result['DeviceFlowCloseTaskCancelled'] = diagnostic.device_flow_close_task_cancelled
            result['DeviceFlowCloseAwaitedToTerminal'] = diagnostic.device_flow_close_awaited_to_terminal
            result['DeviceFlowRawCoroutineLeak'] = diagnostic.device_flow_raw_coroutine_leak
            result['DeviceFlowCloseOwnerLoopObserved'] = diagnostic.device_flow_close_owner_loop_observed
            result['DeviceFlowCloseOwnerLoopRunning'] = diagnostic.device_flow_close_owner_loop_running
            result['DeviceFlowCloseOwnerLoopClosed'] = diagnostic.device_flow_close_owner_loop_closed
            result['DeviceFlowCloseAwaited'] = diagnostic.device_flow_close_awaited
            result['DeviceFlowCloseDone'] = diagnostic.device_flow_close_done
            result['DeviceFlowCloseCancelled'] = diagnostic.device_flow_close_cancelled
            result['DeviceFlowCloseTimedOut'] = diagnostic.device_flow_close_timed_out
            result['DeviceFlowCloseTerminalEvidence'] = diagnostic.device_flow_close_terminal_evidence
            result['DeviceFlowCloseCleanupMechanism'] = diagnostic.device_flow_close_cleanup_mechanism
            result['DeviceFlowCloseOwnerLoopIsWsLoop'] = diagnostic.device_flow_owner_loop_is_ws_loop
            result['DeviceFlowCloseOwnerLoopIsBgLoop'] = diagnostic.device_flow_owner_loop_is_bg_loop
            result['DeviceFlowCloseOwnerLoopIsCacheLoop'] = diagnostic.device_flow_owner_loop_is_cache_loop
            result['DeviceFlowCloseOwnerLoopIsTransportLoop'] = diagnostic.device_flow_owner_loop_is_transport_loop
            result['SdkWsTasksBefore'] = diagnostic.sdk_ws_tasks_before
            result['SdkWsTasksDrained'] = diagnostic.sdk_ws_tasks_drained
            result['SdkWsTasksRemaining'] = diagnostic.sdk_ws_tasks_remaining
            result['SdkCacheTasksBefore'] = diagnostic.sdk_cache_tasks_before
            result['SdkCacheTasksDrained'] = diagnostic.sdk_cache_tasks_drained
            result['SdkCacheTasksRemaining'] = diagnostic.sdk_cache_tasks_remaining
            result['SdkDeviceFlowTasksBefore'] = diagnostic.sdk_device_flow_tasks_before
            result['SdkDeviceFlowTasksDrained'] = diagnostic.sdk_device_flow_tasks_drained
            result['SdkDeviceFlowTasksRemaining'] = diagnostic.sdk_device_flow_tasks_remaining
            result['RemainingTaskNames'] = list(diagnostic.remaining_task_names)
            result['PreShutdownCapture'] = diagnostic.pre_shutdown_capture
            result['StartFutureCaptured'] = diagnostic.start_future_captured
            result['StartFutureWrapperCancelled'] = diagnostic.start_future_wrapper_cancelled
            result['StartFutureExited'] = diagnostic.start_future_exited
            result['StartWorkerTerminalAuthority'] = diagnostic.start_worker_terminal_authority
            result['StartWorkerTerminalWaitAttempted'] = diagnostic.start_worker_terminal_wait_attempted
            result['StartWorkerExited'] = diagnostic.start_worker_exited
            result['StartWorkerTerminalTimedOut'] = diagnostic.start_worker_terminal_timed_out
            result['StartWorkerTerminalEvidence'] = diagnostic.start_worker_terminal_evidence
            result['BgOwnershipHandoffAttempted'] = diagnostic.bg_ownership_handoff_attempted
            result['BgSchedulingBlockedBeforeDetach'] = diagnostic.bg_scheduling_blocked_before_detach
            result['BgOwnershipHandoffCompleted'] = diagnostic.bg_ownership_handoff_completed
            result['BgOwnershipDetachedFromSdk'] = diagnostic.bg_ownership_detached_from_sdk
            result['BgProducerQuiescenceContract'] = diagnostic.bg_producer_quiescence_contract
            result['LateBgTaskCreatedAfterHandoff'] = diagnostic.late_bg_task_created_after_handoff
            result['StartWorkerTerminalBeforeBgDrain'] = diagnostic.start_worker_terminal_before_bg_drain
            result['BgFinalDrainAttempted'] = diagnostic.bg_final_drain_attempted
            result['BgFinalDrainTasksBefore'] = list(diagnostic.bg_final_drain_tasks_before)
            result['BgFinalDrainTasksRemaining'] = list(diagnostic.bg_final_drain_tasks_remaining)
            result['BgFinalDrainTerminalEvidence'] = diagnostic.bg_final_drain_terminal_evidence
            result['BgCapturedLoopStopAllowed'] = diagnostic.bg_captured_loop_stop_allowed
            result['Host072914LateSleepRaceRegression'] = diagnostic.host_072914_late_sleep_race_regression
            result['WsClientCaptured'] = diagnostic.ws_client_captured
            result['WsLoopCaptured'] = diagnostic.ws_loop_captured
            result['WsLoopRunningBeforePublicStop'] = diagnostic.ws_loop_running_before_public_stop
            result['WsLoopClosedBeforePublicStop'] = diagnostic.ws_loop_closed_before_public_stop
            result['WsTasksBeforePublicStop'] = list(diagnostic.ws_tasks_before_public_stop)
            result['WsTasksAfterPublicStop'] = list(diagnostic.ws_tasks_after_public_stop)
            result['CacheCronCaptured'] = diagnostic.cache_cron_captured
            result['CacheLoopRunningBefore'] = diagnostic.cache_loop_running_before
            result['CacheLoopClosedBefore'] = diagnostic.cache_loop_closed_before
            result['CacheLoopSameAsWsLoop'] = diagnostic.cache_loop_same_as_ws_loop
            result['CacheCronTaskDoneBefore'] = diagnostic.cache_cron_task_done_before
            result['CacheLoopClosedByCfr'] = diagnostic.cache_loop_closed_by_cfr
            result['WsTaskObservationAvailable'] = diagnostic.ws_task_observation_available
            result['CacheTaskObservationAvailable'] = diagnostic.cache_task_observation_available
            result['BgLoopCaptured'] = diagnostic.bg_loop_captured
            result['BgThreadCaptured'] = diagnostic.bg_thread_captured
            result['BgLoopRunningBeforePublicStop'] = diagnostic.bg_loop_running_before_public_stop
            result['BgLoopClosedBeforePublicStop'] = diagnostic.bg_loop_closed_before_public_stop
            result['BgThreadAliveBeforePublicStop'] = diagnostic.bg_thread_alive_before_public_stop
            result['BgThreadExited'] = diagnostic.bg_thread_exited
            result['BgTaskObservationAvailable'] = diagnostic.bg_task_observation_available
            result['BgTasksBeforePublicStop'] = list(diagnostic.bg_tasks_before_public_stop)
            result['BgTasksAfterPublicStop'] = list(diagnostic.bg_tasks_after_public_stop)
            result['BgTasksDrained'] = diagnostic.bg_tasks_drained
            result['BgTasksRemaining'] = diagnostic.bg_tasks_remaining
            result['BgRemainingTaskNames'] = list(diagnostic.bg_remaining_task_names)
            result['BgAsyncGeneratorsShutdown'] = diagnostic.bg_async_generators_shutdown
            result['BgDefaultExecutorShutdown'] = diagnostic.bg_default_executor_shutdown
            result['BgLoopClosedByCfr'] = diagnostic.bg_loop_closed_by_cfr
            result['BgLoopClosedAfterPublicStop'] = diagnostic.bg_loop_closed_after_public_stop
            result['BgLoopClosureState'] = diagnostic.bg_loop_closure_state
            result['BgTaskObservationStatus'] = diagnostic.bg_task_observation_status
            result['BgTaskTerminalEvidence'] = diagnostic.bg_task_terminal_evidence
            result['AsyncGeneratorsShutdown'] = 'PASS' if transport._async_generators_shutdown else 'FAIL'
            result['BlockingIssues'].extend(transport._shutdown_blocking_issues)
    except Exception as exc:
        result['BlockingIssues'].append(getattr(exc, 'code', type(exc).__name__))
    finally:
        if lease is not None:
            lease.release()
    result['BlockingIssues'] = sorted(set(result['BlockingIssues']))
    safe = sanitize_feishu_log_text(json.dumps(result, indent=2, ensure_ascii=False))
    (artifact_dir / 'child_result.json').write_text(safe, encoding='utf-8')
    # No final Verdict is emitted by the child. The parent owns process-exit
    # and post-exit warning evaluation.
    print(json.dumps({'ChildResultPath': str(artifact_dir / 'child_result.json')}))
    return 0 if result['ChannelReady'] == 'PASS' and result['DisconnectCompleted'] == 'PASS' and result['TransportThreadExited'] == 'PASS' else 2


def _parent_run(run_id, artifact_dir, timeout):
    command = [sys.executable, str(Path(__file__).resolve()), '--child', '--run-id', run_id, '--artifact-dir', str(artifact_dir), '--timeout', str(timeout)]
    try:
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=max(timeout * 2.0, timeout + 10.0))
        child_exit = completed.returncode
        stdout = completed.stdout or ''
        stderr = completed.stderr or ''
    except subprocess.TimeoutExpired as exc:
        child_exit = -1
        stdout = exc.stdout or ''
        stderr = (exc.stderr or '') + '\nChild process timeout'
    child_path = artifact_dir / 'child_result.json'
    try:
        child = json.loads(child_path.read_text(encoding='utf-8'))
    except Exception:
        child = build_contract(run_id)
        child['BlockingIssues'] = ['CHILD_RESULT_MISSING']
    result = build_contract(run_id)
    result.update({key: value for key, value in child.items() if key != 'RunId'})
    result['RunId'] = run_id
    result['ChildExitCode'] = child_exit
    raw_output = f'{stdout}\n{stderr}'
    runtime, pending = warning_lines(raw_output)
    result['RuntimeWarnings'] = runtime
    result['PendingTaskWarnings'] = pending
    result['AsyncioSleepPendingWarningDetected'] = bool(re.search(r'(?i)(?:coro=<sleep|coroutine [\'\"]sleep[\'\"]|sleep\(\) was never awaited)', raw_output))
    result['DeviceFlowPublicStopTimeoutWarningDetected'] = bool(re.search(r'(?i)device_flow\.close timed out', raw_output))
    result['DeviceFlowPostExitPendingWarningDetected'] = bool(re.search(r'(?i)(?:Task was destroyed.*DeviceFlowClient\.close|DeviceFlowClient\.close.*(?:pending task|Task was destroyed))', raw_output, re.S))
    result['DeviceFlowPostExitNeverAwaitedWarningDetected'] = bool(re.search(r'(?i)(?:DeviceFlowClient\.close.*was never awaited|coroutine [\'\"]DeviceFlowClient\.close[\'\"] was never awaited)', raw_output, re.S))
    result['AccessKeyLeak'] = bool(SENSITIVE_ASSIGNMENT.search(raw_output))
    result['TicketLeak'] = bool(re.search(r'(?i)ticket\s*(?:=|:)\s*(?!<redacted>)[^\s&,}]+', raw_output))
    result['SecretsRecorded'] = bool(re.search(r'(?i)(?:app_secret|tenant_access_token|user_access_token|authorization)\s*(?:=|:)\s*(?!<redacted>)[^\s&,}]+', raw_output))
    issues = list(result.get('BlockingIssues') or [])
    if child_exit != 0:
        issues.append(f'CHILD_EXIT_CODE:{child_exit}')
    if runtime:
        issues.append('POST_EXIT_RUNTIME_WARNING')
    if pending:
        issues.append('POST_EXIT_PENDING_TASK_WARNING')
    if result['AsyncioSleepPendingWarningDetected']:
        issues.append('ASYNCIO_SLEEP_PENDING_WARNING')
    if result['DeviceFlowPublicStopTimeoutWarningDetected']:
        issues.append('DEVICE_FLOW_PUBLIC_STOP_TIMEOUT_WARNING')
    if result['DeviceFlowPostExitPendingWarningDetected']:
        issues.append('DEVICE_FLOW_POST_EXIT_PENDING_WARNING')
    if result['DeviceFlowPostExitNeverAwaitedWarningDetected']:
        issues.append('DEVICE_FLOW_POST_EXIT_NEVER_AWAITED_WARNING')
    if result['DeviceFlowPreCloseTimedOut']:
        issues.append('DEVICE_FLOW_PRECLOSE_TIMEOUT')
    post_exit_clean = not runtime and not pending and not result['AsyncioSleepPendingWarningDetected'] and child_exit == 0
    if result['BgLoopClosureState'] == 'CLOSED_BY_UPSTREAM_CLEANLY':
        result['BgTaskTerminalEvidence'] = 'POST_EXIT_PROCESS_CLEAN' if post_exit_clean else 'POST_EXIT_PROCESS_UNCLEAN'
        if not post_exit_clean:
            result['BgLoopClosureState'] = 'CLOSED_UNCLEANLY'
            result['BgTaskObservationStatus'] = 'CLOSED_WITH_FAILURE_EVIDENCE'
            issues.append('CHANNEL_BG_LOOP_CLOSED_UNCLEANLY')
    if result['AccessKeyLeak']:
        issues.append('ACCESS_KEY_LEAK')
    if result['TicketLeak']:
        issues.append('TICKET_LEAK')
    if result['SecretsRecorded']:
        issues.append('SECRET_RECORDED')
    for key in ('SdkWsTasksRemaining', 'SdkCacheTasksRemaining', 'SdkDeviceFlowTasksRemaining'):
        if isinstance(result.get(key), int) and result[key] != 0:
            issues.append(f'{key.upper()}_NONZERO')
    if 'SDK_TASK_DRAIN_ERROR:RuntimeError' in issues and final_clean_shutdown_authority(result):
        issues = [issue for issue in issues if issue != 'SDK_TASK_DRAIN_ERROR:RuntimeError']
        diagnostics = list(result.get('NonBlockingDiagnostics') or [])
        diagnostics.append('SDK_TASK_DRAIN_TRANSIENT_RUNTIME_ERROR')
        result['NonBlockingDiagnostics'] = sorted(set(diagnostics))
    result['BlockingIssues'] = sorted(set(issues))
    clean = (
        child_exit == 0
        and result['ChannelReady'] == 'PASS'
        and result['DisconnectCompleted'] == 'PASS'
        and result['TransportThreadExited'] == 'PASS'
        and result['PreShutdownCapture'] == 'PASS'
        and (not result['StartFutureCaptured'] or result['StartFutureExited'])
        and (not result['StartFutureCaptured'] or result['StartFutureExited'])
        and (not result['BgOwnershipHandoffAttempted'] or (result['StartWorkerExited'] and result['StartWorkerTerminalEvidence'] == 'START_FUTURE_NATURAL_COMPLETION'))
        and (not result['BgOwnershipHandoffAttempted'] or result['StartWorkerTerminalTimedOut'] is False)
        and (not result['BgOwnershipHandoffAttempted'] or result['BgOwnershipHandoffCompleted'])
        and (not result['BgOwnershipHandoffAttempted'] or result['BgSchedulingBlockedBeforeDetach'])
        and (not result['BgOwnershipHandoffAttempted'] or result['BgOwnershipDetachedFromSdk'])
        and (not result['BgOwnershipHandoffAttempted'] or result['BgProducerQuiescenceContract'] == 'PASS')
        and (not result['BgOwnershipHandoffAttempted'] or result['StartWorkerTerminalBeforeBgDrain'])
        and (not result['BgOwnershipHandoffAttempted'] or result['BgFinalDrainTerminalEvidence'] not in {'NOT_RUN', 'CFR_FINAL_DRAIN_PRODUCER_NOT_QUIESCENT'})
        and (not result['BgOwnershipHandoffAttempted'] or result['BgCapturedLoopStopAllowed'])
        and (not result['BgOwnershipHandoffAttempted'] or result['Host072914LateSleepRaceRegression'] == 'PASS')
        and (
            (not result['DeviceFlowCaptured'])
            or (result['DeviceFlowPreCloseCompleted'] and not result['DeviceFlowPreCloseTimedOut'])
        )
        and not result['DeviceFlowPublicStopTimeoutWarningDetected']
        and not result['DeviceFlowPostExitPendingWarningDetected']
        and not result['DeviceFlowPostExitNeverAwaitedWarningDetected']
        and (not result['WsClientCaptured'] or result['WsLoopCaptured'])
        and result['WsTaskObservationAvailable'] is True
        and result['CacheTaskObservationAvailable'] is True
        and result['BgLoopCaptured'] is True
        and result['BgThreadCaptured'] is True
        and result['BgThreadExited'] is True
        and result['BgLoopClosureState'] in {'CLOSED_BY_CFR', 'CLOSED_BY_UPSTREAM_CLEANLY'}
        and result['BgTaskObservationStatus'] in {'OBSERVED', 'CLOSED_CLEANLY_BY_UPSTREAM'}
        and result['BgLoopClosedAfterPublicStop'] is True
        and (
            (result['BgTaskObservationStatus'] == 'OBSERVED' and result['BgTaskObservationAvailable'] is True and result['BgTasksRemaining'] == 0 and result['BgLoopClosedByCfr'] is True and result['BgAsyncGeneratorsShutdown'] == 'PASS')
            or
            (result['BgTaskObservationStatus'] == 'CLOSED_CLEANLY_BY_UPSTREAM' and result['BgTasksRemaining'] is None and result['BgAsyncGeneratorsShutdown'] in {'NOT_OBSERVABLE', 'NOT_REQUIRED'})
        )
        and result['DeviceFlowCloseCompleted'] in {'PASS', 'NOT_OBSERVABLE', 'NOT_REQUIRED'}
        and result['SdkWsTasksRemaining'] == 0
        and result['SdkCacheTasksRemaining'] == 0
        and result['SdkDeviceFlowTasksRemaining'] == 0
        and result['AsyncGeneratorsShutdown'] == 'PASS'
        and not result['RuntimeWarnings']
        and not result['PendingTaskWarnings']
        and not result['AccessKeyLeak']
        and not result['TicketLeak']
        and not result['SecretsRecorded']
        and result['BgTaskTerminalEvidence'] == 'POST_EXIT_PROCESS_CLEAN'
        and not result['BlockingIssues']
    )
    result['Verdict'] = 'PASS' if clean else 'FAIL'
    if clean:
        result['BlockingIssues'] = []
    (artifact_dir / 'probe.log').write_text(sanitize_feishu_log_text(raw_output), encoding='utf-8')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--contract-only', action='store_true')
    parser.add_argument('--child', action='store_true')
    parser.add_argument('--run-id')
    parser.add_argument('--artifact-dir')
    args = parser.parse_args(argv)
    run_id = args.run_id or datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    artifact_dir = Path(args.artifact_dir) if args.artifact_dir else ROOT / '.tmp' / 'm2-feishu-shutdown' / run_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    if args.child:
        return _child_result(run_id, artifact_dir, args.timeout)
    if args.contract_only:
        result = build_contract(run_id)
        (artifact_dir / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps({'ArtifactDir': str(artifact_dir), **result}))
        return 0
    result = _parent_run(run_id, artifact_dir, args.timeout)
    safe_result = sanitize_feishu_log_text(json.dumps(result, indent=2, ensure_ascii=False))
    (artifact_dir / 'result.json').write_text(safe_result, encoding='utf-8')
    print(json.dumps({'ArtifactDir': str(artifact_dir), **result}, ensure_ascii=False))
    return 0 if result['Verdict'] == 'PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
