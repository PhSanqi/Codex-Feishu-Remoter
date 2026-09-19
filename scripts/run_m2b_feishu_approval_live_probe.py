"""Host-only M2B approval probe with real evidence boundaries.

The probe is intentionally fail-closed. It never treats requested thread
options, a JSON-RPC response, or a card click count as evidence of a different
event. It also never reads schema JSON from stdout: Codex writes schema files
under the requested output directory.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cfr.codex.app_server import AppServerClient, AppServerRpcError
from cfr.codex.launcher import CodexLauncher
from cfr.codex.threads import ThreadManager
from cfr.codex.turns import TurnManager
from cfr.feishu.approvals import ApprovalBridge, first_present
from cfr.feishu.config import load_settings
from cfr.feishu.connection import FeishuConnectionLease
from cfr.feishu.log_sanitize import sanitize_feishu_log_text
from cfr.feishu.models import FeishuCardAction, FeishuExecutionContext
from cfr.feishu.replies import FeishuReplyClient
from cfr.feishu.store import FeishuStore
from cfr.feishu.transport import ChannelFeishuTransport


ALLOW_PROMPT = """CFR_M2B_ALLOW_PROBE:

Create exactly one file in the current temporary workspace:
allow_probe.txt

Use a protected operation that writes these exact UTF-8 bytes, with no BOM,
newline, or trailing whitespace:
`python -c "from pathlib import Path; Path('allow_probe.txt').write_bytes(b'CFR_M2B_ALLOW_OK')"`

The file bytes must be exactly:
`CFR_M2B_ALLOW_OK`

Do not modify anything else.
Do not access the network.
Do not inspect unrelated files.

If permission is required, request approval.
"""

DECLINE_PROMPT = """CFR_M2B_DECLINE_PROBE:

Create exactly one file in the current temporary workspace:
decline_probe.txt

Its entire contents must be:
CFR_M2B_DECLINE_SHOULD_NOT_EXIST

Do not modify anything else.
Do not access the network.
Do not inspect unrelated files.

