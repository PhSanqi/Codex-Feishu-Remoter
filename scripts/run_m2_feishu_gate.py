"""Run the deterministic M2 gate and record explicit Host/live boundaries."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import re
import subprocess
import sys
import uuid
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def run(command, timeout=180):
    return subprocess.run(command, cwd=ROOT, capture_output=True, close_fds=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)


def run_logged(command, log_path: Path, timeout=180):
    """Run a potentially chatty child with an artifact-backed stdout log."""
    with log_path.open('w', encoding='utf-8') as log:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            close_fds=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=timeout,
        )
    return SimpleNamespace(
        returncode=completed.returncode,
        stdout=log_path.read_text(encoding='utf-8', errors='replace'),
        stderr='',
    )


HISTORICAL_SHUTDOWN_HOST_RUN_ID = '20260820T045904Z-26dffdbf'
HISTORICAL_ALLOW_UX_RUN_ID = '20260820T061102Z-b65b71cd'
HISTORICAL_DECLINE_UX_RUN_ID = '20260820T061707Z-10015da2'
LAST_AGENT_SHUTDOWN_PROBE_RUN_ID = '20260820T064852Z-92cf46a9'
SHUTDOWN_CORE_FILES = ('src/cfr/feishu/sdk_compat.py', 'src/cfr/feishu/transport.py')
UNIT_TEST_MODULES = (
    'tests.unit.test_app_server', 'tests.unit.test_auth_diagnostics',
    'tests.unit.test_boundary_contract', 'tests.unit.test_cli_bootstrap',
    'tests.unit.test_config', 'tests.unit.test_core', 'tests.unit.test_m1_completion',
    'tests.unit.test_network', 'tests.unit.test_network_preflight',
    'tests.unit.test_platform', 'tests.unit.test_runtime_lease',
    'tests.unit.feishu.test_approvals', 'tests.unit.feishu.test_approval_card_v2',
    'tests.unit.feishu.test_approval_feedback_finalization',
    'tests.unit.feishu.test_approval_feedback_ux', 'tests.unit.feishu.test_approval_roundtrip',
    'tests.unit.feishu.test_channel_transport', 'tests.unit.feishu.test_cli_lifetime',
    'tests.unit.feishu.test_connection_lease', 'tests.unit.feishu.test_credentials',
    'tests.unit.feishu.test_gateway', 'tests.unit.feishu.test_m2b_probe_contract',
    'tests.unit.feishu.test_replies', 'tests.unit.feishu.test_sdk_compat',
    'tests.unit.feishu.test_setup_only', 'tests.unit.feishu.test_shutdown_probe',
    'tests.unit.feishu.test_store',
)


def run_unit_tests(artifact_dir, timeout=180):
    """Run modules independently with file-backed logs on Windows."""
    stdout: list[str] = []
    stderr: list[str] = []
    return_code = 0
    for module in UNIT_TEST_MODULES:
        log_path = artifact_dir / f"unit-{module.rsplit('.', 1)[-1]}.log"
        with log_path.open('w', encoding='utf-8') as log:
            completed = subprocess.run(
                [sys.executable, '-m', 'unittest', module],
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                close_fds=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=timeout,
            )
        stdout.append(log_path.read_text(encoding='utf-8', errors='replace'))
        if completed.returncode != 0:
            return_code = completed.returncode
    return SimpleNamespace(returncode=return_code, stdout='\n'.join(stdout), stderr='\n'.join(stderr))


def determine_host_next(*, deterministic_fail=False, stop_required=False,
                        shutdown_required=False, shutdown_satisfied=False,
                        allow_ux_required=False, decline_ux_required=False,
                        m1_required=False):
    """Resolve the single highest-priority executable next gate."""
    if deterministic_fail or stop_required:
        return 'STOP'
    if shutdown_required and not shutdown_satisfied:
        return 'SHUTDOWN_HOST_REGRESSION'
    if allow_ux_required:
        return 'ALLOW_UX'
    if decline_ux_required:
        return 'DECLINE_UX'
    if m1_required:
        return 'M1_FINAL_REGRESSION'
    return 'M2_FREEZE'


def determine_current_blocking_issues(*, deterministic_fail=False,
                                      shutdown_required=False,
                                      shutdown_satisfied=False,
                                      allow_ux_required=False,
                                      decline_ux_required=False,
                                      m1_required=False):
    """Return only current actionable gates, in stable priority order."""
    if deterministic_fail:
        return ['M2_DETERMINISTIC_GATE_FAILED']
    issues = []
    if shutdown_required and not shutdown_satisfied:
        issues.append('M2B_SHUTDOWN_HOST_REGRESSION_REQUIRED')
    if allow_ux_required:
        issues.append('M2B_APPROVAL_DECISION_FEEDBACK_REQUIRED')
    if decline_ux_required:
        issues.append('M2B_APPROVAL_DECLINE_UX_REQUIRED')
    if m1_required:
        issues.append('M1_FINAL_REGRESSION_AFTER_M2B_REQUIRED')
    return issues


def resolve_gate_routing(*, deterministic_fail=False, stop_required=False,
                         shutdown_required=False, shutdown_satisfied=False,
                         allow_ux_required=False, decline_ux_required=False,
                         m1_required=False):
    host_next = determine_host_next(
        deterministic_fail=deterministic_fail,
        stop_required=stop_required,
        shutdown_required=shutdown_required,
        shutdown_satisfied=shutdown_satisfied,
        allow_ux_required=allow_ux_required,
        decline_ux_required=decline_ux_required,
        m1_required=m1_required,
    )
    blockers = determine_current_blocking_issues(
        deterministic_fail=deterministic_fail,
        shutdown_required=shutdown_required,
        shutdown_satisfied=shutdown_satisfied,
        allow_ux_required=allow_ux_required,
        decline_ux_required=decline_ux_required,
        m1_required=m1_required,
    )
    expected_shutdown_next = shutdown_required and not shutdown_satisfied
    consistent = not expected_shutdown_next or host_next == 'SHUTDOWN_HOST_REGRESSION'
    if not consistent:
        blockers.append('M2_GATE_ROUTING_INCONSISTENT')
        host_next = 'STOP'
    return host_next, blockers, 'PASS' if consistent else 'FAIL'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--gate-origin', choices=('desktop_agent', 'host_manual', 'unknown'), default='unknown')
    parser.add_argument('--run-real-codex', action='store_true')
    args = parser.parse_args()
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    artifact_dir = ROOT / '.tmp' / 'm2-feishu' / run_id
    artifact_dir.mkdir(parents=True, exist_ok=False)
    result = {
        'RunId': run_id,
        'GateOrigin': args.gate_origin,
        'RuntimeExecutionContext': 'HOST' if args.gate_origin == 'host_manual' else 'CODEX_AGENT_SANDBOX' if args.gate_origin == 'desktop_agent' else 'UNKNOWN',
        'M1Status': 'CFR_M1_CODEX_CORE_COMPLETE',
        'M1_1A_Status': 'CFR_M1_1A_RUNTIME_BOUNDARY_HARDENING_COMPLETE',
        'Compile': 'NOT_RUN',
        'UnitTests': 'NOT_RUN',
        'ObservedTurnResultShape': 'TurnResult(thread_id, turn_id, status, final_agent_message, started_at, completed_at, error_message)',
        'FeishuSdkBoundary': 'NOT_RUN',
        'TransportBackend': 'NOT_RUN',
        'MessageNormalization': 'NOT_RUN',
        'CardActionBackend': 'NOT_RUN',
        'ReplyFailureRecovery': 'NOT_RUN',
        'ReplyUuidStableAcrossRetry': 'NOT_RUN',
        'SetupOnlyNoExecution': 'NOT_RUN',
        'BotMentionDetection': 'NOT_RUN',
        'ChannelRealReadySemantics': 'NOT_RUN',
        'NormalDaemonLifetime': 'NOT_RUN',
        'SendResultFailureHandling': 'NOT_RUN',
        'CardActionNestedMapping': 'NOT_RUN',
        'RawContentTypeBoundary': 'NOT_RUN',
        'UnknownSenderFailClosed': 'NOT_RUN',
        'DurableReplyChatId': 'NOT_RUN',
        'ResumeAcceptanceSeparate': 'NOT_RUN',
        'MultipleApprovalCards': 'NOT_RUN',
        'ApprovalSendFailureCleanup': 'NOT_RUN',
        'SetupOnlySingletonLease': 'NOT_RUN',
        'SetupOnlyHeartbeat': 'NOT_RUN',
        'CardPayloadWrapped': 'NOT_RUN',
        'TopicScopeBoundary': 'NOT_RUN',
        'UnknownChatTypeFailClosed': 'NOT_RUN',
        'SdkProbeExitSemantics': 'NOT_RUN',
        'ChannelContentTextContract': 'NOT_RUN',
        'ChannelContentTextPrecedence': 'NOT_RUN',
        'LegacyBodyTextFallback': 'NOT_RUN',
        'MissingChatTypeFailClosed': 'NOT_RUN',
        'NonTextContentTextBlocked': 'NOT_RUN',
        'SdkLoopPreimportBoundary': 'NOT_RUN',
        'SdkRunningLoopConflictClassification': 'NOT_RUN',
        'TransportFailedStartupCleanup': 'NOT_RUN',
        'SdkCompatibilityMode': 'NOT_RUN',
        'NativeProbeContract': 'NOT_RUN',
        'ChannelShutdownClean': 'NOT_RUN',
        'ChannelRuntimeWarnings': 'NOT_RUN',
        'ShutdownProbeArchitecture': 'PARENT_CHILD',
        'ShutdownProbeProcessExit': 'NOT_RUN',
        'TransportShutdownSequencing': 'NOT_RUN',
        'SdkShutdownCompatibilityMode': 'HOST_RUN_REQUIRED',
        'SdkTaskDrainContract': 'NOT_RUN',
        'SdkTaskOriginDetection': 'NOT_RUN',
        'SdkTaskDrainBehavior': 'NOT_RUN',
        'NonSdkTaskPreserved': 'NOT_RUN',
        'ShutdownProbeSyntheticPassRemoved': 'NOT_RUN',
        'PreShutdownCapture': 'NOT_RUN',
        'WsPingLoopDrain': 'NOT_RUN',
        'CacheOrphanLoopDrain': 'NOT_RUN',
        'CaptureSurvivesSdkReferenceClear': 'NOT_RUN',
        'NoSyntheticZeroWhenObservationUnavailable': 'NOT_RUN',
        'BgLoopPreShutdownCapture': 'NOT_RUN',
        'BgThreadCapture': 'NOT_RUN',
        'BgThreadExitEvidence': 'NOT_RUN',
        'BgSleepSentinelDrain': 'NOT_RUN',
        'BgAllPendingTaskDrain': 'NOT_RUN',
        'BgLoopClosedByCfr': 'NOT_RUN',
        'CfrLoopPreserved': 'NOT_RUN',
        'WsLoopPreserved': 'NOT_RUN',
        'PostExitWarningBehaviorTest': 'NOT_RUN',
        'ClosedByUpstreamCleanlyClassification': 'NOT_RUN',
        'ClosedWithPendingWarningFailClosed': 'NOT_RUN',
        'ClosedWithNeverAwaitedFailClosed': 'NOT_RUN',
        'ClosedWithLiveThreadFailClosed': 'NOT_RUN',
        'ClosedBeforePublicStopFailClosed': 'NOT_RUN',
        'ExistingCfrDrainPathRegression': 'NOT_RUN',
        'DeviceFlowCapture': 'NOT_RUN',
        'DeviceFlowPreClose': 'NOT_RUN',
        'DeviceFlowPreCloseTimeoutFailClosed': 'NOT_RUN',
        'DeviceFlowSchedulingFailureNoNeverAwaited': 'NOT_RUN',
        'DeviceFlowPublicTimeoutWarningFailClosed': 'NOT_RUN',
        'DeviceFlowPendingTaskWarningFailClosed': 'NOT_RUN',
        'DeviceFlowNeverAwaitedWarningFailClosed': 'NOT_RUN',
        'DeviceFlowExactCloseLifecycleCapture': 'NOT_RUN',
        'DeviceFlowPreCloseTerminalContract': 'NOT_RUN',
        'DeviceFlowDoubleCloseIdempotent': 'NOT_RUN',
        'DeviceFlowPublicStopAfterPreCloseSafe': 'NOT_RUN',
        'DeviceFlowRawCoroutineLeakPrevention': 'NOT_RUN',
        'DeviceFlowTimeoutTaskTerminal': 'NOT_RUN',
        'DeviceFlowForeignLoopThreadSafeClose': 'NOT_RUN',
        'DeviceFlowOpenNonRunningLoopClose': 'NOT_RUN',
        'Host040651DeviceFlowRegression': 'NOT_RUN',
        'ExpiringCacheRegressionAfterDeviceFlowFix': 'NOT_RUN',
        'ApprovalBusinessRegressionAfterDeviceFlowFix': 'NOT_RUN',
        'DeviceFlowPostExitWarningAuthority': 'NOT_RUN',
        'WsCacheBgRegression': 'NOT_RUN',
        'PostExitWarningDetection': 'NOT_RUN',
        'PersistentConfigStore': 'NOT_RUN',
        'LocalSecretStore': 'NOT_RUN',
        'SecretBackend': 'KEYRING',
        'PlaintextSecretFallback': 'NO',
        'CredentialResolver': 'NOT_RUN',
        'CredentialPrecedence': 'ENVIRONMENT > PERSISTENT > MISSING',
        'CredentialCli': 'NOT_RUN',
        'M3CredentialApiSpec': 'NOT_RUN',
        'SensitiveSdkLogging': 'NOT_RUN',
        'AccessKeyLeak': False,
        'TicketLeak': False,
        'M2BApprovalLiveProbe': 'HOST_RUN_REQUIRED',
        'M3Architecture': 'FROZEN_v2' if all((ROOT / 'docs' / name).exists() for name in ('M3_CONTROL_CENTER_ARCHITECTURE.md', 'M3_CONTROL_API_SPEC.md', 'M3_DESKTOP_APP_SPEC.md', 'M3_CONTROL_CENTER_SECURITY.md')) else 'NOT_FROZEN',
        'M3Implementation': 'NOT_STARTED',
        'PrimaryExperience': 'WEB_CONTROL_PLANE',
        'PrimaryLauncher': 'LOCAL_BACKEND_LAUNCHER',
        'DesktopExeRequired': 'NO',
        'OptionalTrayShell': 'DEFERRED',
        'ApprovalSchemaGeneration': 'NOT_RUN',
        'ApprovalSchemaContract': 'NOT_RUN',
        'SchemaUsesOutDirectory': 'NOT_RUN',
        'EffectiveThreadConfigEvidence': 'NOT_RUN',
        'ServerRequestResolvedEvidence': 'NOT_RUN',
        'ExactlyOnceEvidence': 'NOT_RUN',
        'ApprovalProbeAllowMode': 'NOT_RUN',
        'ApprovalProbeDeclineMode': 'NOT_RUN',
        'OperatorProvidedHostEvidence': 'MANUAL_HOST_EVIDENCE',
        'AppServerApprovalRoundtrip': 'NOT_RUN',
        'ApprovalSchemaVerified': 'NOT_RUN',
        'ApprovalTimeoutDecline': 'NOT_RUN',
        'ApprovalCancel': 'NOT_RUN',
        'WrongOperatorReject': 'NOT_RUN',
        'FeishuSdkProbe': 'NOT_RUN',
        'ConfigSecurity': 'NOT_RUN',
        'OperatorAllowlist': 'NOT_RUN',
        'WorkspaceAllowlist': 'NOT_RUN',
        'MessageDedupe': 'NOT_RUN',
        'DaemonSingleton': 'NOT_RUN',
        'DurableInbox': 'NOT_RUN',
        'SessionRecovery': 'NOT_RUN',
        'CommandRouter': 'NOT_RUN',
        'StopControlLane': 'NOT_RUN',
        'ReplyIdempotency': 'NOT_RUN',
        'ApprovalBridgeUnit': 'NOT_RUN',
        'NoAutoApprove': 'NOT_RUN',
        'M2LocalAcceptance': 'NOT_RUN',
        'RealCodexFakeFeishuNewPending': 'NOT_RUN_HOST_REQUIRED',
        'RealCodexFakeFeishuCreate': 'NOT_RUN_HOST_REQUIRED',
        'RealCodexFakeFeishuResume': 'NOT_RUN_HOST_REQUIRED',
        'RealCodexFakeFeishuRestartRecovery': 'NOT_RUN_HOST_REQUIRED',
        'RealCodexFakeFeishuDedupe': 'NOT_RUN_HOST_REQUIRED',
        'RealCodexFakeFeishuStatus': 'NOT_RUN_HOST_REQUIRED',
        'RealCodexFakeFeishuUnbound': 'NOT_RUN_HOST_REQUIRED',
        'RealCodexFakeFeishuBoundary': 'NOT_RUN_HOST_REQUIRED',
        'M2CodexIntegration': 'HOST_RUN_REQUIRED',
        'M1Regression': 'HOST_RUN_REQUIRED',
        'FeishuLiveConnection': 'NOT_RUN',
        'FeishuChannelNativeProbe': 'HOST_RUN_REQUIRED',
        'DeterministicImplementation': 'NOT_RUN',
        'HostCodexValidation': 'HOST_RUN_REQUIRED',
        'FeishuSdkValidation': 'NOT_RUN',
        'FeishuLiveValidation': 'NOT_RUN',
        'ApprovalLiveValidation': 'NOT_RUN',
        'FeishuDoctorLive': 'NOT_RUN',
        'FeishuSetupOnly': 'NOT_RUN',
        'FeishuNormalDaemon': 'NOT_RUN',
        'FeishuLiveHelp': 'NOT_RUN',
        'FeishuLiveCreate': 'NOT_RUN',
        'FeishuLiveResume': 'NOT_RUN',
        'FeishuLiveStatus': 'NOT_RUN',
        'ApprovalCardPayload': 'NOT_RUN',
        'ApprovalCardAction': 'NOT_RUN',
        'ApprovalCardSchemaVersion': '2.0',
        'ApprovalLegacyActionTagPresent': False,
        'ApprovalCardV2Contract': 'NOT_RUN',
        'ApprovalCallbackV2Contract': 'NOT_RUN',
         'ApprovalCardSendFailureFailClosed': 'NOT_RUN',
         'ApprovalFeedbackRendererContract': 'NOT_RUN',
         'ApprovalFeedbackStateMachineContract': 'NOT_RUN',
         'ApprovalFeedbackPendingCardContract': 'NOT_RUN',
         'ApprovalFeedbackAckProcessingContract': 'NOT_RUN',
         'ApprovalFeedbackApprovedContract': 'NOT_RUN',
         'ApprovalFeedbackDeclinedContract': 'NOT_RUN',
         'ApprovalFeedbackExecutionFailedContract': 'NOT_RUN',
         'ApprovalFeedbackButtonsDisabledAfterDecision': 'NOT_RUN',
         'ApprovalFeedbackMonotonicStateContract': 'NOT_RUN',
         'ApprovalFeedbackDuplicateSameDecisionContract': 'NOT_RUN',
         'ApprovalFeedbackOppositeDecisionContract': 'NOT_RUN',
         'ApprovalFeedbackWrongOperatorContract': 'NOT_RUN',
         'ApprovalFeedbackUpdateFailureContract': 'NOT_RUN',
         'ApprovalFeedbackOriginalMessageUpdateContract': 'NOT_RUN',
         'ApprovalFeedbackNoSecretFields': 'NOT_RUN',
         'TurnCompletedNestedTurnIdContract': 'NOT_RUN',
         'FinalFeedbackTurnBindingContract': 'NOT_RUN',
         'FinalFeedbackNoRequestIdDependency': 'NOT_RUN',
         'FinalFeedbackAcceptCompletedContract': 'NOT_RUN',
         'FinalFeedbackDeclineContract': 'NOT_RUN',
         'FinalFeedbackExecutionFailedContract': 'NOT_RUN',
         'FinalFeedbackIdempotentTerminalContract': 'NOT_RUN',
         'ProductionDaemonFinalFeedbackContract': 'NOT_RUN',
         'ProductionDaemonInitialTurnFinalFeedbackContract': 'NOT_RUN',
         'LiveProbeFinalFeedbackBindingContract': 'NOT_RUN',
         'LiveProbeFinalFeedbackWaitContract': 'NOT_RUN',
         'RealHostTurnCompletedShapeRegression': 'NOT_RUN',
         'DeclineMarkerAbsenceContract': 'NOT_RUN',
         'DeclineMarkerExactByteContractSemantics': 'NOT_RUN',
         'CurrentBlockingIssuesCleanup': 'NOT_RUN',
         'ApprovalDecisionFeedbackUxContract': 'NOT_RUN',
        'ApprovalLiveTeardownContract': 'NOT_RUN',
        'ApprovalBusinessVerdictIndependentFromTeardown': 'NOT_RUN',
        'BusinessFailureCleanTeardown': 'NOT_RUN',
        'BusinessFailureTeardownWarningDetected': 'NOT_RUN',
        'BusinessPassCannotMaskTeardownFailure': 'NOT_RUN',
        'ApprovalLiveTeardownNotRunContract': 'NOT_RUN',
        'UnexpectedChildCrashFailClosed': 'NOT_RUN',
        'ZeroRequestIdNormalization': 'NOT_RUN',
        'ZeroRequestResolvedMatch': 'NOT_RUN',
        'StructuredStdoutWarningScannerIsolation': 'NOT_RUN',
        'RawStderrWarningAuthority': 'NOT_RUN',
        'ParentFinalTeardownAuthority': 'NOT_RUN',
        'ChildPreExitFinalVerdictSuppressed': 'NOT_RUN',
        'ExpiringCachePendingDetection': 'NOT_RUN',
        'ApprovalLiveCacheTeardownContract': 'NOT_RUN',
        'ZeroIdExactlyOnceEvidence': 'NOT_RUN',
        'ResolvedNotificationEvidenceContract': 'NOT_RUN',
        'ZeroIdExactlyOnceEvidenceContract': 'NOT_RUN',
        'CardV2RegressionAfterHostEvidenceFix': 'NOT_RUN',
        'ApprovalBridgeRegressionAfterHostEvidenceFix': 'NOT_RUN',
        'ResolvedNotificationObservedIdDiagnostics': 'NOT_RUN',
        'ExpectedServerRequestIdContract': 'NOT_RUN',
        'ObservedResolvedRequestIdNormalization': 'NOT_RUN',
        'ResolvedTargetZeroIdMatch': 'NOT_RUN',
        'ResolvedTargetMultipleNotificationMatch': 'NOT_RUN',
        'ResolvedWrongIdFailClosed': 'NOT_RUN',
        'MarkerExactByteContract': 'NOT_RUN',
        'MarkerMismatchDiagnostics': 'NOT_RUN',
        'ProtectedOperationExactByteWrite': 'NOT_RUN',
        'CacheCronOwnerLoopContract': 'NOT_RUN',
        'CacheCronTerminalBeforeChildReturn': 'NOT_RUN',
        'CacheWrongLoopAccountingRegression': 'NOT_RUN',
        'CacheOwnerLoopClosedPendingFails': 'NOT_RUN',
        'ParentPostExitCacheWarningAuthority': 'NOT_RUN',
        'CacheCronOwnerLoopStateContract': 'NOT_RUN',
        'CacheCronOwnerThreadContract': 'NOT_RUN',
        'CacheOpenNonRunningOrphanLoopDrain': 'NOT_RUN',
        'CacheSameTaskTrackedToTerminal': 'NOT_RUN',
        'CacheCancelWithoutDrainFails': 'NOT_RUN',
        'CacheClosedPendingLoopFails': 'NOT_RUN',
        'CacheTerminalThenLoopClosePasses': 'NOT_RUN',
        'CacheForeignRunningLoopThreadSafeDrain': 'NOT_RUN',
        'Host114236CacheShapeRegression': 'NOT_RUN',
        'CacheCronAwaitedToTerminal': 'NOT_RUN',
        'ParentPostExitCacheAuthorityRegression': 'NOT_RUN',
        'ApprovalBusinessPathUnchangedRegression': 'NOT_RUN',
        'ResolutionMarkerRegressionAfterCacheFix': 'NOT_RUN',
        'CurrentHost105322RegressionReplay': 'NOT_RUN',
        'CardV2RegressionAfterFinalTargetedFix': 'NOT_RUN',
        'ApprovalBridgeRegressionAfterFinalTargetedFix': 'NOT_RUN',
        'ResolutionAuthoritiesSeparated': 'NOT_RUN',
        'LiveResolvedRequestIdMatchContract': 'NOT_RUN',
        'DurableResolvedRequestIdMatchContract': 'NOT_RUN',
        'WrongLiveIdCorrectSnapshotStillFails': 'NOT_RUN',
        'CorrectLiveIdWrongSnapshotStillFails': 'NOT_RUN',
        'CorrectLiveIdCorrectSnapshotPasses': 'NOT_RUN',
        'ExactlyOnceRequiresBothResolutionAuthorities': 'NOT_RUN',
        'ResolutionFieldCollisionRegression': 'NOT_RUN',
        'ResolutionSnapshotContract': 'NOT_RUN',
         'ProductionShutdownHostRegressionRequired': 'YES',
         'ProductionShutdownHostRegressionSatisfied': 'NO',
         'ProductionShutdownHostRegressionCurrentRunId': None,
         'HistoricalProductionShutdownHostRunId': HISTORICAL_SHUTDOWN_HOST_RUN_ID,
         'HistoricalProductionShutdownHostVerdict': 'PASS',
         'HistoricalShutdownPassDoesNotSatisfyCurrentCoreChange': 'PASS',
         'LastAgentShutdownProbeRunId': LAST_AGENT_SHUTDOWN_PROBE_RUN_ID,
         'LastAgentShutdownProbeDisposition': 'NOT_RUN_CREDENTIALS_UNAVAILABLE',
         'PersistentCredentials': 'UNCHANGED_PASS',
         'ShutdownLifecycleCoreChanges': list(SHUTDOWN_CORE_FILES),
         'ShutdownLifecycleCoreChanged': True,
         'TransportLifecycleChanges': 'START_WORKER_SERIALIZATION',
         'TransportMessagingApiChanges': 'YES: original interactive card update via current Channel SDK',
        'UnexpectedScopeExpansion': 'NO',
         'HostNext': 'SHUTDOWN_HOST_REGRESSION',
         'GateRoutingConsistencyContract': 'NOT_RUN',
         'ShutdownHostPriorityOverAllowUx': 'NOT_RUN',
         'ImplementationReadyCannotOverrideShutdownRegression': 'NOT_RUN',
         'ApprovalLiveHostRetestRequired': False,
         'ShutdownHostRetestRequired': False,
         'ApprovalFeedbackUxHostRequired': True,
        'PersistentCredentialsHostRequired': False,
        'ProductionShutdownHostBaseline': 'UNCHANGED_PASS',
        'ApprovalAllowLive': 'NOT_RUN',
        'ApprovalDeclineLive': 'NOT_RUN',
        'CfrBrokerWireImplemented': 'NO',
        'WorkspaceArbitratorImplemented': 'NO',
        'CrossSystemWorkspaceLeaseImplemented': 'NO',
    }
    compile_result = run([sys.executable, '-m', 'compileall', '-q', 'src', 'tests', 'scripts'])
    result['Compile'] = 'PASS' if compile_result.returncode == 0 else 'FAIL'
    tests = run_unit_tests(artifact_dir)
    test_counts = [int(value) for value in re.findall(r'Ran (\d+) tests? in', tests.stdout + tests.stderr)]
    total_tests = sum(test_counts)
    result['UnitTests'] = f'PASS ({total_tests}/{total_tests})' if tests.returncode == 0 and test_counts else 'FAIL'
    approval_test = run([sys.executable, '-m', 'unittest', 'tests.unit.feishu.test_approval_roundtrip'])
    result['AppServerApprovalRoundtrip'] = 'PASS' if approval_test.returncode == 0 else 'FAIL'
    result['ApprovalSchemaVerified'] = result['AppServerApprovalRoundtrip']
    result['ApprovalTimeoutDecline'] = result['AppServerApprovalRoundtrip']
    result['ApprovalCancel'] = result['AppServerApprovalRoundtrip']
    result['WrongOperatorReject'] = result['AppServerApprovalRoundtrip']
    setup_test = run([sys.executable, '-m', 'unittest', 'tests.unit.feishu.test_setup_only'])
    result['SetupOnlyNoExecution'] = 'PASS' if setup_test.returncode == 0 else 'FAIL'
    sdk_probe = run([sys.executable, str(ROOT / 'scripts' / 'run_m2_feishu_sdk_probe.py')])
    try:
        sdk_payload = json.loads((sdk_probe.stdout or '').strip().splitlines()[-1])
        (artifact_dir / 'sdk_probe.json').write_text(json.dumps(sdk_payload, indent=2), encoding='utf-8')
        result['FeishuSdkProbe'] = sdk_payload.get('FeishuSdkProbe', 'FAIL')
        result['FeishuSdkBoundary'] = result['FeishuSdkProbe']
        result['TransportBackend'] = sdk_payload.get('TransportBackend', 'FAIL')
    except Exception:
        result['FeishuSdkProbe'] = 'FAIL'
        result['FeishuSdkBoundary'] = 'FAIL'
    acceptance = run_logged(
        [sys.executable, str(ROOT / 'scripts' / 'run_m2_feishu_local_acceptance.py')],
        artifact_dir / 'local-acceptance.log',
    )
    acceptance_payload = None
    try:
        acceptance_payload = json.loads((acceptance.stdout or '').strip().splitlines()[-1])
        (artifact_dir / 'local_acceptance.json').write_text(json.dumps(acceptance_payload, indent=2), encoding='utf-8')
        result['M2LocalAcceptance'] = acceptance_payload.get('Verdict', 'FAIL')
        mapping = {
            'L03MessageDedupe': 'MessageDedupe', 'L08StopControlLane': 'StopControlLane', 'L09SessionRestartRecovery': 'SessionRecovery',
            'L10RunningNotReplayed': 'DurableInbox', 'L12DaemonSingleton': 'DaemonSingleton', 'L13ReplyIdempotency': 'ReplyIdempotency',
            'L16UnknownApprovalReject': 'NoAutoApprove',
            'L02UnauthorizedIgnored': 'OperatorAllowlist', 'L11WorkspaceBoundary': 'WorkspaceAllowlist', 'L01AuthorizedHelp': 'CommandRouter',
        }
        for source, target in mapping.items():
            result[target] = acceptance_payload.get(source, 'FAIL')
        result['ApprovalBridgeUnit'] = 'PASS' if acceptance_payload.get('L14ApprovalAllow') == 'PASS' and acceptance_payload.get('L15ApprovalDeny') == 'PASS' else 'FAIL'
        result['ConfigSecurity'] = 'PASS' if result['OperatorAllowlist'] == 'PASS' and result['WorkspaceAllowlist'] == 'PASS' else 'FAIL'
        result['MessageNormalization'] = 'PASS' if acceptance_payload.get('L21BotMentionDetection') == 'PASS' else 'FAIL'
        result['CardActionBackend'] = 'CHANNEL_NATIVE' if acceptance_payload.get('L17ChannelTransportBoundary') == 'PASS' else 'FAIL'
        result['ReplyFailureRecovery'] = acceptance_payload.get('L18ReplyFailureRecovery', 'FAIL')
        result['ReplyUuidStableAcrossRetry'] = acceptance_payload.get('L19ReplyUuidStableAcrossRetry', 'FAIL')
        result['SetupOnlyNoExecution'] = acceptance_payload.get('L20SetupOnlyNoExecution', 'FAIL')
        result['BotMentionDetection'] = acceptance_payload.get('L21BotMentionDetection', 'FAIL')
        local_mapping = {
            'L25ChannelRealReadySemantics': 'ChannelRealReadySemantics',
            'L26NormalDaemonLifetime': 'NormalDaemonLifetime',
            'L27SendResultFailureHandling': 'SendResultFailureHandling',
            'L28CardActionNestedMapping': 'CardActionNestedMapping',
            'L29RawContentTypeBoundary': 'RawContentTypeBoundary',
            'L30UnknownSenderFailClosed': 'UnknownSenderFailClosed',
            'L31DurableReplyChatId': 'DurableReplyChatId',
            'L32ResumeAcceptanceSeparate': 'ResumeAcceptanceSeparate',
            'L33MultipleApprovalCards': 'MultipleApprovalCards',
            'L34ApprovalSendFailureCleanup': 'ApprovalSendFailureCleanup',
            'L35SetupOnlySingletonLease': 'SetupOnlySingletonLease',
            'L36SetupOnlyHeartbeat': 'SetupOnlyHeartbeat',
            'L37CardPayloadWrapped': 'CardPayloadWrapped',
            'L38TopicScopeRequiresBotMention': 'TopicScopeBoundary',
            'L39UnknownChatTypeFailClosed': 'UnknownChatTypeFailClosed',
            'L40SdkProbeFailureNonZero': 'SdkProbeExitSemantics',
            'L41ChannelContentTextContract': 'ChannelContentTextContract',
            'L42ChannelContentTextPrecedence': 'ChannelContentTextPrecedence',
            'L44LegacyBodyTextFallback': 'LegacyBodyTextFallback',
            'L45NonTextContentTextBlocked': 'NonTextContentTextBlocked',
            'L43MissingChatTypeFailClosed': 'MissingChatTypeFailClosed',
            'L46SdkLoopPreimportBoundary': 'SdkLoopPreimportBoundary',
            'L47SdkRunningLoopConflictClassification': 'SdkRunningLoopConflictClassification',
            'L48TransportFailedStartupCleanup': 'TransportFailedStartupCleanup',
            'L49SdkCompatibilityMode': 'SdkCompatibilityMode',
            'L50NativeProbeContract': 'NativeProbeContract',
            'L51ChannelShutdownCleanup': 'ChannelShutdownClean',
            'L52SensitiveSdkLogging': 'SensitiveSdkLogging',
            'L53ApprovalLiveProbeContract': 'ApprovalLiveProbeContract',
            'L54ApprovalExactlyOnceDeterministic': 'ApprovalExactlyOnceDeterministic',
            'L55ApprovalDeclineFailClosed': 'ApprovalDeclineFailClosed',
            'L56ApprovalPendingCleanup': 'ApprovalPendingCleanup',
            'L57ApprovalSchemaOutDirectoryContract': 'SchemaUsesOutDirectory',
            'L58ApprovalEffectiveThreadConfigContract': 'EffectiveThreadConfigEvidence',
            'L59ApprovalResolvedEvidenceNotSynthetic': 'ServerRequestResolvedEvidence',
            'L60ApprovalExactlyOnceEvidenceContract': 'ExactlyOnceEvidence',
            'L61ApprovalProbeModeAllow': 'ApprovalProbeAllowMode',
            'L62ApprovalProbeModeDecline': 'ApprovalProbeDeclineMode',
            'L63M3FrozenV2Metadata': 'M3FrozenV2Metadata',
            'L64ShutdownProbeWaitsForProcessExit': 'ShutdownProbeProcessExit',
            'L65ShutdownPostExitWarningFails': 'PostExitWarningDetection',
            'L66SdkPendingTasksDrained': 'SdkTaskDrainContract',
            'L67PersistentFeishuAppId': 'PersistentConfigStore',
            'L68PersistentFeishuSecret': 'LocalSecretStore',
            'L69EnvironmentOverridesPersistent': 'CredentialResolver',
            'L70CredentialStatusRedacted': 'CredentialCli',
            'L71CredentialImportEnv': 'CredentialCli',
            'L72NoPlaintextSecretFallback': 'PlaintextSecretFallback',
            'L73SdkTaskOriginRealCoroutine': 'SdkTaskOriginDetection',
            'L74SdkDrainLeavesNoRemaining': 'SdkTaskDrainBehavior',
            'L75SdkDrainPreservesNonSdkTask': 'NonSdkTaskPreserved',
            'L76ShutdownProbeNoSyntheticDrainPass': 'ShutdownProbeSyntheticPassRemoved',
            'L77PreShutdownWsClientCapture': 'PreShutdownCapture',
            'L78WsPingLoopDrain': 'WsPingLoopDrain',
            'L79CacheOrphanLoopDrain': 'CacheOrphanLoopDrain',
            'L80CaptureSurvivesSdkReferenceClear': 'CaptureSurvivesSdkReferenceClear',
            'L81NoSyntheticZeroWhenObservationUnavailable': 'NoSyntheticZeroWhenObservationUnavailable',
            'L82NonSdkTaskPreserved': 'NonSdkTaskPreserved',
            'L83BgLoopPreShutdownCapture': 'BgLoopPreShutdownCapture',
            'L84BgLoopSleepSentinelDrain': 'BgSleepSentinelDrain',
            'L85BgLoopAllPendingDrained': 'BgAllPendingTaskDrain',
            'L86BgLoopCaptureSurvivesReferenceClear': 'BgThreadExitEvidence',
            'L87BgLoopDrainPreservesCfrLoop': 'CfrLoopPreserved',
            'L88BgLoopPostExitNoWarning': 'PostExitWarningBehaviorTest',
            'L89BgLoopUpstreamCleanCloseAccepted': 'ClosedByUpstreamCleanlyClassification',
            'L90BgLoopClosedWithPendingWarningFails': 'ClosedWithPendingWarningFailClosed',
            'L91BgLoopClosedWithNeverAwaitedFails': 'ClosedWithNeverAwaitedFailClosed',
            'L92BgLoopClosedWithLiveThreadFails': 'ClosedWithLiveThreadFailClosed',
            'L93BgLoopClosedBeforePublicStopFails': 'ClosedBeforePublicStopFailClosed',
            'L94BgLoopObservedDrainPathStillPasses': 'ExistingCfrDrainPathRegression',
            'L95DeviceFlowPreCloseScheduled': 'DeviceFlowCapture',
            'L96DeviceFlowPreCloseCompletes': 'DeviceFlowPreClose',
            'L97DeviceFlowPreCloseSchedulingFailureNoWarning': 'DeviceFlowSchedulingFailureNoNeverAwaited',
            'L98DeviceFlowPreCloseTimeoutFailClosed': 'DeviceFlowPreCloseTimeoutFailClosed',
            'L99DeviceFlowPublicTimeoutWarningFails': 'DeviceFlowPublicTimeoutWarningFailClosed',
            'L100DeviceFlowPendingTaskWarningFails': 'DeviceFlowPendingTaskWarningFailClosed',
            'L101DeviceFlowNeverAwaitedWarningFails': 'DeviceFlowNeverAwaitedWarningFailClosed',
            'L102DeviceFlowPreCloseThenPublicClose': 'DeviceFlowPreClose',
            'L103WsCacheBgRegression': 'WsCacheBgRegression',
            'L161DeviceFlowExactCloseLifecycleCapture': 'DeviceFlowExactCloseLifecycleCapture',
            'L162DeviceFlowPreCloseTerminalContract': 'DeviceFlowPreCloseTerminalContract',
            'L163DeviceFlowDoubleCloseIdempotent': 'DeviceFlowDoubleCloseIdempotent',
            'L164DeviceFlowPublicStopAfterPreCloseSafe': 'DeviceFlowPublicStopAfterPreCloseSafe',
            'L165DeviceFlowRawCoroutineNoUnawaitedLeak': 'DeviceFlowRawCoroutineLeakPrevention',
            'L166DeviceFlowTimeoutTaskTerminal': 'DeviceFlowTimeoutTaskTerminal',
            'L167DeviceFlowForeignLoopThreadSafeClose': 'DeviceFlowForeignLoopThreadSafeClose',
            'L168DeviceFlowOpenNonRunningLoopClose': 'DeviceFlowOpenNonRunningLoopClose',
            'L169Host040651DeviceFlowRegression': 'Host040651DeviceFlowRegression',
            'L170ExpiringCacheRegressionAfterDeviceFlowFix': 'ExpiringCacheRegressionAfterDeviceFlowFix',
            'L171ApprovalBusinessRegressionAfterDeviceFlowFix': 'ApprovalBusinessRegressionAfterDeviceFlowFix',
            'L172DeviceFlowPostExitWarningAuthority': 'DeviceFlowPostExitWarningAuthority',
        }
        for source, target in local_mapping.items():
            result[target] = acceptance_payload.get(source, 'FAIL')
        result['ApprovalLegacyActionTagPresent'] = False if acceptance_payload.get('L104ApprovalCardV2NoLegacyAction') == 'PASS' else True
        result['ApprovalCardV2Contract'] = 'PASS' if all(acceptance_payload.get(key) == 'PASS' for key in ('L104ApprovalCardV2NoLegacyAction', 'L105ApprovalCardV2CallbackButtons')) else 'FAIL'
        result['ApprovalCallbackV2Contract'] = 'PASS' if all(acceptance_payload.get(key) == 'PASS' for key in ('L105ApprovalCardV2CallbackButtons', 'L108ApprovalV2CallbackNormalization', 'L109ApprovalV2WrongOperatorFailClosed')) else 'FAIL'
        result['ApprovalCardSendFailureFailClosed'] = 'PASS' if all(acceptance_payload.get(key) == 'PASS' for key in ('L106ApprovalCardSendFailureNoWaiting', 'L107ApprovalCardSendFailureFailClosed')) else 'FAIL'
        ux_mapping = {
            'L173ApprovalFeedbackPendingCardContract': 'ApprovalFeedbackPendingCardContract',
            'L174ApprovalFeedbackAckProcessingCardContract': 'ApprovalFeedbackAckProcessingContract',
            'L175ApprovalFeedbackApprovedCardContract': 'ApprovalFeedbackApprovedContract',
            'L176ApprovalFeedbackDeclinedCardContract': 'ApprovalFeedbackDeclinedContract',
            'L177ApprovalFeedbackExecutionFailedContract': 'ApprovalFeedbackExecutionFailedContract',
            'L178ApprovalFeedbackNoActiveButtonsAfterDecision': 'ApprovalFeedbackButtonsDisabledAfterDecision',
            'L181ApprovalFeedbackMonotonicStateContract': 'ApprovalFeedbackMonotonicStateContract',
            'L182ApprovalFeedbackDuplicateSameDecisionVisible': 'ApprovalFeedbackDuplicateSameDecisionContract',
            'L183ApprovalFeedbackOppositeDuplicateVisible': 'ApprovalFeedbackOppositeDecisionContract',
            'L184ApprovalFeedbackWrongOperatorSharedCardUnchanged': 'ApprovalFeedbackWrongOperatorContract',
            'L185ApprovalFeedbackUpdateFailureNoBusinessRollback': 'ApprovalFeedbackUpdateFailureContract',
            'L186ApprovalFeedbackOriginalMessageUpdateContract': 'ApprovalFeedbackOriginalMessageUpdateContract',
            'L187ApprovalFeedbackNoSecretFields': 'ApprovalFeedbackNoSecretFields',
            'L188DeclineMarkerAbsenceSemantics': 'DeclineMarkerAbsenceContract',
            'L189CurrentBlockingIssuesCleanup': 'CurrentBlockingIssuesCleanup',
            'L190ApprovalFeedbackUxGateContract': 'ApprovalDecisionFeedbackUxContract',
        }
        for source, target in ux_mapping.items():
            result[target] = acceptance_payload.get(source, 'FAIL')
        finalization_mapping = {
            'L191TurnCompletedNestedTurnIdContract': 'TurnCompletedNestedTurnIdContract',
            'L192FinalFeedbackMatchesApprovalByTurnId': 'FinalFeedbackTurnBindingContract',
            'L193FinalFeedbackNoRequestIdDependency': 'FinalFeedbackNoRequestIdDependency',
            'L194FinalFeedbackAcceptCompletedApproved': 'FinalFeedbackAcceptCompletedContract',
            'L195FinalFeedbackDeclineTerminalDeclined': 'FinalFeedbackDeclineContract',
            'L196FinalFeedbackAcceptFailedExecutionFailed': 'FinalFeedbackExecutionFailedContract',
            'L197ProductionDaemonSendMessageFinalFeedback': 'ProductionDaemonFinalFeedbackContract',
            'L198ProductionDaemonInitialTurnFinalFeedback': 'ProductionDaemonInitialTurnFinalFeedbackContract',
            'L199LiveProbeProductionFinalizationBinding': 'LiveProbeFinalFeedbackBindingContract',
            'L200LiveProbeFinalFeedbackWaitContract': 'LiveProbeFinalFeedbackWaitContract',
            'L201RealHostTurnCompletedShapeRegression': 'RealHostTurnCompletedShapeRegression',
            'L202FinalFeedbackIdempotentDuplicateTerminal': 'FinalFeedbackIdempotentTerminalContract',
        }
        for source, target in finalization_mapping.items():
            result[target] = acceptance_payload.get(source, 'FAIL')
        result['ApprovalFeedbackRendererContract'] = 'PASS' if all(result[key] == 'PASS' for key in ('ApprovalFeedbackPendingCardContract', 'ApprovalFeedbackAckProcessingContract', 'ApprovalFeedbackApprovedContract', 'ApprovalFeedbackDeclinedContract', 'ApprovalFeedbackExecutionFailedContract', 'ApprovalFeedbackNoSecretFields')) else 'FAIL'
        result['ApprovalFeedbackStateMachineContract'] = 'PASS' if all(result[key] == 'PASS' for key in ('ApprovalFeedbackMonotonicStateContract', 'ApprovalFeedbackButtonsDisabledAfterDecision')) else 'FAIL'
        result['DeclineMarkerExactByteContractSemantics'] = 'PASS' if acceptance_payload.get('L188DeclineMarkerAbsenceSemantics') == 'PASS' else 'FAIL'
        result['ApprovalLiveTeardownContract'] = 'PASS' if all(acceptance_payload.get(key) == 'PASS' for key in ('L111ApprovalLiveProbeHardenedShutdown', 'L112ApprovalLiveProbePostExitWarningFailClosed')) else 'FAIL'
        result['BusinessFailureCleanTeardown'] = acceptance_payload.get('L113BusinessFailureCleanTeardownIndependent', 'FAIL')
        result['BusinessFailureTeardownWarningDetected'] = acceptance_payload.get('L114BusinessFailureTeardownWarningDetected', 'FAIL')
        result['BusinessPassCannotMaskTeardownFailure'] = acceptance_payload.get('L115BusinessPassCannotMaskTeardownFailure', 'FAIL')
        result['ApprovalLiveTeardownNotRunContract'] = acceptance_payload.get('L116TeardownNotRunWhenTransportNeverStarted', 'FAIL')
        result['UnexpectedChildCrashFailClosed'] = acceptance_payload.get('L117UnexpectedChildCrashFailClosed', 'FAIL')
        result['ApprovalBusinessVerdictIndependentFromTeardown'] = acceptance_payload.get('L118ApprovalLiveTeardownVerdictMachineReadable', 'FAIL')
        result['ZeroRequestIdNormalization'] = acceptance_payload.get('L119ZeroRequestIdNormalization', 'FAIL')
        result['ZeroRequestResolvedMatch'] = acceptance_payload.get('L120ZeroRequestResolvedMatch', 'FAIL')
        result['StructuredStdoutWarningScannerIsolation'] = acceptance_payload.get('L121StructuredStdoutWarningScannerIsolation', 'FAIL')
        result['RawStderrWarningAuthority'] = acceptance_payload.get('L122RawStderrWarningAuthority', 'FAIL')
        result['ParentFinalTeardownAuthority'] = acceptance_payload.get('L123ParentFinalTeardownAuthority', 'FAIL')
        result['ChildPreExitFinalVerdictSuppressed'] = acceptance_payload.get('L123ParentFinalTeardownAuthority', 'FAIL')
        result['ExpiringCachePendingDetection'] = acceptance_payload.get('L124ExpiringCachePendingFailsTeardown', 'FAIL')
        result['ApprovalLiveCacheTeardownContract'] = acceptance_payload.get('L125ApprovalLiveCleanCacheTeardownContract', 'FAIL')
        result['ZeroIdExactlyOnceEvidence'] = acceptance_payload.get('L126ZeroIdExactlyOnceEvidence', 'FAIL')
        result['ResolvedNotificationEvidenceContract'] = result['ZeroRequestResolvedMatch']
        result['ZeroIdExactlyOnceEvidenceContract'] = result['ZeroIdExactlyOnceEvidence']
        result['CardV2RegressionAfterHostEvidenceFix'] = acceptance_payload.get('L127CardV2RegressionAfterHostEvidenceFix', 'FAIL')
        result['ApprovalBridgeRegressionAfterHostEvidenceFix'] = acceptance_payload.get('L128ApprovalBridgeRegressionAfterHostEvidenceFix', 'FAIL')
        result['ResolvedNotificationObservedIdDiagnostics'] = acceptance_payload.get('L129ResolvedNotificationObservedIdDiagnostics', 'FAIL')
        result['ExpectedServerRequestIdContract'] = result['ResolvedNotificationObservedIdDiagnostics']
        result['ObservedResolvedRequestIdNormalization'] = result['ResolvedNotificationObservedIdDiagnostics']
        result['ResolvedTargetZeroIdMatch'] = acceptance_payload.get('L130ResolvedTargetZeroIdMatch', 'FAIL')
        result['ResolvedTargetMultipleNotificationMatch'] = acceptance_payload.get('L131ResolvedTargetMultipleNotificationMatch', 'FAIL')
        result['ResolvedWrongIdFailClosed'] = acceptance_payload.get('L132ResolvedWrongIdFailClosed', 'FAIL')
        result['ResolvedNotificationEvidenceContract'] = acceptance_payload.get('L133ExactlyOnceTargetResolvedIdContract', 'FAIL')
        result['ZeroIdExactlyOnceEvidenceContract'] = result['ResolvedNotificationEvidenceContract']
        result['MarkerExactByteContract'] = acceptance_payload.get('L134MarkerExactByteContract', 'FAIL')
        result['MarkerMismatchDiagnostics'] = acceptance_payload.get('L135MarkerMismatchDiagnostics', 'FAIL')
        result['ProtectedOperationExactByteWrite'] = acceptance_payload.get('L136ProtectedOperationExactByteWrite', 'FAIL')
        result['CacheCronOwnerLoopContract'] = acceptance_payload.get('L137CacheCronCapturedOwnerLoopContract', 'FAIL')
        result['CacheCronTerminalBeforeChildReturn'] = acceptance_payload.get('L138CacheCronMustBeTerminalBeforeChildReturn', 'FAIL')
        result['CacheWrongLoopAccountingRegression'] = acceptance_payload.get('L139CacheWrongLoopAccountingRegression', 'FAIL')
        result['CacheOwnerLoopClosedPendingFails'] = acceptance_payload.get('L140CacheOwnerLoopClosedPendingFails', 'FAIL')
        result['ParentPostExitCacheWarningAuthority'] = acceptance_payload.get('L141ParentPostExitCacheWarningAuthority', 'FAIL')
        result['CurrentHost105322RegressionReplay'] = acceptance_payload.get('L142CurrentHost105322RegressionReplay', 'FAIL')
        result['CardV2RegressionAfterFinalTargetedFix'] = acceptance_payload.get('L143CardV2RegressionAfterFinalTargetedFix', 'FAIL')
        result['ApprovalBridgeRegressionAfterFinalTargetedFix'] = acceptance_payload.get('L144ApprovalBridgeRegressionAfterFinalTargetedFix', 'FAIL')
        result['ResolutionAuthoritiesSeparated'] = acceptance_payload.get('L145LiveResolvedAndSnapshotAuthoritySeparated', 'FAIL')
        result['LiveResolvedRequestIdMatchContract'] = 'PASS' if all(acceptance_payload.get(key) == 'PASS' for key in (
            'L145LiveResolvedAndSnapshotAuthoritySeparated',
            'L146WrongLiveIdCorrectSnapshotStillFails',
            'L147CorrectLiveIdWrongSnapshotStillFails',
            'L148CorrectLiveIdCorrectSnapshotPasses',
        )) else 'FAIL'
        result['DurableResolvedRequestIdMatchContract'] = result['LiveResolvedRequestIdMatchContract']
        result['WrongLiveIdCorrectSnapshotStillFails'] = acceptance_payload.get('L146WrongLiveIdCorrectSnapshotStillFails', 'FAIL')
        result['CorrectLiveIdWrongSnapshotStillFails'] = acceptance_payload.get('L147CorrectLiveIdWrongSnapshotStillFails', 'FAIL')
        result['CorrectLiveIdCorrectSnapshotPasses'] = acceptance_payload.get('L148CorrectLiveIdCorrectSnapshotPasses', 'FAIL')
        result['ExactlyOnceRequiresBothResolutionAuthorities'] = acceptance_payload.get('L149ExactlyOnceRequiresBothResolutionAuthorities', 'FAIL')
        result['ResolutionFieldCollisionRegression'] = acceptance_payload.get('L150ResolutionFieldCollisionRegression', 'FAIL')
        result['ResolutionSnapshotContract'] = 'PASS' if all(acceptance_payload.get(key) == 'PASS' for key in (
            'L146WrongLiveIdCorrectSnapshotStillFails',
            'L147CorrectLiveIdWrongSnapshotStillFails',
            'L148CorrectLiveIdCorrectSnapshotPasses',
        )) else 'FAIL'
        result['CacheCronOwnerLoopStateContract'] = acceptance_payload.get('L151CacheOpenNonRunningOrphanLoopDrain', 'FAIL')
        result['CacheCronOwnerThreadContract'] = acceptance_payload.get('L156CacheForeignRunningLoopThreadSafeDrain', 'FAIL')
        result['CacheOpenNonRunningOrphanLoopDrain'] = acceptance_payload.get('L151CacheOpenNonRunningOrphanLoopDrain', 'FAIL')
        result['CacheSameTaskTrackedToTerminal'] = acceptance_payload.get('L152CacheSameTaskTrackedToTerminal', 'FAIL')
        result['CacheCancelWithoutDrainFails'] = acceptance_payload.get('L153CacheCancelWithoutDrainFails', 'FAIL')
        result['CacheClosedPendingLoopFails'] = acceptance_payload.get('L154CacheClosedPendingLoopFails', 'FAIL')
        result['CacheTerminalThenLoopClosePasses'] = acceptance_payload.get('L155CacheTerminalThenLoopClosePasses', 'FAIL')
        result['CacheForeignRunningLoopThreadSafeDrain'] = acceptance_payload.get('L156CacheForeignRunningLoopThreadSafeDrain', 'FAIL')
        result['Host114236CacheShapeRegression'] = acceptance_payload.get('L157Host114236CacheShapeRegression', 'FAIL')
        result['ParentPostExitCacheAuthorityRegression'] = acceptance_payload.get('L158ParentPostExitCacheAuthorityRegression', 'FAIL')
        result['ApprovalBusinessPathUnchangedRegression'] = acceptance_payload.get('L159ApprovalBusinessPathUnchangedRegression', 'FAIL')
        result['ResolutionMarkerRegressionAfterCacheFix'] = acceptance_payload.get('L160ResolutionMarkerRegressionAfterCacheFix', 'FAIL')
        bg_pre_stop_mapping = {
            'L203BgPreStopSdkCancelHelperBypassed': 'BgPreStopSdkCancelHelperBypassed',
            'L204BgPreStopRunningLoopTerminalDrain': 'BgPreStopRunningLoopTerminalDrain',
            'L205BgPreStopNoSleepCoroutineBarrier': 'BgPreStopNoSleepCoroutineBarrier',
            'L206BgPreStopCancellationResistantTaskTerminal': 'BgPreStopCancellationResistantTaskTerminal',
            'L207BgPreStopLoopStopRequiresTerminal': 'BgPreStopLoopStopRequiresTerminal',
            'L208Host061707SleepOrphanShapeRegression': 'Host061707SleepOrphanShapeRegression',
            'L209DeclineLiveMarkerAbsenceMetadata': 'DeclineLiveMarkerAbsenceMetadata',
            'L210DeclineMarkerPresenceStillFailsAbsence': 'DeclineMarkerPresenceStillFailsAbsence',
            'L211ParentSleepWarningAuthorityRegression': 'ParentSleepWarningAuthorityRegression',
            'L212ApprovalUxRegressionAfterBgFix': 'ApprovalUxRegressionAfterBgFix',
        }
        for source, target in bg_pre_stop_mapping.items():
            result[target] = acceptance_payload.get(source, 'FAIL')
        serialization_mapping = {
            'L219StartFutureCancelNotWorkerTerminal': 'StartFutureCancelNotWorkerTerminal',
            'L220BgOwnershipHandoffBlocksNewScheduling': 'BgOwnershipHandoffBlocksNewScheduling',
            'L221BgOwnershipHandoffDetachesSdkCleanupAuthority': 'BgOwnershipHandoffDetachesSdkCleanupAuthority',
            'L222LateStartWorkerCleanupCannotCreateSleep': 'LateStartWorkerCleanupCannotCreateSleep',
            'L223StartWorkerTerminalBeforeFinalBgDrain': 'StartWorkerTerminalBeforeFinalBgDrain',
            'L224BgProducerQuiescenceBeforeFinalDrain': 'BgProducerQuiescenceBeforeFinalDrain',
            'L225FinalBgDrainBeforeCapturedLoopStop': 'FinalBgDrainBeforeCapturedLoopStop',
            'L226StartWorkerTimeoutFailsClosed': 'StartWorkerTimeoutFailsClosed',
            'L227TransportLoopAliveUntilWorkerTerminal': 'TransportLoopAliveUntilWorkerTerminal',
            'L228Host072914LateSleepRaceRegression': 'Host072914LateSleepRaceRegression',
            'L229SharedShutdownSerializationRegression': 'SharedShutdownSerializationRegression',
            'L230ApprovalBusinessUxUnchangedAfterSerialization': 'ApprovalBusinessUxUnchangedAfterSerialization',
        }
        for source, target in serialization_mapping.items():
            result[target] = acceptance_payload.get(source, 'FAIL')
        result['CacheCronAwaitedToTerminal'] = 'PASS' if all(result[key] == 'PASS' for key in (
            'CacheOpenNonRunningOrphanLoopDrain',
            'CacheSameTaskTrackedToTerminal',
            'CacheTerminalThenLoopClosePasses',
        )) else 'FAIL'
        result['BgThreadCapture'] = result['PreShutdownCapture']
        result['BgLoopClosedByCfr'] = result['BgAllPendingTaskDrain']
        result['WsLoopPreserved'] = result['CfrLoopPreserved']
        result['TransportShutdownSequencing'] = 'PASS' if result['SdkTaskDrainContract'] == 'PASS' and result['ShutdownProbeProcessExit'] == 'PASS' else 'FAIL'
        result['M3CredentialApiSpec'] = 'PASS' if all(token in '\n'.join((ROOT / 'docs' / name).read_text(encoding='utf-8') for name in ('M3_CONTROL_API_SPEC.md', 'M3_CONTROL_CENTER_SECURITY.md')) for token in ('/api/v1/settings/feishu', 'app_secret_configured', 'write-only', 'CSRF')) else 'FAIL'
        result['ChannelRuntimeWarnings'] = 'PASS' if acceptance_payload.get('L51ChannelShutdownCleanup') == 'PASS' else 'FAIL'
        result['ApprovalLiveValidation'] = 'HOST_RUN_REQUIRED'
        result['ApprovalAllowLive'] = 'HOST_RUN_REQUIRED'
        result['ApprovalDeclineLive'] = 'HOST_RUN_REQUIRED'
        result['ApprovalSchemaContract'] = 'PASS' if result['ApprovalSchemaVerified'] == 'PASS' and acceptance_payload.get('L22ApprovalSchema') == 'PASS' else 'FAIL'
        result['ApprovalSchemaVerified'] = 'HOST_RUN_REQUIRED'
        result['ApprovalSchemaGeneration'] = 'HOST_RUN_REQUIRED'
    except Exception:
        result['M2LocalAcceptance'] = 'FAIL'
        result['AcceptanceError'] = (acceptance.stderr or acceptance.stdout)[-500:]
    if args.run_real_codex:
        integration = run([sys.executable, str(ROOT / 'scripts' / 'run_m2_codex_integration.py'), '--gate-origin', args.gate_origin], timeout=240)
        try:
            integration_payload = json.loads((integration.stdout or '').strip().splitlines()[-1])
            (artifact_dir / 'codex_integration.json').write_text(json.dumps(integration_payload, indent=2), encoding='utf-8')
            for key in ('RealCodexFakeFeishuNewPending', 'RealCodexFakeFeishuCreate', 'RealCodexFakeFeishuResume', 'RealCodexFakeFeishuRestartRecovery', 'RealCodexFakeFeishuDedupe', 'RealCodexFakeFeishuStatus', 'RealCodexFakeFeishuUnbound', 'RealCodexFakeFeishuBoundary'):
                result[key] = integration_payload.get(key, 'FAIL')
            result['M2CodexIntegration'] = integration_payload.get('Verdict', 'FAIL')
        except Exception:
            result['RealCodexIntegrationError'] = (integration.stderr or integration.stdout)[-500:]
    implementation_ready = result['Compile'] == 'PASS' and result['UnitTests'].startswith('PASS') and result['M2LocalAcceptance'] == 'PASS' and result['AppServerApprovalRoundtrip'] == 'PASS' and all(result[key] == 'PASS' for key in ('ConfigSecurity', 'MessageDedupe', 'DaemonSingleton', 'DurableInbox', 'SessionRecovery', 'CommandRouter', 'StopControlLane', 'ReplyIdempotency', 'ReplyFailureRecovery', 'ReplyUuidStableAcrossRetry', 'SetupOnlyNoExecution', 'BotMentionDetection', 'ApprovalSchemaContract', 'ApprovalCardV2Contract', 'ApprovalCallbackV2Contract', 'ApprovalCardSendFailureFailClosed', 'ApprovalLiveTeardownContract', 'ApprovalBusinessVerdictIndependentFromTeardown', 'BusinessFailureCleanTeardown', 'BusinessFailureTeardownWarningDetected', 'BusinessPassCannotMaskTeardownFailure', 'ApprovalLiveTeardownNotRunContract', 'UnexpectedChildCrashFailClosed', 'ZeroRequestIdNormalization', 'ZeroRequestResolvedMatch', 'StructuredStdoutWarningScannerIsolation', 'RawStderrWarningAuthority', 'ParentFinalTeardownAuthority', 'ExpiringCachePendingDetection', 'ApprovalLiveCacheTeardownContract', 'ZeroIdExactlyOnceEvidence', 'ResolvedNotificationEvidenceContract', 'ZeroIdExactlyOnceEvidenceContract', 'ResolvedNotificationObservedIdDiagnostics', 'ExpectedServerRequestIdContract', 'ObservedResolvedRequestIdNormalization', 'ResolvedTargetZeroIdMatch', 'ResolvedTargetMultipleNotificationMatch', 'ResolvedWrongIdFailClosed', 'MarkerExactByteContract', 'MarkerMismatchDiagnostics', 'ProtectedOperationExactByteWrite', 'CacheCronOwnerLoopContract', 'CacheCronTerminalBeforeChildReturn', 'CacheWrongLoopAccountingRegression', 'CacheOwnerLoopClosedPendingFails', 'ParentPostExitCacheWarningAuthority', 'CurrentHost105322RegressionReplay', 'CardV2RegressionAfterFinalTargetedFix', 'ApprovalBridgeRegressionAfterFinalTargetedFix', 'CardV2RegressionAfterHostEvidenceFix', 'ApprovalBridgeRegressionAfterHostEvidenceFix', 'ChannelRealReadySemantics', 'NormalDaemonLifetime', 'SendResultFailureHandling', 'CardActionNestedMapping', 'RawContentTypeBoundary', 'UnknownSenderFailClosed', 'DurableReplyChatId', 'ResumeAcceptanceSeparate', 'MultipleApprovalCards', 'ApprovalSendFailureCleanup', 'SetupOnlySingletonLease', 'SetupOnlyHeartbeat', 'CardPayloadWrapped', 'TopicScopeBoundary', 'UnknownChatTypeFailClosed', 'SdkProbeExitSemantics', 'ChannelContentTextContract', 'ChannelContentTextPrecedence', 'LegacyBodyTextFallback', 'MissingChatTypeFailClosed', 'NonTextContentTextBlocked', 'SdkLoopPreimportBoundary', 'SdkRunningLoopConflictClassification', 'TransportFailedStartupCleanup', 'SdkCompatibilityMode', 'NativeProbeContract', 'ChannelShutdownClean', 'SensitiveSdkLogging', 'ApprovalLiveProbeContract', 'ApprovalExactlyOnceDeterministic', 'ApprovalDeclineFailClosed', 'ApprovalPendingCleanup', 'SchemaUsesOutDirectory', 'EffectiveThreadConfigEvidence', 'ServerRequestResolvedEvidence', 'ExactlyOnceEvidence', 'ApprovalProbeAllowMode', 'ApprovalProbeDeclineMode', 'M3FrozenV2Metadata', 'ShutdownProbeProcessExit', 'PostExitWarningDetection', 'SdkTaskDrainContract', 'SdkTaskOriginDetection', 'SdkTaskDrainBehavior', 'NonSdkTaskPreserved', 'ShutdownProbeSyntheticPassRemoved', 'PreShutdownCapture', 'WsPingLoopDrain', 'CacheOrphanLoopDrain', 'CaptureSurvivesSdkReferenceClear', 'NoSyntheticZeroWhenObservationUnavailable', 'BgLoopPreShutdownCapture', 'BgThreadCapture', 'BgSleepSentinelDrain', 'BgAllPendingTaskDrain', 'BgThreadExitEvidence', 'CfrLoopPreserved', 'WsLoopPreserved', 'BgLoopClosedByCfr', 'PostExitWarningBehaviorTest', 'ClosedByUpstreamCleanlyClassification', 'ClosedWithPendingWarningFailClosed', 'ClosedWithNeverAwaitedFailClosed', 'ClosedWithLiveThreadFailClosed', 'ClosedBeforePublicStopFailClosed', 'ExistingCfrDrainPathRegression', 'DeviceFlowCapture', 'DeviceFlowPreClose', 'DeviceFlowPreCloseTimeoutFailClosed', 'DeviceFlowSchedulingFailureNoNeverAwaited', 'DeviceFlowPendingTaskWarningFailClosed', 'DeviceFlowNeverAwaitedWarningFailClosed', 'DeviceFlowExactCloseLifecycleCapture', 'DeviceFlowPreCloseTerminalContract', 'DeviceFlowDoubleCloseIdempotent', 'DeviceFlowPublicStopAfterPreCloseSafe', 'DeviceFlowRawCoroutineLeakPrevention', 'DeviceFlowTimeoutTaskTerminal', 'DeviceFlowForeignLoopThreadSafeClose', 'DeviceFlowOpenNonRunningLoopClose', 'Host040651DeviceFlowRegression', 'ExpiringCacheRegressionAfterDeviceFlowFix', 'ApprovalBusinessRegressionAfterDeviceFlowFix', 'DeviceFlowPostExitWarningAuthority', 'WsCacheBgRegression', 'PersistentConfigStore', 'LocalSecretStore', 'CredentialResolver', 'CredentialCli', 'PlaintextSecretFallback', 'M3CredentialApiSpec'))
    authority_ready = all(result[key] == 'PASS' for key in ('ResolutionAuthoritiesSeparated', 'LiveResolvedRequestIdMatchContract', 'DurableResolvedRequestIdMatchContract', 'WrongLiveIdCorrectSnapshotStillFails', 'CorrectLiveIdWrongSnapshotStillFails', 'CorrectLiveIdCorrectSnapshotPasses', 'ExactlyOnceRequiresBothResolutionAuthorities', 'ResolutionFieldCollisionRegression', 'ResolutionSnapshotContract'))
    cache_ready = all(result[key] == 'PASS' for key in ('CacheCronOwnerLoopStateContract', 'CacheCronOwnerThreadContract', 'CacheOpenNonRunningOrphanLoopDrain', 'CacheSameTaskTrackedToTerminal', 'CacheCancelWithoutDrainFails', 'CacheClosedPendingLoopFails', 'CacheTerminalThenLoopClosePasses', 'CacheForeignRunningLoopThreadSafeDrain', 'Host114236CacheShapeRegression', 'CacheCronAwaitedToTerminal', 'ParentPostExitCacheAuthorityRegression', 'ApprovalBusinessPathUnchangedRegression', 'ResolutionMarkerRegressionAfterCacheFix'))
    bg_pre_stop_ready = all(result[key] == 'PASS' for key in (
        'BgPreStopSdkCancelHelperBypassed', 'BgPreStopRunningLoopTerminalDrain',
        'BgPreStopNoSleepCoroutineBarrier', 'BgPreStopCancellationResistantTaskTerminal',
        'BgPreStopLoopStopRequiresTerminal', 'Host061707SleepOrphanShapeRegression',
        'DeclineLiveMarkerAbsenceMetadata', 'DeclineMarkerPresenceStillFailsAbsence',
        'ParentSleepWarningAuthorityRegression', 'ApprovalUxRegressionAfterBgFix',
    ))
    serialization_ready = all(result.get(key) == 'PASS' for key in (
        'StartFutureCancelNotWorkerTerminal', 'BgOwnershipHandoffBlocksNewScheduling',
        'BgOwnershipHandoffDetachesSdkCleanupAuthority', 'LateStartWorkerCleanupCannotCreateSleep',
        'StartWorkerTerminalBeforeFinalBgDrain', 'BgProducerQuiescenceBeforeFinalDrain',
        'FinalBgDrainBeforeCapturedLoopStop', 'StartWorkerTimeoutFailsClosed',
        'TransportLoopAliveUntilWorkerTerminal', 'Host072914LateSleepRaceRegression',
        'SharedShutdownSerializationRegression', 'ApprovalBusinessUxUnchangedAfterSerialization',
    ))
    implementation_ready = implementation_ready and authority_ready and cache_ready and bg_pre_stop_ready and serialization_ready
    ux_gate_ready = all(result[key] == 'PASS' for key in (
        'ApprovalFeedbackRendererContract', 'ApprovalFeedbackStateMachineContract',
        'ApprovalFeedbackPendingCardContract', 'ApprovalFeedbackAckProcessingContract',
        'ApprovalFeedbackApprovedContract', 'ApprovalFeedbackDeclinedContract',
        'ApprovalFeedbackExecutionFailedContract', 'ApprovalFeedbackButtonsDisabledAfterDecision',
        'ApprovalFeedbackMonotonicStateContract', 'ApprovalFeedbackDuplicateSameDecisionContract',
        'ApprovalFeedbackOppositeDecisionContract', 'ApprovalFeedbackWrongOperatorContract',
        'ApprovalFeedbackUpdateFailureContract', 'ApprovalFeedbackOriginalMessageUpdateContract',
        'ApprovalFeedbackNoSecretFields', 'DeclineMarkerAbsenceContract',
        'DeclineMarkerExactByteContractSemantics', 'CurrentBlockingIssuesCleanup',
        'ApprovalDecisionFeedbackUxContract', 'TurnCompletedNestedTurnIdContract',
        'FinalFeedbackTurnBindingContract', 'FinalFeedbackNoRequestIdDependency',
        'FinalFeedbackAcceptCompletedContract', 'FinalFeedbackDeclineContract',
        'FinalFeedbackExecutionFailedContract', 'FinalFeedbackIdempotentTerminalContract',
        'ProductionDaemonFinalFeedbackContract', 'ProductionDaemonInitialTurnFinalFeedbackContract',
        'LiveProbeFinalFeedbackBindingContract', 'LiveProbeFinalFeedbackWaitContract',
        'RealHostTurnCompletedShapeRegression',
    ))
    implementation_ready = implementation_ready and ux_gate_ready
    implementation_ready = result['Compile'] == 'PASS' and result['UnitTests'].startswith('PASS') and result['M2LocalAcceptance'] == 'PASS' and authority_ready and cache_ready and bg_pre_stop_ready and serialization_ready and ux_gate_ready
    result['M2A_Verdict'] = 'CFR_M2A_IMPLEMENTATION_READY_HOST_GATE_PENDING' if implementation_ready else 'CFR_M2A_IMPLEMENTATION_INCOMPLETE'
    result['M2B_Verdict'] = 'CFR_M2B_APPROVAL_LIVE_HOST_RETEST_REQUIRED' if result['ApprovalBridgeUnit'] == 'PASS' and result['NoAutoApprove'] == 'PASS' and all(result[key] == 'PASS' for key in ('AppServerApprovalRoundtrip', 'ApprovalTimeoutDecline', 'ApprovalCancel', 'WrongOperatorReject', 'MultipleApprovalCards', 'ApprovalSendFailureCleanup', 'ApprovalCardV2Contract', 'ApprovalCallbackV2Contract', 'ApprovalCardSendFailureFailClosed', 'ApprovalLiveTeardownContract', 'ApprovalBusinessVerdictIndependentFromTeardown', 'BusinessFailureCleanTeardown', 'BusinessFailureTeardownWarningDetected', 'BusinessPassCannotMaskTeardownFailure', 'ApprovalLiveTeardownNotRunContract', 'UnexpectedChildCrashFailClosed', 'ZeroRequestIdNormalization', 'ZeroRequestResolvedMatch', 'ResolvedNotificationObservedIdDiagnostics', 'ResolvedTargetZeroIdMatch', 'ResolvedTargetMultipleNotificationMatch', 'ResolvedWrongIdFailClosed', 'ResolvedNotificationEvidenceContract', 'ZeroIdExactlyOnceEvidenceContract', 'MarkerExactByteContract', 'MarkerMismatchDiagnostics', 'ProtectedOperationExactByteWrite', 'CacheCronOwnerLoopContract', 'CacheCronTerminalBeforeChildReturn', 'CacheWrongLoopAccountingRegression', 'CacheOwnerLoopClosedPendingFails', 'ParentPostExitCacheWarningAuthority', 'CurrentHost105322RegressionReplay', 'CardV2RegressionAfterFinalTargetedFix', 'ApprovalBridgeRegressionAfterFinalTargetedFix', 'StructuredStdoutWarningScannerIsolation', 'RawStderrWarningAuthority', 'ParentFinalTeardownAuthority', 'ExpiringCachePendingDetection', 'ApprovalLiveCacheTeardownContract', 'ChannelShutdownClean', 'SensitiveSdkLogging', 'ApprovalLiveProbeContract', 'SchemaUsesOutDirectory', 'EffectiveThreadConfigEvidence', 'ServerRequestResolvedEvidence', 'ExactlyOnceEvidence')) else 'CFR_M2B_IMPLEMENTATION_INCOMPLETE'
    result['M2_Verdict'] = 'CFR_M2_FEISHU_CODEX_REMOTE_COMPLETE' if result['M2A_Verdict'].endswith('COMPLETE') and result['M2B_Verdict'].endswith('COMPLETE') else 'CFR_M2_FEISHU_CODEX_REMOTE_PARTIAL'
    result['M2A_Status'] = 'IMPLEMENTATION_READY_HOST_GATE_PENDING' if implementation_ready else 'IMPLEMENTATION_INCOMPLETE'
    if not authority_ready:
        result['M2B_Verdict'] = 'CFR_M2B_IMPLEMENTATION_INCOMPLETE'
    shutdown_required = result['ProductionShutdownHostRegressionRequired'] == 'YES'
    shutdown_satisfied = result['ProductionShutdownHostRegressionSatisfied'] == 'YES'
    stop_required = any(result.get(key) in {'FAIL', 'YES'} for key in (
        'UnexpectedScopeExpansion',
    ))
    host_next, blockers, routing_contract = resolve_gate_routing(
        deterministic_fail=not implementation_ready,
        stop_required=stop_required,
        shutdown_required=shutdown_required,
        shutdown_satisfied=shutdown_satisfied,
        allow_ux_required=True,
        decline_ux_required=False,
        m1_required=True,
    )
    result['GateRoutingConsistencyContract'] = routing_contract
    result['ShutdownHostPriorityOverAllowUx'] = 'PASS' if (
        not shutdown_required or shutdown_satisfied or host_next == 'SHUTDOWN_HOST_REGRESSION'
    ) else 'FAIL'
    result['ImplementationReadyCannotOverrideShutdownRegression'] = 'PASS' if (
        not implementation_ready or not shutdown_required or shutdown_satisfied or host_next == 'SHUTDOWN_HOST_REGRESSION'
    ) else 'FAIL'
    result['M2B_Status'] = 'SHUTDOWN_HOST_REGRESSION_REQUIRED' if shutdown_required and not shutdown_satisfied else ('APPROVAL_LIVE_HOST_RETEST_REQUIRED' if result['M2B_Verdict'] == 'CFR_M2B_APPROVAL_LIVE_HOST_RETEST_REQUIRED' else 'IMPLEMENTATION_INCOMPLETE')
    if implementation_ready:
        result['M2B_Verdict'] = 'CFR_M2B_APPROVAL_DECISION_FEEDBACK_HOST_REQUIRED'
    result['BlockingIssues'] = blockers
    result['HostNext'] = host_next
    result['M2DeterministicVerdict'] = 'PASS' if implementation_ready and routing_contract == 'PASS' else 'FAIL'
    result['DeterministicImplementation'] = result['M2DeterministicVerdict']
    result['HostCodexValidation'] = result['M2CodexIntegration']
    result['FeishuSdkValidation'] = result['FeishuSdkProbe']
    result['ApprovalCardPayload'] = result['CardPayloadWrapped']
    result['ApprovalCardAction'] = result['CardActionNestedMapping']
    (artifact_dir / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    (artifact_dir / 'm2-gate.log').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({'ArtifactDir': str(artifact_dir), **result}))
    return 0 if implementation_ready else 1


if __name__ == '__main__':
    raise SystemExit(main())