If permission is required, request approval.
"""

APPROVAL_METHODS = ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval')
ALLOW_MARKER_BYTES = b'CFR_M2B_ALLOW_OK'
DECLINE_MARKER_BYTES = b'CFR_M2B_DECLINE_SHOULD_NOT_EXIST'


def safe(value, limit=1000):
    return sanitize_feishu_log_text(str(value))[:limit]


def _safe_scalar(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return '<non-scalar>'


def _first_present_with_source(mapping, *keys):
    if not isinstance(mapping, dict):
        return None, None
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key], key
    return None, None


def _structured_probe_stdout(line):
    """Structured child relay JSON is protocol data, not warning evidence."""
    try:
        payload = json.loads(line)
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and any(key in payload for key in (
        'ArtifactDir', 'RunId', 'Mode', 'Verdict', 'BusinessVerdict',
        'FinalVerdict', 'PostExitRuntimeWarnings', 'PostExitPendingTaskWarnings',
    ))


def post_exit_warning_evidence(stdout_text='', stderr_text=None):
    """Scan raw stderr plus non-protocol stdout for post-exit lifecycle warnings."""
    if stderr_text is None:
        # Backward-compatible single-stream calls are treated as raw stderr.
        stderr_text = ''
    stdout_lines = [line for line in str(stdout_text).splitlines() if line.strip() and not _structured_probe_stdout(line)]
    stderr_lines = [line for line in str(stderr_text).splitlines() if line.strip()]
    lines = [sanitize_feishu_log_text(line).strip() for line in stdout_lines + stderr_lines]
    runtime = [line for line in lines if re.search(r'RuntimeWarning|was never awaited|ResourceWarning', line, re.I)]
    pending = [line for line in lines if re.search(r'Task was destroyed but it is pending|pending task|pending$', line, re.I)]
    event_loop = [line for line in lines if re.search(r'event loop is closed|ExpiringCache\.__del__|_start_clear_cron', line, re.I)]
    device_flow = [line for line in lines if re.search(r'DeviceFlowClient\.close timed out|device_flow\.close timed out', line, re.I)]
    return {
        'PostExitRuntimeWarnings': runtime,
        'PostExitPendingTaskWarnings': pending,
        'PostExitEventLoopClosedErrors': event_loop,
        'PostExitDeviceFlowWarnings': device_flow,
    }


def transport_teardown_evidence(transport):
    if transport is None:
        return {
            'TransportDisconnectCompleted': False,
            'TransportThreadExited': False,
            'TransportShutdownBlockingIssues': ['TRANSPORT_NOT_CREATED'],
            'DeviceFlowCloseTerminalEvidence': 'NOT_OBSERVED',
        }
    completed = bool(getattr(transport, '_disconnect_completed', None) and transport._disconnect_completed.is_set())
    thread = getattr(transport, '_thread', None)
    exited = thread is None or not thread.is_alive()
    diagnostic = getattr(transport, 'shutdown_diagnostic', None)
    issues = list(getattr(diagnostic, 'blocking_issues', ()) or ())
    return {
        'TransportDisconnectCompleted': completed,
        'TransportThreadExited': exited,
        'TransportShutdownBlockingIssues': issues,
        'DeviceFlowCloseCompleted': diagnostic.device_flow_close_completed,
        'DeviceFlowCaptured': diagnostic.device_flow_captured,
        'DeviceFlowPreCloseAttempted': diagnostic.device_flow_preclose_attempted,
        'DeviceFlowPreCloseScheduled': diagnostic.device_flow_preclose_scheduled,
        'DeviceFlowPreCloseCompleted': diagnostic.device_flow_preclose_completed,
        'DeviceFlowPreCloseTimedOut': diagnostic.device_flow_preclose_timed_out,
        'DeviceFlowPreCloseCancelledAfterTimeout': diagnostic.device_flow_preclose_cancelled_after_timeout,
        'DeviceFlowPreCloseCancellationObserved': diagnostic.device_flow_preclose_cancellation_observed,
        'DeviceFlowPreCloseErrorCode': diagnostic.device_flow_preclose_error_code,
        'DeviceFlowObjectObserved': diagnostic.device_flow_object_observed,
        'DeviceFlowObjectSameAcrossPreCloseAndPublicStop': diagnostic.device_flow_object_same_across_preclose_and_public_stop,
        'DeviceFlowCloseInvocationCountObserved': diagnostic.device_flow_close_invocation_count_observed,
        'DeviceFlowPreCloseInvocationCount': diagnostic.device_flow_preclose_invocation_count,
        'DeviceFlowPublicStopCloseInvocationCount': diagnostic.device_flow_public_stop_close_invocation_count,
        'DeviceFlowCloseKind': diagnostic.device_flow_close_kind,
        'DeviceFlowCloseCoroutineCreated': diagnostic.device_flow_close_coroutine_created,
        'DeviceFlowCloseCoroutineScheduled': diagnostic.device_flow_close_coroutine_scheduled,
        'DeviceFlowCloseTaskCaptured': diagnostic.device_flow_close_task_captured,
        'DeviceFlowCloseTaskDone': diagnostic.device_flow_close_task_done,
        'DeviceFlowCloseTaskCancelled': diagnostic.device_flow_close_task_cancelled,
        'DeviceFlowCloseAwaitedToTerminal': diagnostic.device_flow_close_awaited_to_terminal,
        'DeviceFlowRawCoroutineLeak': diagnostic.device_flow_raw_coroutine_leak,
        'DeviceFlowCloseOwnerLoopObserved': diagnostic.device_flow_close_owner_loop_observed,
        'DeviceFlowCloseOwnerLoopRunning': diagnostic.device_flow_close_owner_loop_running,
        'DeviceFlowCloseOwnerLoopClosed': diagnostic.device_flow_close_owner_loop_closed,
        'DeviceFlowCloseScheduled': diagnostic.device_flow_close_scheduled,
        'DeviceFlowCloseAwaited': diagnostic.device_flow_close_awaited,
        'DeviceFlowCloseDone': diagnostic.device_flow_close_done,
        'DeviceFlowCloseCancelled': diagnostic.device_flow_close_cancelled,
        'DeviceFlowCloseTimedOut': diagnostic.device_flow_close_timed_out,
        'DeviceFlowCloseTerminalEvidence': diagnostic.device_flow_close_terminal_evidence,
        'DeviceFlowCloseCleanupMechanism': diagnostic.device_flow_close_cleanup_mechanism,
        'DeviceFlowCloseOwnerLoopIsWsLoop': diagnostic.device_flow_owner_loop_is_ws_loop,
        'DeviceFlowCloseOwnerLoopIsBgLoop': diagnostic.device_flow_owner_loop_is_bg_loop,
        'DeviceFlowCloseOwnerLoopIsCacheLoop': diagnostic.device_flow_owner_loop_is_cache_loop,
        'DeviceFlowCloseOwnerLoopIsTransportLoop': diagnostic.device_flow_owner_loop_is_transport_loop,
        'BgPreStopDrainAttempted': diagnostic.bg_pre_stop_drain_attempted,
        'BgPreStopDrainMechanism': diagnostic.bg_pre_stop_drain_mechanism,
        'BgPreStopLoopRunning': diagnostic.bg_pre_stop_loop_running,
        'BgPreStopThreadAlive': diagnostic.bg_pre_stop_thread_alive,
        'BgPreStopTrackedFutureCount': diagnostic.bg_pre_stop_tracked_future_count,
        'BgPreStopTrackedFuturesCancelled': diagnostic.bg_pre_stop_tracked_futures_cancelled,
        'BgPreStopTasksBefore': list(diagnostic.bg_pre_stop_tasks_before),
        'BgPreStopTasksCancelled': diagnostic.bg_pre_stop_tasks_cancelled,
        'BgPreStopTasksRemaining': list(diagnostic.bg_pre_stop_tasks_remaining),
        'BgPreStopDrainCompleted': diagnostic.bg_pre_stop_drain_completed,
        'BgPreStopDrainTimedOut': diagnostic.bg_pre_stop_drain_timed_out,
        'BgPreStopTerminalEvidence': diagnostic.bg_pre_stop_terminal_evidence,
        'BgPreStopLoopStopAllowed': diagnostic.bg_pre_stop_loop_stop_allowed,
        'BgPreStopErrorCode': diagnostic.bg_pre_stop_error_code,
        'BgPreStopSdkCancelHelperBypassed': diagnostic.bg_pre_stop_drain_mechanism != 'SDK_SLEEP_BARRIER' if diagnostic.bg_pre_stop_drain_attempted else False,
        'BgPreStopRunningLoopTerminalDrain': diagnostic.bg_pre_stop_drain_mechanism == 'CFR_LOOP_CALLBACK_TERMINAL_DRAIN' and diagnostic.bg_pre_stop_drain_completed,
        'BgPreStopNoSleepCoroutineBarrier': diagnostic.bg_pre_stop_drain_mechanism != 'SDK_SLEEP_BARRIER' if diagnostic.bg_pre_stop_drain_attempted else False,
        'BgPreStopCancellationResistantTaskTerminal': diagnostic.bg_pre_stop_drain_completed and not diagnostic.bg_pre_stop_tasks_remaining,
        'BgPreStopLoopStopRequiresTerminal': diagnostic.bg_pre_stop_loop_stop_allowed == diagnostic.bg_pre_stop_drain_completed and not diagnostic.bg_pre_stop_tasks_remaining,
        'StartFutureCaptured': diagnostic.start_future_captured,
        'StartFutureWrapperCancelled': diagnostic.start_future_wrapper_cancelled,
        'StartWorkerTerminalAuthority': diagnostic.start_worker_terminal_authority,
        'StartWorkerTerminalWaitAttempted': diagnostic.start_worker_terminal_wait_attempted,
        'StartWorkerExited': diagnostic.start_worker_exited,
        'StartWorkerTerminalTimedOut': diagnostic.start_worker_terminal_timed_out,
        'StartWorkerTerminalEvidence': diagnostic.start_worker_terminal_evidence,
        'BgOwnershipHandoffAttempted': diagnostic.bg_ownership_handoff_attempted,
        'BgSchedulingBlockedBeforeDetach': diagnostic.bg_scheduling_blocked_before_detach,
        'BgOwnershipHandoffCompleted': diagnostic.bg_ownership_handoff_completed,
        'BgOwnershipDetachedFromSdk': diagnostic.bg_ownership_detached_from_sdk,
        'BgProducerQuiescenceContract': diagnostic.bg_producer_quiescence_contract,
        'LateBgTaskCreatedAfterHandoff': diagnostic.late_bg_task_created_after_handoff,
        'StartWorkerTerminalBeforeBgDrain': diagnostic.start_worker_terminal_before_bg_drain,
        'BgFinalDrainAttempted': diagnostic.bg_final_drain_attempted,
        'BgFinalDrainTasksBefore': list(diagnostic.bg_final_drain_tasks_before),
        'BgFinalDrainTasksRemaining': list(diagnostic.bg_final_drain_tasks_remaining),
        'BgFinalDrainTerminalEvidence': diagnostic.bg_final_drain_terminal_evidence,
        'BgCapturedLoopStopAllowed': diagnostic.bg_captured_loop_stop_allowed,
        'Host072914LateSleepRaceRegression': diagnostic.host_072914_late_sleep_race_regression,
    }


def classify_approval_live_teardown(evidence):
    """Classify lifecycle evidence independently from the business result."""
    started = bool(evidence.get('TransportLifecycleStarted'))
    child_exit = evidence.get('ChildExitCode')
    if not started:
        return 'FAIL' if child_exit not in (None, 0, 2) else 'NOT_RUN'
    if child_exit not in (0, 2):
        return 'FAIL'
    if evidence.get('TransportDisconnectCompleted') is not True or evidence.get('TransportThreadExited') is not True:
        return 'FAIL'
    if evidence.get('TransportShutdownBlockingIssues'):
        return 'FAIL'
    for key, value in evidence.items():
        if key.endswith('Remaining') and isinstance(value, (int, float)) and value > 0:
            return 'FAIL'
        if key.endswith('WarningDetected') and value:
            return 'FAIL'
    if evidence.get('BgThreadExited') is False or evidence.get('BgLoopClosureState') == 'CLOSED_UNCLEANLY':
        return 'FAIL'
    if evidence.get('DeviceFlowObjectObserved') and evidence.get('DeviceFlowCloseTerminalEvidence') != 'PASS':
        return 'FAIL'
    if evidence.get('DeviceFlowRawCoroutineLeak') or evidence.get('DeviceFlowCloseTimedOut'):
        return 'FAIL'
    if evidence.get('DeviceFlowPreCloseCompleted') is True and evidence.get('DeviceFlowPublicStopCloseInvocationCount') not in (0, None):
        return 'FAIL'
    if evidence.get('CacheCronCaptured') and evidence.get('CacheCronTerminalEvidence') != 'PASS':
        return 'FAIL'
    if any(evidence.get(key) for key in ('AccessKeyLeak', 'TicketLeak', 'SecretsRecorded')):
        return 'FAIL'
    warning_lists = ('PostExitRuntimeWarnings', 'PostExitPendingTaskWarnings', 'PostExitEventLoopClosedErrors', 'PostExitDeviceFlowWarnings')
    if any(evidence.get(key) for key in warning_lists):
        return 'FAIL'
    return 'PASS'


def finalize_approval_live_verdict(business_verdict, teardown_verdict, blocking_issues=()):
    """Compose final evidence without allowing either domain to mask the other."""
    issues = list(blocking_issues)
    if teardown_verdict == 'FAIL':
        issues.append('FEISHU_APPROVAL_LIVE_TEARDOWN_FAILED')
    return {
        'BusinessVerdict': business_verdict,
        'ApprovalLiveTeardownVerdict': teardown_verdict,
        'FinalVerdict': 'PASS' if business_verdict == 'PASS' and teardown_verdict == 'PASS' else 'FAIL',
        'BlockingIssues': sorted(set(issues)),
    }


def _schema_text(schema_dir: Path):
    parts = []
    files = sorted(schema_dir.rglob('*.json'))
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        except Exception:
            encoded = path.read_text(encoding='utf-8', errors='replace')
        parts.append(f'{path.relative_to(schema_dir)}\n{encoded}')
    return '\n'.join(parts)[:400000], files


def _schema_command(launcher, schema_dir: Path, experimental: bool):
    args = ['app-server', 'generate-json-schema', '--out', str(schema_dir)]
    if experimental:
        args.append('--experimental')
    return subprocess.run(
        launcher.build_command(*args),
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=60,
    )


def schema_probe(artifact_dir: Path, launcher):
    schema_dir = artifact_dir / 'schema'
    schema_dir.mkdir(parents=True, exist_ok=True)
    attempts = []
    selected = None
    schema_files = []
    for experimental in (True, False):
        try:
            completed = _schema_command(launcher, schema_dir, experimental)
        except FileNotFoundError:
            return {
                'SchemaCommand': 'FAIL', 'SchemaOutputDirectory': str(schema_dir), 'SchemaFileCount': 0,
                'SchemaUsesOutDirectory': True,
                'SchemaError': 'CODEX_NOT_AVAILABLE', 'ApprovalSchemaVerified': False,
                'CommandApprovalSchemaPresent': False, 'FileApprovalSchemaPresent': False,
                'ApprovalPolicyFieldPresent': False, 'ApprovalsReviewerFieldPresent': False,
                'SandboxFieldPresent': False, 'ApprovalDecisionAcceptPresent': False,
                'ApprovalDecisionDeclinePresent': False, 'ApprovalDecisionCancelPresent': False,
                '_schema_index_text': '',
            }
        output = sanitize_feishu_log_text((completed.stdout or '') + (completed.stderr or ''))[-20000:]
        attempts.append({'experimental': experimental, 'returncode': completed.returncode, 'output': output})
        selected = completed
        schema_index, schema_files = _schema_text(schema_dir)
        if completed.returncode == 0 and schema_files:
            break
    schema_index, schema_files = _schema_text(schema_dir)
    lowered = schema_index.lower()
    result = {
        'SchemaCommand': 'PASS' if selected and selected.returncode == 0 and schema_files else 'FAIL',
        'SchemaOutputDirectory': str(schema_dir),
        'SchemaUsesOutDirectory': True,
        'SchemaFileCount': len(schema_files),
        'SchemaError': None if selected and selected.returncode == 0 and schema_files else 'CODEX_SCHEMA_OUTPUT_NOT_CREATED',
        'CommandApprovalSchemaPresent': all(method.lower() in lowered for method in APPROVAL_METHODS),
        'FileApprovalSchemaPresent': 'item/filechange/requestapproval' in lowered,
        'ApprovalPolicyFieldPresent': 'approvalpolicy' in lowered,
        'ApprovalsReviewerFieldPresent': 'approvalsreviewer' in lowered,
        'SandboxFieldPresent': re.search(r'\bsandbox\b', lowered) is not None,
        'ApprovalDecisionAcceptPresent': re.search(r'\baccept\b', lowered) is not None,
        'ApprovalDecisionDeclinePresent': re.search(r'\bdecline\b', lowered) is not None,
        'ApprovalDecisionCancelPresent': re.search(r'\bcancel\b', lowered) is not None,
        '_schema_index_text': schema_index,
    }
    result['ApprovalSchemaVerified'] = result['SchemaCommand'] == 'PASS' and all(result[key] for key in (
        'CommandApprovalSchemaPresent', 'FileApprovalSchemaPresent', 'ApprovalPolicyFieldPresent',
        'ApprovalsReviewerFieldPresent', 'SandboxFieldPresent', 'ApprovalDecisionAcceptPresent',
        'ApprovalDecisionDeclinePresent', 'ApprovalDecisionCancelPresent',
    ))
    (artifact_dir / 'schema_command.log').write_text(json.dumps(attempts, indent=2), encoding='utf-8')
    return result


def approval_rows(database):
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute('select * from feishu_approvals order by created_at').fetchall()]
    finally:
        connection.close()


def _thread_payload(raw):
    if not isinstance(raw, dict):
        return {}
    return raw.get('thread') or raw


def _field(payload, name):
    if name in payload:
        return payload[name]
    snake = re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()
    return payload.get(snake)


def _requested_thread_params(schema_text, workspace):
    lowered = schema_text.lower()
    params = {'cwd': str(workspace), 'ephemeral': True, 'threadSource': 'user'}
    requested = {
        'ApprovalPolicyRequested': 'on-request' if 'approvalpolicy' in lowered else 'NOT_SUPPORTED',
        'ApprovalsReviewerRequested': 'user' if 'approvalsreviewer' in lowered else 'NOT_SUPPORTED',
        'SandboxRequested': 'read-only' if re.search(r'\bsandbox\b', lowered) else 'NOT_SUPPORTED',
    }
    if requested['ApprovalPolicyRequested'] != 'NOT_SUPPORTED':
        params['approvalPolicy'] = requested['ApprovalPolicyRequested']
    if requested['ApprovalsReviewerRequested'] != 'NOT_SUPPORTED':
        params['approvalsReviewer'] = requested['ApprovalsReviewerRequested']
    if requested['SandboxRequested'] != 'NOT_SUPPORTED':
        params['sandbox'] = requested['SandboxRequested']
    return params, requested


def _resolution_evidence(snapshot, request_id):
    resolved = list(snapshot.get('resolved', ()))
    inflight = list(snapshot.get('inflight', ()))
    normalized = '' if request_id is None else str(request_id)
    target_count = sum(1 for item in resolved if str(item) == normalized)
    contains_target = normalized in {str(item) for item in resolved}
    return {
        'DurableResolvedRequestIdMatched': contains_target,
        'ResolvedRequestIdSnapshotCount': target_count,
        'ResolvedIdsContainsRequest': contains_target,
        'InFlightEmpty': not inflight,
        'ResolutionSnapshotContract': 'PASS' if contains_target and not inflight else 'FAIL',
        'ResolutionSnapshot': {'resolved': [str(item) for item in resolved], 'inflight': [str(item) for item in inflight]},
    }


def _safe_notification(message):
    params = message.get('params') or {}
    raw_request_id, request_id_key = _first_present_with_source(params, 'requestId', 'request_id', 'id')
    if raw_request_id is None:
        raw_request_id, request_id_key = _first_present_with_source(message, 'id')
        request_id_source = f'top-level.{request_id_key}' if request_id_key else None
    else:
        request_id_source = f'params.{request_id_key}'
    raw_thread_id = first_present(params, 'threadId', 'thread_id')
    raw_turn_id = first_present(params, 'turnId', 'turn_id')
    return {
        'method': message.get('method'),
        'requestId': '' if raw_request_id is None else str(raw_request_id),
        'requestIdRaw': _safe_scalar(raw_request_id),
        'requestIdRawType': type(raw_request_id).__name__ if raw_request_id is not None else None,
        'requestIdSource': request_id_source,
        'threadId': '' if raw_thread_id is None else str(raw_thread_id),
        'turnId': '' if raw_turn_id is None else str(raw_turn_id),
        'topLevelKeys': sorted(str(key) for key in message.keys()) if isinstance(message, dict) else [],
        'paramKeys': sorted(str(key) for key in params.keys()) if isinstance(params, dict) else [],
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }


def resolved_notification_diagnostics(notifications, expected_request_id):
    resolved = [item for item in notifications if item.get('method') == 'serverRequest/resolved']
    observed_ids = [str(item['requestId']) for item in resolved if item.get('requestId') != '']
    expected = '' if expected_request_id is None else str(expected_request_id)
    target = next((item for item in resolved if item.get('requestId') == expected), None)
    first = resolved[0] if resolved else None
    return {
        'ExpectedServerRequestId': expected,
        'ExpectedServerRequestIdType': type(expected_request_id).__name__ if expected_request_id is not None else None,
        'ObservedResolvedRequestIdRaw': first.get('requestIdRaw') if first else None,
        'ObservedResolvedRequestIdRawType': first.get('requestIdRawType') if first else None,
        'ObservedResolvedRequestIdNormalized': first.get('requestId') if first else None,
        'ObservedResolvedRequestIdSource': first.get('requestIdSource') if first else None,
        'ObservedResolvedRequestIds': observed_ids,
        'ObservedResolvedRequestIdCount': len(observed_ids),
        'ResolvedNotificationMethod': first.get('method') if first else None,
        'ResolvedNotificationTopLevelKeys': first.get('topLevelKeys', []) if first else [],
        'ResolvedNotificationParamKeys': first.get('paramKeys', []) if first else [],
        'LiveResolvedRequestIdMatched': target is not None,
        'ResolvedNotificationEvidenceContract': 'PASS' if target is not None else 'FAIL',
    }


def apply_resolution_authority_evidence(result, notifications, snapshot, expected_request_id):
    """Merge live and durable resolution evidence without a shared authority key."""
    live = resolved_notification_diagnostics(notifications, expected_request_id)
    durable = _resolution_evidence(snapshot, expected_request_id)
    if 'ResolvedRequestIdMatched' in live or 'ResolvedRequestIdMatched' in durable:
        raise AssertionError('resolution authority collision: legacy aggregate must not be emitted by a source helper')
    result.update(live)
    result.update(durable)
    result['ResolvedRequestIdMatched'] = bool(
        result['LiveResolvedRequestIdMatched'] and result['DurableResolvedRequestIdMatched']
    )
    result['ResolvedRequestIdMatchedAuthority'] = 'AGGREGATE_LIVE_AND_DURABLE'
    result['ResolutionAuthoritiesSeparated'] = 'PASS'
    result['ResolutionExactlyOnceAuthorityContract'] = 'PASS' if (
        result['LiveResolvedRequestIdMatched']
        and result['DurableResolvedRequestIdMatched']
        and result['ResolvedRequestIdSnapshotCount'] == 1
        and result['InFlightEmpty']
    ) else 'FAIL'
    return result


def marker_byte_evidence(path, expected_bytes, prefix='Allow'):
    actual = path.read_bytes() if path.exists() else None
    expected = bytes(expected_bytes)
    actual_bytes = actual if actual is not None else b''
    crlf = actual_bytes.endswith(b'\r\n')
    lf = actual_bytes.endswith(b'\n') and not crlf
    trailing_whitespace = bool(actual_bytes and actual_bytes[-1:] in {b' ', b'\t'})
    bom = actual_bytes.startswith(b'\xef\xbb\xbf')
    escaped = lambda value: json.dumps(value.decode('utf-8', errors='backslashreplace'), ensure_ascii=False)
    exact = actual is not None and actual == expected
    return {
        f'{prefix}MarkerExpectedByteLength': len(expected),
        f'{prefix}MarkerActualByteLength': len(actual) if actual is not None else None,
        f'{prefix}MarkerExpectedSha256': hashlib.sha256(expected).hexdigest(),
        f'{prefix}MarkerActualSha256': hashlib.sha256(actual).hexdigest() if actual is not None else None,
        f'{prefix}MarkerUtf8BomPresent': bom,
        f'{prefix}MarkerTrailingLfPresent': lf,
        f'{prefix}MarkerTrailingCrLfPresent': crlf,
        f'{prefix}MarkerTrailingWhitespacePresent': trailing_whitespace,
        f'{prefix}MarkerExpectedEscaped': escaped(expected),
        f'{prefix}MarkerActualEscaped': escaped(actual_bytes) if actual is not None else None,
        f'{prefix}MarkerContentExact': exact,
        f'{prefix}MarkerExactByteContract': 'PASS' if exact else 'FAIL',
        f'{prefix}MarkerMismatchDiagnostics': 'PASS' if actual is not None else 'FAIL',
    }


def apply_marker_contract(result, path, expected_bytes, prefix='Allow'):
    """Build marker evidence while keeping absence distinct from byte mismatch."""
    result.update(marker_byte_evidence(path, expected_bytes, prefix))
    absent = not path.exists()
    result[f'{prefix}MarkerAbsent'] = absent
    if prefix == 'Decline':
        result['DeclineMarkerAbsenceContract'] = 'PASS' if absent else 'FAIL'
        if absent:
            result['DeclineMarkerExactByteContract'] = 'NOT_RUN'
            result['DeclineMarkerMismatchDiagnostics'] = 'NOT_RUN'
    return result


def _cache_owner_thread_evidence(loop):
    owner_id = getattr(loop, '_thread_id', None) if loop is not None else None
    current_id = threading.get_ident()
    owner_thread = next((thread for thread in threading.enumerate() if thread.ident == owner_id), None) if owner_id is not None else None
    return {
        'CacheCronOwnerThreadObserved': owner_id is not None,
        'CacheCronOwnerThreadAlive': bool(owner_thread and owner_thread.is_alive()),
        'CacheCronOwnerThreadIsCurrentThread': owner_id == current_id if owner_id is not None else False,
        'CacheCronOwnerThreadIdentityAvailable': owner_id is not None,
    }


def cache_cron_lifecycle_evidence(transport):
    capture = getattr(transport, '_shutdown_capture', None) if transport is not None else None
    diagnostic = getattr(transport, 'shutdown_diagnostic', None) if transport is not None else None
    task = getattr(capture, 'cache_cron_task', None) if capture is not None else None
    loop = getattr(capture, 'cache_loop', None) if capture is not None else None
    captured = bool(capture and capture.cache_cron_captured and task is not None)
    owner_thread = _cache_owner_thread_evidence(loop)
    done_after_public = bool(task.done()) if task is not None else None
    cancelled_after_public = bool(task.cancelled()) if task is not None and task.done() else None
    done_at_capture = getattr(capture, 'cache_cron_task_done_before_shutdown', None) if capture else None
    cancelled_at_capture = bool(task.cancelled()) if task is not None and done_at_capture else None
    exception_type = None
    exception_observed = False
    if task is not None and task.done() and not task.cancelled():
        try:
            exception = task.exception()
        except Exception as exc:
            exception = exc
        if exception is not None:
            exception_observed = True
            exception_type = type(exception).__name__
    terminal = 'PASS' if captured and done_after_public and not exception_observed else 'FAIL' if captured else 'NOT_RUN'
    if not captured:
        owner_loop_state = 'NOT_OBSERVED'
    elif getattr(capture, 'cache_loop_closed_before_shutdown', None):
        owner_loop_state = 'CLOSED_CLEANLY' if done_at_capture else 'CLOSED_UNCLEANLY'
    elif getattr(capture, 'cache_loop_running_before_shutdown', None):
        owner_loop_state = 'RUNNING_OWNED_LOOP'
    elif owner_thread['CacheCronOwnerThreadAlive']:
        owner_loop_state = 'OPEN_NONRUNNING_OWNED_LOOP'
    else:
        owner_loop_state = 'OPEN_NONRUNNING_ORPHAN_CACHE_LOOP'
    close_by_cfr = bool(diagnostic and getattr(diagnostic, 'cache_loop_closed_by_cfr', False))
    close_attempted = bool(captured and not getattr(capture, 'cache_loop_closed_before_shutdown', False) and diagnostic)
    if not captured:
        drain_mechanism = 'NOT_OBSERVABLE'
    elif done_at_capture:
        drain_mechanism = 'ALREADY_TERMINAL'
    elif getattr(capture, 'cache_loop_running_before_shutdown', False):
        drain_mechanism = 'OWNER_LOOP_THREADSAFE'
    elif terminal == 'PASS':
        drain_mechanism = 'CFR_DRIVE_NONRUNNING_LOOP'
    else:
        drain_mechanism = 'FAIL_CLOSED'
    evidence = {
        'CacheCronCaptured': captured,
        'CacheCronOwnerLoopObserved': bool(loop is not None),
        'CacheCronOwnerLoopState': owner_loop_state,
        **owner_thread,
        'CacheCronOwnerLoopRunningAtCapture': getattr(capture, 'cache_loop_running_before_shutdown', None) if capture else None,
        'CacheCronOwnerLoopClosedAtCapture': getattr(capture, 'cache_loop_closed_before_shutdown', None) if capture else None,
        'CacheCronOwnerLoopIsWsLoop': getattr(capture, 'cache_loop_same_as_ws_loop', None) if capture else None,
        'CacheCronOwnerLoopIsBgLoop': bool(capture and capture.cache_loop is not None and capture.cache_loop is getattr(capture, 'bg_loop', None)),
        'CacheCronOwnerLoopIsTransportLoop': bool(capture and capture.cache_loop is not None and capture.cache_loop is getattr(transport, '_loop', None)),
        'CacheCronDoneAtCapture': done_at_capture,
        'CacheCronCancelledAtCapture': cancelled_at_capture,
        'CacheCronDoneAfterPublicStop': done_after_public,
        'CacheCronCancelledAfterPublicStop': cancelled_after_public,
        'CacheCronDrainAttempted': bool(captured and diagnostic),
        'CacheCronDrainMechanism': drain_mechanism,
        'CacheCronDrainCompleted': terminal == 'PASS',
        'CacheCronDoneAfterDrain': done_after_public,
        'CacheCronCancelledAfterDrain': cancelled_after_public,
        'CacheCronDoneBeforeChildReturn': done_after_public,
        'CacheCronCancelledBeforeChildReturn': cancelled_after_public,
        'CacheCronAwaitedToTerminal': terminal == 'PASS',
        'CacheCronTerminalExceptionObserved': exception_observed,
        'CacheCronTerminalExceptionType': exception_type,
        'CacheCronTerminalEvidence': terminal,
        'ApprovalLiveCacheTeardownContract': terminal,
        'CacheOwnerLoopCloseAttempted': close_attempted,
        'CacheOwnerLoopClosedByCfr': close_by_cfr,
        'CacheOwnerLoopClosureState': 'CLOSED_BY_CFR' if close_by_cfr else (
            'CLOSED_CLEANLY' if loop is not None and loop.is_closed() and terminal == 'PASS' else
            'CLOSED_UNCLEANLY' if loop is not None and loop.is_closed() else 'OPEN_AFTER_DRAIN'
        ),
    }
    return evidence


def _mode_result(mode):
    prefix = 'Allow' if mode == 'allow' else 'Decline'
    return {
        'Mode': mode,
        'PendingObserved': False,
        'MarkerAbsentBeforeDecision': False,
        'ServerRequestId': None,
        'ApprovalId': None,
        'ServerRequestObservedCount': 0,
        'CardActionAcceptedCount': 0,
        'StoredDecision': None,
        'ApprovalFeedbackAckAttempted': False,
        'ApprovalFeedbackAckSucceeded': False,
        'ApprovalFeedbackOriginalCardUpdated': False,
        'ApprovalFeedbackActiveButtonsAfterAck': None,
        'ApprovalFeedbackStateAfterDecision': None,
        'ApprovalFeedbackFinalUpdateAttempted': False,
        'ApprovalFeedbackFinalUpdateSucceeded': False,
        'ApprovalFeedbackStateFinal': None,
        'ApprovalFeedbackActiveButtonsFinal': None,
        'ApprovalFeedbackFinalizationAuthority': None,
        'ApprovalFeedbackTurnIdObserved': None,
        'ApprovalFeedbackThreadIdObserved': None,
        'ApprovalFeedbackTurnStatusObserved': None,
        'ApprovalFeedbackTurnBindingMatchedCount': 0,
        'ApprovalFeedbackFinalizationTriggered': False,
        'ApprovalFeedbackFinalizationTriggerSource': None,
        'ApprovalFeedbackFinalizationObserved': False,
        'ApprovalFeedbackFinalizationWaitTimedOut': False,
        'ApprovalFeedbackMonotonicStateContract': 'NOT_RUN',
        'ApprovalFeedbackUxVerdict': 'NOT_RUN',
        'ResolvedRequestIdMatched': False,
        'ResolvedRequestIdMatchedAuthority': 'AGGREGATE_LIVE_AND_DURABLE',
        'LiveResolvedRequestIdMatched': False,
        'DurableResolvedRequestIdMatched': False,
        'ResolutionAuthoritiesSeparated': 'NOT_RUN',
        'ResolutionExactlyOnceAuthorityContract': 'NOT_RUN',
        'ResolvedIdsContainsRequest': False,
        'InFlightEmpty': False,
        'ResolvedRequestIdSnapshotCount': 0,
        'ResolutionSnapshotContract': 'NOT_RUN',
        'ServerRequestResolvedObserved': False,
        'ServerRequestResolvedEvidence': 'NOT_OBSERVED',
        'ExactlyOnce': False,
        'NoAutoApprove': False,
        f'{prefix}CardSent': False,
        f'{prefix}CardActionReceived': False,
        f'{prefix}OperatorValidated': False,
        f'{prefix}JsonRpcResponse': False,
        f'{prefix}ServerRequestResolved': False,
        f'{prefix}ItemCompleted': False,
        f'{prefix}MarkerCreated': False,
        f'{prefix}MarkerContentExact': False,
        f'{prefix}ItemDeclined': False,
        f'{prefix}MarkerAbsent': False,
        f'{prefix}MarkerExactByteContract': 'NOT_RUN',
        f'{prefix}MarkerMismatchDiagnostics': 'NOT_RUN',
        **({'DeclineMarkerAbsenceContract': 'NOT_RUN'} if mode == 'decline' else {}),
        'TurnTerminalState': None,
        'Verdict': 'NOT_RUN',
        'BlockingIssues': [],
    }


def run_mode(mode, artifact_dir, schema_result, launcher, operator_id, timeout):
    result = _mode_result(mode)
    mode_dir = artifact_dir / mode
    workspace = mode_dir / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    database = mode_dir / 'probe.sqlite3'
    settings = load_settings(database=database)
    store = None
    transport = None
    bridge = None
    client = None
    subscription = None
    watcher_stop = threading.Event()
    watcher_thread = None
    worker = None
    notifications = []
    actions = []
    requests = []
    context_holder = {'context': None}
    turn_result = {}
    turn_error = {}
    card_send_failure = None
    card_failure_row = None

    prompt = ALLOW_PROMPT if mode == 'allow' else DECLINE_PROMPT
    marker = workspace / ('allow_probe.txt' if mode == 'allow' else 'decline_probe.txt')

    try:
        store = FeishuStore(database)

        def card_handler(action: FeishuCardAction):
            actions.append({'approval_id': action.approval_id, 'operator_open_id': action.operator_open_id, 'action': action.action})
            return bridge.resolve(action.approval_id, action.operator_open_id, action.action)

        def server_request_handler(message):
            requests.append({'id': str(message.get('id')), 'method': message.get('method'), 'threadId': str((message.get('params') or {}).get('threadId') or '')})
            if context_holder['context'] is None:
                return {'decision': 'decline'}
            return bridge.handle_server_request(message, context_holder['context'])

        transport = ChannelFeishuTransport(settings, connect_timeout=30, disconnect_timeout=5)
        bridge = ApprovalBridge(store, FeishuReplyClient(transport, store), settings)
        result['TransportLifecycleStarted'] = True
        transport.connect_until_ready(message_handler=lambda *_: None, card_handler=card_handler, timeout=30)
        params, requested = _requested_thread_params(schema_result['_schema_index_text'], workspace)
        result.update(requested)
        client = AppServerClient(launcher=launcher, timeout=timeout, on_server_request=server_request_handler)
        client.start()
        subscription = client.subscribe(lambda message: message.get('method') in {'serverRequest/resolved', 'turn/completed', 'turn/interrupted'})

        def watcher():
            while not watcher_stop.is_set():
                try:
                    message = subscription.get(timeout=0.25)
                except queue.Empty:
                    continue
                evidence = _safe_notification(message)
                notifications.append(evidence)
                bridge.handle_server_notification(message)

        watcher_thread = threading.Thread(target=watcher, name=f'm2b-{mode}-resolution-watcher', daemon=True)
        watcher_thread.start()
        raw_thread = client.request('thread/start', params)
        thread_payload = _thread_payload(raw_thread)
        thread_ref = ThreadManager.ref_from_result(raw_thread, workspace)
        result['ThreadId'] = thread_ref.thread_id
        for field in ('approvalPolicy', 'approvalsReviewer', 'sandbox', 'model', 'modelProvider', 'cwd'):
            value = _field(thread_payload, field)
            if field == 'approvalPolicy':
                result['ApprovalPolicyEffective'] = value if value is not None else 'NOT_REPORTED'
            elif field == 'approvalsReviewer':
                result['ApprovalsReviewerEffective'] = value if value is not None else 'NOT_REPORTED'
            elif field == 'sandbox':
                result['SandboxEffective'] = value if value is not None else 'NOT_REPORTED'
            else:
                result[f'Thread{field[0].upper()}{field[1:]}'] = value
        context_holder['context'] = FeishuExecutionContext(f'm2b-{mode}', f'm2b-{mode}', operator_id, thread_ref.thread_id)

        def turn_worker():
            try:
                turn_result['value'] = TurnManager(client).run_turn(thread_ref.thread_id, prompt, timeout)
                terminal = turn_result['value']
                bridge.finalize_feedback_for_turn(
                    thread_id=terminal.thread_id,
                    turn_id=terminal.turn_id,
                    turn_status=terminal.status,
                    error=terminal.error_message,
                    trigger_source='TURN_RESULT',
                )
            except Exception as exc:
                turn_error['value'] = exc

        worker = threading.Thread(target=turn_worker, name=f'm2b-{mode}-turn-worker', daemon=True)
        worker.start()
        pending = None
        observation_deadline = time.monotonic() + 60
        while time.monotonic() < observation_deadline and worker.is_alive():
            rows = approval_rows(database)
            for candidate in reversed(rows):
                evidence = bridge.card_send_evidence(candidate['approval_id'])
                if evidence and evidence.get('FeishuSendSuccess') is False:
                    card_send_failure = evidence
                    card_failure_row = candidate
                    break
                reply = store.get_reply_record(f'm2b-{mode}', f'approval:{candidate["approval_id"]}', 0)
                if candidate['state'] == 'pending' and reply and reply['state'] == 'sent':
                    pending = candidate
                    break
            if pending or card_send_failure:
                break
            time.sleep(0.25)
        result['PendingObserved'] = pending is not None
        result['MarkerAbsentBeforeDecision'] = not marker.exists()
        result['NoAutoApprove'] = result['PendingObserved'] and result['MarkerAbsentBeforeDecision']
        if not pending:
            if card_send_failure:
                worker.join(timeout + 10)
                rows = approval_rows(database)
                if card_failure_row is not None:
                    card_failure_row = next((item for item in rows if item['approval_id'] == card_failure_row['approval_id']), card_failure_row)
                    result['ServerRequestId'] = str(card_failure_row['codex_request_id'])
                    result['ApprovalId'] = card_failure_row['approval_id']
                    result['StoredDecision'] = card_failure_row.get('decision')
                    result.update(bridge.card_contract_evidence(result['ApprovalId']))
                result['ServerRequestObservedCount'] = sum(1 for item in requests if item['id'] == result['ServerRequestId'] and item['method'] in APPROVAL_METHODS)
                result['FeishuSendSuccess'] = False
                result['FeishuErrorCode'] = card_send_failure.get('FeishuErrorCode')
                result['FeishuCardErrorCode'] = card_send_failure.get('FeishuCardErrorCode')
                result['FeishuErrorMessageSanitized'] = card_send_failure.get('FeishuErrorMessageSanitized')
                result['FailureDisposition'] = 'decline'
                result['FailureDispositionReason'] = 'FEISHU_CARD_DELIVERY_FAILED'
                result['FailureMarkerAbsent'] = not marker.exists()
                result['FailureRequestResolved'] = False
                if result['ServerRequestId']:
                    apply_resolution_authority_evidence(
                        result,
                        [],
                        client.server_request_resolution_snapshot(),
                        result['ServerRequestId'],
                    )
                    result['FailureRequestResolved'] = result['ResolvedIdsContainsRequest'] and result['InFlightEmpty']
                result['FailureCardSendFailed'] = True
                result['FailureClosedExactlyOnce'] = (
                    result['ServerRequestObservedCount'] == 1
                    and result['StoredDecision'] == 'decline'
                    and result['FailureRequestResolved']
                    and result['FailureMarkerAbsent']
                )
                result['Verdict'] = 'FAIL_CARD_SEND'
                result['BlockingIssues'] = ['FEISHU_APPROVAL_CARD_SEND_FAILED']
                if str(result['FeishuErrorCode']) == '230099' or str(result['FeishuCardErrorCode']) == '200861':
                    result['BlockingIssues'].append('FEISHU_APPROVAL_CARD_SCHEMA_REJECTED')
                return result, notifications
            print(f'CODEX_APPROVAL_REQUEST_NOT_OBSERVED\nMODE={mode}', flush=True)
            result['Verdict'] = 'CODEX_APPROVAL_REQUEST_NOT_OBSERVED'
            result['BlockingIssues'] = [result['Verdict']]
            return result, notifications
        result['ServerRequestId'] = str(pending['codex_request_id'])
        print(f'WAITING_FOR_FEISHU_APPROVAL\nMODE={mode}', flush=True)
        worker.join(timeout + 10)
        if worker.is_alive():
            result['Verdict'] = 'APPROVAL_ACTION_TIMEOUT'
            result['BlockingIssues'] = [result['Verdict']]
            return result, notifications
        if 'value' in turn_result:
            result['TurnTerminalState'] = turn_result['value'].status
        elif 'value' in turn_error:
            result['TurnTerminalState'] = 'failed'
        rows = approval_rows(database)
        row = next((item for item in rows if str(item['codex_request_id']) == result['ServerRequestId']), pending)
        final_observed = bridge.wait_for_final_feedback(row['approval_id'], timeout=10.0)
        result['ApprovalId'] = row['approval_id']
        result['StoredDecision'] = row.get('decision')
        result.update(bridge.card_contract_evidence(result['ApprovalId']))
        result.update(bridge.card_send_evidence(result['ApprovalId']))
        result.update(bridge.feedback_evidence(result['ApprovalId']))
        result['ApprovalFeedbackFinalizationObserved'] = final_observed and result.get('ApprovalFeedbackStateFinal') in {'APPROVED', 'DECLINED', 'EXECUTION_FAILED'}
        result['ApprovalFeedbackFinalizationWaitTimedOut'] = not result['ApprovalFeedbackFinalizationObserved']
        result['ServerRequestObservedCount'] = sum(1 for item in requests if item['id'] == result['ServerRequestId'] and item['method'] in APPROVAL_METHODS)
        matching_actions = [item for item in actions if item['approval_id'] == result['ApprovalId']]
        result['CardActionAcceptedCount'] = len(matching_actions)
        reply = store.get_reply_record(f'm2b-{mode}', f'approval:{result["ApprovalId"]}', 0)
        result[f'{"Allow" if mode == "allow" else "Decline"}CardSent'] = bool(reply and reply['state'] == 'sent')
        prefix = 'Allow' if mode == 'allow' else 'Decline'
        result[f'{prefix}CardActionReceived'] = result['CardActionAcceptedCount'] == 1
        result[f'{prefix}OperatorValidated'] = result[f'{prefix}CardActionReceived'] and matching_actions[0]['operator_open_id'] == operator_id
        result[f'{prefix}JsonRpcResponse'] = row.get('decision') == ('accept' if mode == 'allow' else 'decline')
        result[f'{prefix}ItemCompleted'] = result['TurnTerminalState'] == 'completed' if mode == 'allow' else False
        result[f'{prefix}ItemDeclined'] = row.get('state') in {'declined', 'expired'} and result['TurnTerminalState'] in {'completed', 'failed'}
        result[f'{prefix}MarkerCreated'] = marker.exists()
        expected_marker = ALLOW_MARKER_BYTES if mode == 'allow' else DECLINE_MARKER_BYTES
        apply_marker_contract(result, marker, expected_marker, prefix)
        resolved_notifications = [item for item in notifications if item.get('method') == 'serverRequest/resolved']
        result['ServerRequestResolvedObserved'] = bool(resolved_notifications)
        result['ServerRequestResolvedEvidence'] = 'REAL' if resolved_notifications else 'NOT_OBSERVED'
        snapshot = client.server_request_resolution_snapshot()
        apply_resolution_authority_evidence(result, notifications, snapshot, result['ServerRequestId'])
        result['ResolvedNotificationEvidenceContract'] = 'PASS' if (
            result['ServerRequestResolvedObserved'] and result['LiveResolvedRequestIdMatched']
        ) else 'FAIL'
        result[f'{prefix}JsonRpcResponse'] = (
            row.get('decision') == ('accept' if mode == 'allow' else 'decline')
            and result['ResolvedIdsContainsRequest']
            and result['InFlightEmpty']
        )
        result[f'{prefix}ServerRequestResolved'] = result['ServerRequestResolvedObserved']
        result['ExactlyOnce'] = (
            result['ServerRequestObservedCount'] == 1
            and result['CardActionAcceptedCount'] == 1
            and result['StoredDecision'] in {'accept', 'decline'}
            and result['ServerRequestResolvedObserved']
            and result['LiveResolvedRequestIdMatched']
            and result['DurableResolvedRequestIdMatched']
            and result['ResolvedRequestIdSnapshotCount'] == 1
            and result['ResolvedIdsContainsRequest']
            and result['InFlightEmpty']
        )
        result['ZeroIdExactlyOnceEvidenceContract'] = 'PASS' if result['ExactlyOnce'] else 'FAIL'
        required = [result['PendingObserved'], result['NoAutoApprove'], result['ExactlyOnce'], result[f'{prefix}CardSent'], result[f'{prefix}CardActionReceived'], result[f'{prefix}OperatorValidated'], result[f'{prefix}JsonRpcResponse'], result[f'{prefix}ServerRequestResolved']]
        if mode == 'allow':
            required.extend([result['AllowItemCompleted'], result['AllowMarkerCreated'], result['AllowMarkerContentExact']])
        else:
            required.extend([result['DeclineItemDeclined'], result['DeclineMarkerAbsent']])
        result['Verdict'] = 'PASS' if all(required) else 'FAIL'
        result['ApprovalFeedbackUxVerdict'] = 'PASS' if (
            result.get('ApprovalFeedbackAckSucceeded') is True
            and result.get('ApprovalFeedbackOriginalCardUpdated') is True
            and result.get('ApprovalFeedbackStateAfterDecision') == 'ACKNOWLEDGED_PROCESSING'
            and result.get('ApprovalFeedbackFinalUpdateSucceeded') is True
            and result.get('ApprovalFeedbackFinalizationObserved') is True
            and result.get('ApprovalFeedbackFinalizationWaitTimedOut') is False
            and result.get('ApprovalFeedbackActiveButtonsAfterAck') == 0
            and result.get('ApprovalFeedbackActiveButtonsFinal') == 0
            and result.get('ApprovalFeedbackStateFinal') == ('APPROVED' if mode == 'allow' else 'DECLINED')
            and result.get('ApprovalFeedbackMonotonicStateContract') == 'PASS'
        ) else 'FAIL'
        if result['Verdict'] == 'PASS' and result['ApprovalFeedbackUxVerdict'] != 'PASS':
            result['Verdict'] = 'FAIL'
            result['BlockingIssues'] = ['M2B_APPROVAL_DECISION_FEEDBACK_REQUIRED']
        elif result['Verdict'] != 'PASS':
            result['BlockingIssues'] = ['M2B_LIVE_EVIDENCE_INCOMPLETE']
        else:
            result['BlockingIssues'] = []
        return result, notifications
    except AppServerRpcError as exc:
        result['Verdict'] = 'CODEX_APPROVAL_LIVE_PROBE_NOT_DETERMINISTIC'
        result['BlockingIssues'] = [safe(exc)]
        return result, notifications
    except Exception as exc:
        result['Verdict'] = getattr(exc, 'code', type(exc).__name__)
        result['BlockingIssues'] = [safe(exc)]
        return result, notifications
    finally:
        watcher_stop.set()
        if subscription is not None:
            subscription.close()
        try:
            if watcher_thread is not None and watcher_thread.is_alive():
                watcher_thread.join(timeout=2.0)
            if worker is not None and worker.is_alive():
                worker.join(timeout=2.0)
            if bridge is not None:
                bridge.close()
            # Let the Feishu transport release its SDK-owned resources before
            # closing the Codex client and dropping the probe's references.
            if transport is not None:
                transport.stop()
                transport.wait_until_stopped(0)
                result.update(transport_teardown_evidence(transport))
                diagnostic = transport.shutdown_diagnostic
                result.update({
                    'PreShutdownCapture': diagnostic.pre_shutdown_capture,
                    'SdkWsTasksRemaining': diagnostic.sdk_ws_tasks_remaining,
                    'SdkCacheTasksRemaining': diagnostic.sdk_cache_tasks_remaining,
                    'SdkDeviceFlowTasksRemaining': diagnostic.sdk_device_flow_tasks_remaining,
                    'CacheCronCaptured': diagnostic.cache_cron_captured,
                    'CacheTaskObservationAvailable': diagnostic.cache_task_observation_available,
                    'CacheCronTaskDoneBefore': diagnostic.cache_cron_task_done_before,
                    'BgLoopCaptured': diagnostic.bg_loop_captured,
                    'BgThreadCaptured': diagnostic.bg_thread_captured,
                    'BgThreadExited': diagnostic.bg_thread_exited,
                    'BgLoopClosureState': diagnostic.bg_loop_closure_state,
                    'BgTasksRemaining': diagnostic.bg_tasks_remaining,
                    'BgRemainingTaskNames': list(diagnostic.bg_remaining_task_names),
                    'BgAsyncGeneratorsShutdown': diagnostic.bg_async_generators_shutdown,
                    'AsyncGeneratorsShutdown': 'PASS' if transport._async_generators_shutdown else 'FAIL',
                })
                result.update(cache_cron_lifecycle_evidence(transport))
        finally:
            if client is not None:
                client.close()
            if store is not None:
                store.close()


def contract_result(run_id, mode):
    result = {
        'RunId': run_id,
        'Mode': mode,
        'SchemaCommand': 'NOT_RUN',
        'SchemaOutputDirectory': 'NOT_RUN',
        'SchemaFileCount': 0,
        'CommandApprovalSchemaPresent': False,
        'FileApprovalSchemaPresent': False,
        'ApprovalPolicyFieldPresent': False,
        'ApprovalsReviewerFieldPresent': False,
        'SandboxFieldPresent': False,
        'ApprovalDecisionAcceptPresent': False,
        'ApprovalDecisionDeclinePresent': False,
        'ApprovalDecisionCancelPresent': False,
        'ApprovalSchemaVerified': False,
        'SchemaUsesOutDirectory': False,
        'ApprovalPolicyRequested': 'NOT_RUN',
        'ApprovalPolicyEffective': 'NOT_RUN',
        'ApprovalsReviewerRequested': 'NOT_RUN',
        'ApprovalsReviewerEffective': 'NOT_RUN',
        'SandboxRequested': 'NOT_RUN',
        'SandboxEffective': 'NOT_RUN',
        'PendingObserved': False,
        'MarkerAbsentBeforeDecision': False,
        'ServerRequestId': None,
        'ApprovalId': None,
        'ServerRequestObservedCount': 0,
        'CardActionAcceptedCount': 0,
        'ApprovalCardSchemaVersion': 'NOT_RUN',
        'ApprovalCardLegacyActionTagPresent': False,
        'ApprovalCardButtonCount': 0,
        'ApprovalCardAllowCallback': 'NOT_RUN',
        'ApprovalCardDeclineCallback': 'NOT_RUN',
        'ApprovalCardNoSecretFields': False,
        'ApprovalCardV2Contract': 'NOT_RUN',
        'FeishuSendSuccess': False,
        'FeishuMessageIdPresent': False,
        'FeishuErrorCode': None,
        'FeishuCardErrorCode': None,
        'FeishuErrorMessageSanitized': None,
        'FailureCardSendFailed': False,
        'FailureDisposition': None,
        'FailureDispositionReason': None,
        'FailureRequestResolved': False,
        'FailureMarkerAbsent': False,
        'FailureClosedExactlyOnce': False,
        'TransportDisconnectCompleted': False,
        'TransportThreadExited': False,
        'TransportLifecycleStarted': False,
        'TransportShutdownBlockingIssues': [],
        'PreShutdownCapture': 'NOT_RUN',
        'SdkWsTasksRemaining': 0,
        'SdkCacheTasksRemaining': 0,
        'SdkDeviceFlowTasksRemaining': 0,
        'CacheCronCaptured': False,
        'CacheTaskObservationAvailable': None,
        'CacheCronTaskDoneBefore': None,
        'BgLoopCaptured': False,
        'BgThreadCaptured': False,
        'BgThreadExited': None,
        'BgLoopClosureState': 'NOT_CAPTURED',
        'BgTasksRemaining': 0,
        'BgRemainingTaskNames': [],
        'BgAsyncGeneratorsShutdown': 'NOT_RUN',
        'AsyncGeneratorsShutdown': 'NOT_RUN',
        'BgPreStopDrainAttempted': False,
        'BgPreStopDrainMechanism': 'NOT_RUN',
        'BgPreStopLoopRunning': None,
        'BgPreStopThreadAlive': None,
        'BgPreStopTrackedFutureCount': 0,
        'BgPreStopTrackedFuturesCancelled': 0,
        'BgPreStopTasksBefore': [],
        'BgPreStopTasksCancelled': 0,
        'BgPreStopTasksRemaining': [],
        'BgPreStopDrainCompleted': False,
        'BgPreStopDrainTimedOut': False,
        'BgPreStopTerminalEvidence': 'NOT_RUN',
        'BgPreStopLoopStopAllowed': False,
        'BgPreStopErrorCode': None,
        'BgPreStopSdkCancelHelperBypassed': False,
        'BgPreStopRunningLoopTerminalDrain': False,
        'BgPreStopNoSleepCoroutineBarrier': False,
        'BgPreStopCancellationResistantTaskTerminal': False,
        'BgPreStopLoopStopRequiresTerminal': False,
        'ApprovalLiveProbeArchitecture': 'PARENT_CHILD',
        'PostExitRuntimeWarnings': [],
        'PostExitPendingTaskWarnings': [],
        'PostExitEventLoopClosedErrors': [],
        'PostExitDeviceFlowWarnings': [],
        'ExpectedServerRequestId': None,
        'ExpectedServerRequestIdType': None,
        'ObservedResolvedRequestIdRaw': None,
        'ObservedResolvedRequestIdRawType': None,
        'ObservedResolvedRequestIdNormalized': None,
        'ObservedResolvedRequestIdSource': None,
        'ObservedResolvedRequestIds': [],
        'ObservedResolvedRequestIdCount': 0,
        'ResolvedNotificationMethod': None,
        'ResolvedNotificationTopLevelKeys': [],
        'ResolvedNotificationParamKeys': [],
        'ResolvedRequestIdSnapshotCount': 0,
        'AllowMarkerExpectedByteLength': None,
        'AllowMarkerActualByteLength': None,
        'AllowMarkerExpectedSha256': None,
        'AllowMarkerActualSha256': None,
        'AllowMarkerUtf8BomPresent': False,
        'AllowMarkerTrailingLfPresent': False,
        'AllowMarkerTrailingCrLfPresent': False,
        'AllowMarkerTrailingWhitespacePresent': False,
        'AllowMarkerExpectedEscaped': None,
        'AllowMarkerActualEscaped': None,
        'AllowMarkerExactByteContract': 'NOT_RUN',
        'AllowMarkerMismatchDiagnostics': 'NOT_RUN',
        'DeclineMarkerExpectedByteLength': None,
        'DeclineMarkerActualByteLength': None,
        'DeclineMarkerExpectedSha256': None,
        'DeclineMarkerActualSha256': None,
        'DeclineMarkerUtf8BomPresent': False,
        'DeclineMarkerTrailingLfPresent': False,
        'DeclineMarkerTrailingCrLfPresent': False,
        'DeclineMarkerTrailingWhitespacePresent': False,
        'DeclineMarkerExpectedEscaped': None,
        'DeclineMarkerActualEscaped': None,
        'DeclineMarkerExactByteContract': 'NOT_RUN',
        'DeclineMarkerMismatchDiagnostics': 'NOT_RUN',
        'DeclineMarkerAbsenceContract': 'NOT_RUN',
        'CacheCronOwnerLoopObserved': False,
        'CacheCronOwnerLoopState': 'NOT_OBSERVED',
        'CacheCronOwnerThreadObserved': False,
        'CacheCronOwnerThreadAlive': False,
        'CacheCronOwnerThreadIsCurrentThread': False,
        'CacheCronOwnerThreadIdentityAvailable': False,
        'CacheCronOwnerLoopRunningAtCapture': None,
        'CacheCronOwnerLoopClosedAtCapture': None,
        'CacheCronOwnerLoopIsWsLoop': None,
        'CacheCronOwnerLoopIsBgLoop': False,
        'CacheCronOwnerLoopIsTransportLoop': False,
        'CacheCronDoneAtCapture': None,
        'CacheCronCancelledAtCapture': None,
        'CacheCronDoneAfterPublicStop': None,
        'CacheCronCancelledAfterPublicStop': None,
        'CacheCronDrainAttempted': False,
        'CacheCronDrainMechanism': 'NOT_OBSERVABLE',
        'CacheCronDrainCompleted': False,
        'CacheCronDoneAfterDrain': None,
        'CacheCronCancelledAfterDrain': None,
        'CacheCronDoneBeforeChildReturn': None,
        'CacheCronCancelledBeforeChildReturn': None,
        'CacheCronAwaitedToTerminal': False,
        'CacheCronTerminalExceptionObserved': False,
        'CacheCronTerminalExceptionType': None,
        'CacheCronTerminalEvidence': 'NOT_RUN',
        'CacheOwnerLoopCloseAttempted': False,
        'CacheOwnerLoopClosedByCfr': False,
        'CacheOwnerLoopClosureState': 'NOT_OBSERVED',
        'ApprovalLiveTeardownVerdict': 'NOT_RUN',
        'ApprovalLiveTeardownNotRunContract': 'NOT_RUN',
        'BusinessVerdict': 'NOT_RUN',
        'FinalVerdict': 'NOT_RUN',
        'ApprovalBusinessVerdictIndependentFromTeardown': 'NOT_RUN',
        'UnexpectedChildCrashFailClosed': 'NOT_RUN',
        'ChildPreExitFinalVerdictSuppressed': 'NOT_RUN',
        'ParentFinalTeardownAuthority': 'NOT_RUN',
        'StructuredStdoutWarningScannerIsolation': 'NOT_RUN',
        'RawStderrWarningAuthority': 'NOT_RUN',
        'ExpiringCachePendingDetection': 'NOT_RUN',
        'ApprovalLiveCacheTeardownContract': 'NOT_RUN',
        'StoredDecision': None,
        'ResolvedRequestIdMatched': False,
        'ResolvedRequestIdMatchedAuthority': 'AGGREGATE_LIVE_AND_DURABLE',
        'LiveResolvedRequestIdMatched': False,
        'DurableResolvedRequestIdMatched': False,
        'ResolutionAuthoritiesSeparated': 'NOT_RUN',
        'ResolutionExactlyOnceAuthorityContract': 'NOT_RUN',
        'ResolvedIdsContainsRequest': False,
        'InFlightEmpty': False,
        'ResolutionSnapshotContract': 'NOT_RUN',
        'ResolvedNotificationEvidenceContract': 'NOT_RUN',
        'ZeroIdExactlyOnceEvidenceContract': 'NOT_RUN',
        'AllowCardSent': False,
        'AllowCardActionReceived': False,
        'AllowOperatorValidated': False,
        'AllowJsonRpcResponse': False,
        'AllowServerRequestResolved': False,
        'AllowItemCompleted': False,
        'AllowMarkerCreated': False,
        'AllowMarkerContentExact': False,
        'DeclineCardSent': False,
        'DeclineCardActionReceived': False,
        'DeclineJsonRpcResponse': False,
        'DeclineItemDeclined': False,
        'DeclineMarkerAbsent': True,
        'ServerRequestResolvedEvidence': 'NOT_RUN',
        'ExactlyOnce': False,
        'NoAutoApprove': False,
        'SecretsRecorded': False,
        'Verdict': 'NOT_RUN',
        'BlockingIssues': ['LIVE_PROBE_NOT_RUN'],
    }
    return result


def _write_result(path: Path, result: dict[str, object], filename: str = 'result.json'):
    safe_result = sanitize_feishu_log_text(json.dumps(result, indent=2, ensure_ascii=False))
    path.mkdir(parents=True, exist_ok=True)
    (path / filename).write_text(safe_result, encoding='utf-8')
    return safe_result


def child_main(mode, timeout, artifact_dir, run_id):
    """Run one live mode; the parent remains the final shutdown authority."""
    mode_dir = artifact_dir / mode
    mode_dir.mkdir(parents=True, exist_ok=True)
    result = contract_result(run_id, mode)
    result['ApprovalLiveProbeArchitecture'] = 'PARENT_CHILD'
    events = []
    operator_ids = tuple(item.strip() for item in os.environ.get('CFR_FEISHU_ALLOWED_OPEN_IDS', '').replace(';', ',').split(',') if item.strip())
    settings = load_settings(database=mode_dir / 'probe.sqlite3')
    if not settings.credentials_present:
        result['Verdict'] = 'FEISHU_LIVE_SETUP_REQUIRED'
        result['BlockingIssues'] = ['FEISHU_LIVE_SETUP_REQUIRED']
    elif len(operator_ids) != 1:
        result['Verdict'] = 'FEISHU_OPERATOR_ALLOWLIST_EXACTLY_ONE_REQUIRED'
        result['BlockingIssues'] = ['FEISHU_OPERATOR_ALLOWLIST_EXACTLY_ONE_REQUIRED']
    else:
        lease = None
        try:
            settings.validate_connection()
            launcher = CodexLauncher()
            version = subprocess.run(launcher.build_command('--version'), capture_output=True, text=True, timeout=15)
            result['CodexVersion'] = safe((version.stdout or version.stderr).strip())
            schema_result = schema_probe(mode_dir, launcher)
            result.update({key: value for key, value in schema_result.items() if not key.startswith('_')})
            if not schema_result['ApprovalSchemaVerified']:
                result['Verdict'] = 'CODEX_APPROVAL_SCHEMA_NOT_VERIFIED'
                result['BlockingIssues'] = ['CODEX_APPROVAL_SCHEMA_NOT_VERIFIED']
            else:
                lease = FeishuConnectionLease(ROOT / 'cfr.sqlite3', f'feishu:{settings.app_namespace}', owner_instance_id=f'm2b-approval-{run_id}-{mode}').acquire()
                mode_result, mode_events = run_mode(mode, artifact_dir, schema_result, launcher, operator_ids[0], timeout)
                events.extend({'mode': mode, **event} for event in mode_events)
                result.update({key: value for key, value in mode_result.items() if key not in {'Mode', 'BlockingIssues'}})
                result['Verdict'] = mode_result['Verdict']
                result['BlockingIssues'] = list(mode_result.get('BlockingIssues', []))
        except Exception as exc:
            result['Verdict'] = getattr(exc, 'code', type(exc).__name__)
            result['BlockingIssues'] = [safe(exc)]
        finally:
            if lease is not None:
                lease.release()
    result['BusinessVerdict'] = result.get('Verdict', 'NOT_RUN')
    result['ChildBusinessVerdict'] = result['BusinessVerdict']
    result['ChildPreExitFinalVerdictSuppressed'] = 'PASS'
    result['ApprovalLiveTeardownVerdict'] = 'PENDING_PARENT_EXIT'
    result['FinalVerdict'] = 'PENDING_PARENT_EXIT'
    result['ApprovalLiveTeardownNotRunContract'] = 'PENDING_PARENT_EXIT'
    result['SecretsRecorded'] = False
    _write_result(mode_dir, result, 'child_result.json')
    (mode_dir / 'events.jsonl').write_text('\n'.join(json.dumps(event, ensure_ascii=False) for event in events) + ('\n' if events else ''), encoding='utf-8')
    print(json.dumps({'ArtifactDir': str(mode_dir), **result}, ensure_ascii=False), flush=True)
    return 0 if result['Verdict'] == 'PASS' else 2


def _relay_child(mode, timeout, artifact_dir, run_id):
    command = [sys.executable, str(Path(__file__).resolve()), '--child', '--mode', mode, '--timeout', str(timeout), '--artifact-dir', str(artifact_dir), '--run-id', run_id]
    process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace', bufsize=1)
    output_queue = queue.Queue()
    captured = {'stdout': [], 'stderr': []}

    def reader(name, stream):
        try:
            for line in stream:
                output_queue.put((name, line))
        finally:
            output_queue.put((name, None))

    threads = [threading.Thread(target=reader, args=(name, stream), daemon=True) for name, stream in (('stdout', process.stdout), ('stderr', process.stderr))]
    for thread in threads:
        thread.start()
    completed_readers = 0
    while completed_readers < len(threads):
        try:
            name, line = output_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        if line is None:
            completed_readers += 1
            continue
        captured[name].append(line)
        if name == 'stdout':
            print(line, end='', flush=True)
    return_code = process.wait()
    mode_dir = artifact_dir / mode
    (mode_dir / 'child_stdout.log').write_text(''.join(captured['stdout']), encoding='utf-8')
    (mode_dir / 'child_stderr.log').write_text(''.join(captured['stderr']), encoding='utf-8')
    try:
        child = json.loads((mode_dir / 'child_result.json').read_text(encoding='utf-8'))
    except Exception:
        child = contract_result(run_id, mode)
        child['Verdict'] = 'APPROVAL_LIVE_CHILD_RESULT_MISSING'
        child['BlockingIssues'] = ['APPROVAL_LIVE_CHILD_RESULT_MISSING']
    evidence = post_exit_warning_evidence(''.join(captured['stdout']), ''.join(captured['stderr']))
    child.update(evidence)
    child['ChildExitCode'] = return_code
    child['UnexpectedChildCrashFailClosed'] = 'PASS' if return_code in (0, 2) else 'FAIL'
    child['ChildPreExitFinalVerdictSuppressed'] = 'PASS' if child.get('FinalVerdict') == 'PENDING_PARENT_EXIT' and child.get('ApprovalLiveTeardownVerdict') == 'PENDING_PARENT_EXIT' else 'FAIL'
    stdout_only = post_exit_warning_evidence(''.join(captured['stdout']), '')
    child['StructuredStdoutWarningScannerIsolation'] = 'PASS' if any(_structured_probe_stdout(line) for line in captured['stdout']) and not any(stdout_only.values()) else 'FAIL'
    child['RawStderrWarningAuthority'] = 'PASS' if post_exit_warning_evidence('', ''.join(captured['stderr'])) == evidence else 'FAIL'
    expiring_warning = any(
        'ExpiringCache' in line or '_start_clear_cron' in line
        for key in ('PostExitPendingTaskWarnings', 'PostExitEventLoopClosedErrors')
        for line in child.get(key, [])
    )
    cache_terminal = child.get('CacheCronTerminalEvidence', 'NOT_RUN')
    child['ExpiringCachePendingDetection'] = 'FAIL' if expiring_warning or (child.get('CacheCronCaptured') and cache_terminal != 'PASS') else 'PASS'
    child['ApprovalLiveCacheTeardownContract'] = 'PASS' if not expiring_warning and (not child.get('CacheCronCaptured') or cache_terminal == 'PASS') else 'FAIL'
    child['ApprovalLiveTeardownVerdict'] = classify_approval_live_teardown(child)
    child.update(finalize_approval_live_verdict(child.get('BusinessVerdict', child.get('Verdict', 'FAIL')), child['ApprovalLiveTeardownVerdict'], child.get('BlockingIssues', [])))
    child['ParentFinalTeardownAuthority'] = 'PASS' if child['ApprovalLiveTeardownVerdict'] in {'PASS', 'FAIL', 'NOT_RUN'} and child['FinalVerdict'] in {'PASS', 'FAIL'} else 'FAIL'
    child['Verdict'] = child.get('BusinessVerdict') if child.get('BusinessVerdict') != 'PASS' else ('PASS' if child['ApprovalLiveTeardownVerdict'] == 'PASS' else 'FAIL_TEARDOWN')
    child['BlockingIssues'] = sorted(set(child.get('BlockingIssues', [])))
    return child


def parent_main(mode, timeout, artifact_dir, run_id):
    modes = ('allow', 'decline') if mode == 'both' else (mode,)
    result = contract_result(run_id, mode)
    result['ApprovalLiveProbeArchitecture'] = 'PARENT_CHILD'
    result['BlockingIssues'] = []
    for current_mode in modes:
        child = _relay_child(current_mode, timeout, artifact_dir, run_id)
        result[f'{current_mode.title()}ModeVerdict'] = child.get('Verdict', 'FAIL')
        result['BusinessVerdict'] = child.get('BusinessVerdict', child.get('Verdict', 'FAIL'))
        for key, value in child.items():
            if key not in {'Mode', 'BlockingIssues', 'RunId'}:
                result[key] = value
        result['BlockingIssues'].extend(child.get('BlockingIssues', []))
        if result['BusinessVerdict'] != 'PASS' or child.get('ApprovalLiveTeardownVerdict') == 'FAIL':
            break
    result['ApprovalProbeAllowMode'] = result.get('AllowModeVerdict', 'NOT_RUN')
    result['ApprovalProbeDeclineMode'] = result.get('DeclineModeVerdict', 'NOT_RUN')
    result['ApprovalBusinessVerdictIndependentFromTeardown'] = 'PASS' if result.get('BusinessVerdict') != 'NOT_RUN' and result.get('ApprovalLiveTeardownVerdict') in {'PASS', 'FAIL', 'NOT_RUN'} else 'FAIL'
    result.update(finalize_approval_live_verdict(result.get('BusinessVerdict', 'FAIL'), result.get('ApprovalLiveTeardownVerdict', 'NOT_RUN'), result['BlockingIssues']))
    result['Verdict'] = result['BusinessVerdict'] if result['BusinessVerdict'] != 'PASS' else ('PASS' if result['ApprovalLiveTeardownVerdict'] == 'PASS' else 'FAIL_TEARDOWN')
    result['ApprovalLiveTeardownNotRunContract'] = 'PASS' if (not result.get('TransportLifecycleStarted') and result['ApprovalLiveTeardownVerdict'] == 'NOT_RUN') or (result.get('TransportLifecycleStarted') and result['ApprovalLiveTeardownVerdict'] in {'PASS', 'FAIL'}) else 'FAIL'
    result['BlockingIssues'] = sorted(set(result['BlockingIssues'])) if result['FinalVerdict'] != 'PASS' else []
    result['SecretsRecorded'] = False
    _write_result(artifact_dir, result)
    (artifact_dir / 'approval.log').write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({'ArtifactDir': str(artifact_dir), **result}, ensure_ascii=False))
    return 0 if result['Verdict'] == 'PASS' else 2


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('allow', 'decline', 'both'), default='both')
    parser.add_argument('--timeout', type=float, default=180)
    parser.add_argument('--contract-only', action='store_true')
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--artifact-dir', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--run-id', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    run_id = args.run_id or datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    artifact_dir = args.artifact_dir or ROOT / '.tmp' / 'm2b-approval-live' / run_id
    artifact_dir.mkdir(parents=True, exist_ok=False if not args.child else True)
    if args.contract_only:
        result = contract_result(run_id, args.mode)
        result['ApprovalLiveProbeArchitecture'] = 'PARENT_CHILD'
        _write_result(artifact_dir, result)
        (artifact_dir / 'approval.log').write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps({'ArtifactDir': str(artifact_dir), **result}))
        return 0
    if args.child:
        return child_main(args.mode, args.timeout, artifact_dir, run_id)
    return parent_main(args.mode, args.timeout, artifact_dir, run_id)


if __name__ == '__main__':
    raise SystemExit(main())
