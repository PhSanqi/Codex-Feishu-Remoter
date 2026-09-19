# CFR M2 Feishu Corrective Transport & Acceptance Report

## Scope

M2 is a direct Feishu-to-CFR gateway. Broker, Router, Workspace Arbitrator, RouteBinding, and cross-system workspace leases remain out of scope.

The conversational live path is `lark-channel-sdk` / `lark_channel.FeishuChannel`. It owns WS lifecycle, reconnect, normalized messages, exact bot mention state, card actions, and outbound delivery. `lark-oapi` remains optional for other OpenAPI surfaces and is not the live conversational transport.

## Corrective findings

| Finding | Result |
|---|---|
| AppServer server-request handler return value was discarded | FIXED: result/error is written as one JSON-RPC response, with safe unknown-request rejection and late-close guard |
| Real Codex/Fake Feishu integration checks were weak or unconditional | FIXED: `CountingCodexAdapter` verifies create/send/restart/dedupe/status/boundary behavior against real CFR stores and real daemon/gateway |
| Raw `lark-oapi` typed-event introspection was unstable | FIXED: live default is `ChannelFeishuTransport` backed by official Channel SDK |
| Reply reservation poisoned retries after send failure | FIXED: durable `pending/sent/failed` state, retryable failure, stable UUID |
| setup-only initialized the execution daemon | FIXED: setup-only connects transport only; no daemon, recovery, workers, CodexAdapter, or state mutation |
| Group validation accepted any mention | FIXED: requires `enable_group_chats` and normalized `mentioned_bot=true` |
| Channel readiness was reported before SDK readiness | FIXED: uses SDK `connect_until_ready` and `is_ready` |
| Normal `feishu run` exited after readiness | FIXED: waits for transport/daemon lifetime |
| Channel `SendResult(success=false)` could become sent via fallback | FIXED: mapped to safe `StructuredError`, no fallback id |
| Card actions read top-level fields instead of nested SDK fields | FIXED: maps `event.operator.open_id` and `event.action.value` |
| Live content type was defaulted to text | FIXED: uses `raw_content_type`, unknown/binary content fails closed |
| Reply target depended on in-memory message-to-chat cache | FIXED: explicit durable inbox `chat_id` is passed on every daemon reply |
| Resume acceptance could be overwritten by restart acceptance | FIXED: separate integration fields and required tuple |
| Approval cards shared one reply key within a prompt | FIXED: phase includes approval id |
| setup-only/doctor could create a parallel Channel connection | FIXED: shared connection lease with heartbeat |
| Channel card payload was passed as a raw card object | FIXED: `send_card` sends the Channel SDK shape `{"card": card_payload}` |
| Topic and unknown chat scope could fall through the group predicate | FIXED: `p2p` is explicit, `group/topic` require opt-in plus bot mention, all other values deny |
| SDK probe returned success when the SDK was absent | FIXED: probe emits machine-readable surface fields and exits `2` for missing SDK/API |
| Channel message fixtures used CFR's `body_text` shape instead of the official normalized field | FIXED: `content_text` is primary, with `body_text` retained only as a compatibility fallback |
| Missing Channel `chat_type` defaulted to private chat | FIXED: missing values normalize to `unknown` and are denied by the explicit scope policy |
| Channel SDK module-global WS loop conflicted with CFR's running loop | FIXED: SDK import/construction happens synchronously in the transport thread before CFR creates its asyncio loop; compatibility is feature-detected and isolated |

## Latest corrective acceptance

`scripts/run_m2_feishu_local_acceptance.py` writes `.tmp/m2-feishu-local/<RunId>/result.json` and currently covers L01-L150. It uses temporary SQLite, an allowlisted temporary workspace, fake transport, and fake Codex only for deterministic local checks. L77-L82 are behavioral shutdown checks covering pre-stop capture, WS ping-loop drain, the distinct cache orphan loop, capture surviving SDK reference clearing, unavailable-observation truthfulness, and non-SDK task preservation. L83-L88 cover dedicated Channel BG-loop capture, exact orphan `asyncio.sleep` drain, all-task drain, reference-clear survival, CFR/WS loop preservation, and fresh-process post-exit warning behavior. L89-L94 cover clean upstream close classification and fail-closed warning, live-thread, pre-public-close, and existing CFR-drain paths. L95-L103 cover DeviceFlow pre-close scheduling/completion, bounded timeout cancellation, scheduling-failure coroutine closure, public timeout/pending/never-awaited hard-fail evidence, idempotent pre-close/public close, and WS/cache/BG regression. L104-L112 cover Card JSON 2.0 contract, callback normalization/rejection, send-failure fail-closed evidence, and parent-authoritative live-probe teardown. L113-L118 cover independent business/lifecycle verdicts, clean business failure, teardown warning detection, teardown masking prevention, NOT_RUN semantics, unexpected child crash handling, and machine-readable classification. L119-L128 cover zero-safe request-id normalization/matching, structured-stdout scanner isolation, raw-stderr warning authority, parent-only final teardown authority, ExpiringCache pending detection, clean cache teardown, zero-id exactly-once evidence, and Card V2/ApprovalBridge regression. L129-L144 cover raw/normalized resolved-ID diagnostics, multiple-event target matching, byte-exact marker verification and mismatch evidence, captured cache owner-loop/terminal checks, current Host failure replay, and Card V2/ApprovalBridge regressions. L145-L150 cover separated live/durable resolution authorities, wrong-live/correct-snapshot and correct-live/wrong-snapshot fail-closed integration, both-correct pass, exactly-once authority truth table, and collision regression.

Required local fields include:

```text
L01-L103: PASS

The M2B approval path now uses a single CFR Approval Card JSON 2.0 builder. Schema 2.0 cards contain no legacy `tag:"action"` container; the two buttons are `tag:"button"` elements with JSON 2.0 callback behaviors and bounded `approval_id`/action values only. Card delivery is a hard boundary: a failed `SendResult` is recorded as `FEISHU_APPROVAL_CARD_SEND_FAILED` (with sanitized provider/error evidence when available), the probe never prints `WAITING_FOR_FEISHU_APPROVAL` before a sent-card record exists, and the request resolves fail-closed to decline without being reported as an Allow pass.

The M2B live probe uses a parent/child process boundary. The parent relays child stdout in real time, reads child evidence only after process exit, treats structured child stdout as protocol data, scans raw stderr plus non-protocol stdout for pending-task, never-awaited, RuntimeWarning, event-loop-closed, and DeviceFlow timeout evidence, and requires the existing `ChannelFeishuTransport` disconnect/thread-exit contract on every child exit path. Approval live teardown is independently classified from business Allow/Decline evidence; the child emits `PENDING_PARENT_EXIT`, and only the parent emits final teardown/final verdict fields.

L01-L150: PASS (local deterministic resolved-ID, separated resolution authorities, byte-exact marker, cache-terminal, card, fail-closed, and parent/child teardown checks)
ChannelRealReadySemantics: PASS
NormalDaemonLifetime: PASS
SendResultFailureHandling: PASS
CardActionNestedMapping: PASS
RawContentTypeBoundary: PASS
UnknownSenderFailClosed: PASS
DurableReplyChatId: PASS
ResumeAcceptanceSeparate: PASS
MultipleApprovalCards: PASS
ApprovalSendFailureCleanup: PASS
SetupOnlySingletonLease: PASS
CardPayloadWrapped: PASS
TopicScopeBoundary: PASS
UnknownChatTypeFailClosed: PASS
SdkProbeExitSemantics: PASS
ChannelContentTextContract: PASS
ChannelContentTextPrecedence: PASS
LegacyBodyTextFallback: PASS
MissingChatTypeFailClosed: PASS
NonTextContentTextBlocked: PASS
AppServerApprovalRoundtrip: PASS (unit protocol test)
```

The SDK probe writes `.tmp/m2-feishu-sdk/<RunId>/result.json`. It reports the Channel SDK surface (`FeishuChannelImport`, `SendResultImport`, `EventsMessage`, and `EventsCardAction`). `Verdict=PASS` exits `0`; missing SDK or required API exits `2` and is never an automatic live PASS.

## Current gate boundary

Expected implementation-ready state without Host credentials or installed SDK:

```text
Compile: PASS
UnitTests: PASS
M2LocalAcceptance: PASS
AppServerApprovalRoundtrip: PASS
NoAutoApprove: PASS
TransportBackend: lark-channel-sdk
MessageNormalization: PASS
CardActionBackend: CHANNEL_NATIVE
ReplyFailureRecovery: PASS
ReplyUuidStableAcrossRetry: PASS
SetupOnlyNoExecution: PASS
BotMentionDetection: PASS
FeishuSdkProbe: PASS or FEISHU_SDK_NOT_INSTALLED
FeishuChannelNativeProbe: HOST_RUN_REQUIRED
CardPayloadWrapped: PASS
TopicScopeBoundary: PASS
UnknownChatTypeFailClosed: PASS
SdkProbeExitSemantics: PASS
ChannelContentTextContract: PASS
ChannelContentTextPrecedence: PASS
MissingChatTypeFailClosed: PASS
NonTextContentTextBlocked: PASS
M2CodexIntegration: HOST_RUN_REQUIRED
M1Regression: HOST_RUN_REQUIRED
M2A_Status: IMPLEMENTATION_READY_HOST_GATE_PENDING
M2B_Status: APPROVAL_LIVE_HOST_RETEST_REQUIRED
```

`M2DeterministicVerdict=PASS` means only deterministic implementation checks passed; it is not `M2LiveReady` and is not `M2_COMPLETE`.

M2 is not complete until the Host runs the real Codex integration, the post-change M1 regression, and live Feishu approval roundtrip. Blocking issues must include `FEISHU_SDK_INSTALL_REQUIRED` when the SDK is absent, plus the pending Host/live gates.

M2B deterministic readiness additionally covers timeout→decline, explicit cancel, wrong-operator rejection, multiple approval cards with distinct UUIDs, and card-send failure cleanup.

## Required Host sequence

Run in the user's own PowerShell session in this order; stop at the first failure:

```powershell
cd '<CFR_PROJECT_ROOT>'
python -m pip install -e ".[feishu]"
python .\scripts\run_m2_feishu_sdk_probe.py
python .\scripts\run_m2_codex_integration.py --timeout 90 --gate-origin host_manual
python .\scripts\run_m1_integration.py --timeout 90 --gate-origin host_manual
python .\scripts\run_cfr.py feishu doctor
python .\scripts\run_cfr.py feishu run --setup-only --show-identifiers
python .\scripts\run_cfr.py feishu run
```

Stop if the real Codex/Fake Feishu command fails. Never paste an App Secret into source, logs, artifacts, or chat.

## Channel SDK event-loop boundary

The live transport owns one dedicated thread. Its order is:

```text
transport thread
  -> synchronous Channel SDK import / compatibility inspection
  -> FeishuChannel construction and handler registration
  -> create CFR asyncio loop
  -> connect_until_ready / READY
```

`src/cfr/feishu/sdk_compat.py` never replaces CFR's active event loop. It only detects the SDK's legacy `lark_channel.ws.client.loop` shape and, when that private loop is already running or closed, rebinds the SDK reference to a fresh loop. The compatibility mode is machine-readable as `NOT_REQUIRED`, `PREIMPORT_ONLY`, or `LEGACY_GLOBAL_LOOP_REBIND`. SDK internals are not imported by Gateway, Daemon, or ApprovalBridge.

The native diagnostic is a fresh-process probe:

```powershell
python .\scripts\run_m2_feishu_channel_loop_probe.py --timeout 30
```

It separately evaluates official pre-import, lazy-import reproduction, and the fixed CFR transport. `CfrTransportAfterFix` must be `PASS` before setup-only is attempted. Missing credentials return `FEISHU_LIVE_SETUP_REQUIRED`; event-loop conflicts return `FEISHU_SDK_EVENT_LOOP_CONFLICT`. Probe artifacts are written under `.tmp/m2-feishu-channel-loop/<RunId>/` with sanitized logs.

## M2B approval-live and shutdown boundary

The Channel transport awaits public `FeishuChannel.disconnect()` before loop shutdown, uses a bounded five-second disconnect, drains CFR-owned tasks, allows two loop ticks, shuts down async generators, closes the loop, and joins the transport thread. `stop()` is idempotent and reports `FEISHU_CHANNEL_SHUTDOWN_TIMEOUT` on a bounded shutdown failure. Run the Host shutdown probe with:

```powershell
python .\scripts\run_m2_feishu_shutdown_probe.py --timeout 30
```

SDK logging defaults to `WARNING`; `CFR_FEISHU_SDK_LOG_LEVEL` accepts `WARNING`, `ERROR`, or `INFO`. Captured Feishu logs are sanitized for access keys, tickets, secrets, tokens, and authorization headers. No SDK private coroutine is awaited and no warning is suppressed.

The real M2B probe is:

```powershell
python .\scripts\run_m2b_feishu_approval_live_probe.py --timeout 180
```

It creates an ephemeral workspace, requests the actual Codex approval path, sends private Feishu cards, validates Allow once and Decline, and fails closed when no real approval request is observed. M2B is not COMPLETE until both real closed loops, exactly-once resolution, timeout/wrong-operator fail-closed behavior, clean shutdown, and clean sensitive logging pass on Host.

## Operator-Provided Host Evidence

The following is historical operator-provided Host evidence. It is explicitly labeled `MANUAL_HOST_EVIDENCE`; the Desktop Agent gate does not synthesize or promote these values:

```text
Feishu SDK Probe: PASS
RunId=20260818T081352Z-30ed5f95

Real Codex/Fake Feishu: PASS
RunId=20260818T081422Z-82fd82e7

M1 Post-change Regression: PASS
RunId=20260818T081511Z-0f8eb035

Native Channel Probe: PASS
Diagnosis=CFR_EVENT_LOOP_OWNERSHIP_FIXED
CompatibilityMode=PREIMPORT_ONLY

Real setup-only: READY
Real normal daemon: READY
/cfr help: PASS
/cfr new: PASS
LIVE_001: CFR_M2_LIVE_ACK_001
LIVE_002: CFR_M2_LIVE_ACK_002
/cfr status: Workspace=CFR; Session=bound; Active turn=no; Desktop=refresh_required
```

This supports `M2A Functional E2E: COMPLETE / MANUAL_HOST_EVIDENCE` in the report layer. Machine deterministic gates remain `HOST_RUN_REQUIRED` until the current Host run records fresh artifacts.

## M3 Local Control Plane v2

`M3Architecture=FROZEN_v2`, `PrimaryExperience=WEB_CONTROL_PLANE`, `PrimaryLauncher=LOCAL_BACKEND_LAUNCHER`, `DesktopExeRequired=NO`, `OptionalTrayShell=DEFERRED`, and `M3Implementation=NOT_STARTED`.

The browser is the primary experience. The future launcher target is `python .\scripts\run_cfr_control.py`, binding a local backend to `127.0.0.1` and serving a web control UI. Tauri/EXE is optional future shell work, not a committed M3 requirement. The four v2 documents remain the architecture, API, optional shell, and security references.

## Final Host sequence

After this corrective pass, run only Allow first and stop at the first failure:

```powershell
cd '<CFR_PROJECT_ROOT>'
python .\scripts\run_m2_feishu_shutdown_probe.py --timeout 30
python .\scripts\run_m2b_feishu_approval_live_probe.py --mode allow --timeout 180
```

Run Decline only after the new Allow artifact is a complete Host PASS. Each M2B run uses a separate ephemeral thread and workspace. M2 remains partial until real Host evidence closes both paths.
## Shutdown and persistent credential boundary (current pass)

The shutdown probe is parent/child: the child writes only machine state, the
parent waits for real process exit, captures complete stdout/stderr, and then
scans post-exit warnings. `Task was destroyed`, `was never awaited`, runtime
loop conflicts, and resource warnings are recorded as blockers; no warning
filters or stderr suppression are used. CFR captures the Channel SDK's WS
client, start future, reconnect task, WS loop, cache cron task, and each task's
owner loop before public shutdown. It then awaits the public lifecycle method,
waits for the captured start future, drains only SDK-owned tasks on their
captured owner loops, and records remaining names. A distinct cache orphan
loop may be closed only after it is observed empty; a closed loop with a
pending task is reported as unavailable with a blocking issue rather than as
synthetic zero. DeviceFlow remains `NOT_OBSERVABLE` unless the SDK exposes an
explicit completion state.

Feishu credentials resolve in this order: process environment, persistent App
ID in the platform config directory, and App Secret in the OS keyring. The
CLI surface is:

```text
python .\scripts\run_cfr.py feishu credentials status
python .\scripts\run_cfr.py feishu credentials import-env
python .\scripts\run_cfr.py feishu credentials set-app-id <APP_ID>
python .\scripts\run_cfr.py feishu credentials set-secret
python .\scripts\run_cfr.py feishu credentials clear-secret
python .\scripts\run_cfr.py feishu credentials clear-all --yes
```

`status`, doctor output, artifacts, and logs expose only configured/source
metadata. There is no plaintext secret-file fallback. After importing a
rotated secret, clear the process environment and run
`scripts/run_feishu_credential_store_probe.py --live` in a fresh Host process
before live approval validation. Production shutdown and persistent
credentials are already PASS; the only current M2B Host gate is
`CFR_M2B_APPROVAL_LIVE_HOST_RETEST_REQUIRED`.
The dedicated Channel BG loop is a separate ownership boundary from the WS
module-global loop, cache loop, and CFR loop. CFR captures `_bg_loop`,
`_bg_thread`, and strong references to every pending task before public stop.
After public stop it joins the captured thread, proves the thread exited, then
drains all remaining tasks on that captured loop, including generic
`asyncio.sleep` tasks left by the upstream drain sentinel. Only after the loop
is empty and async generators/default executor cleanup are complete does CFR
close that dedicated loop. The diagnostic records BG task names, cancellation,
remaining count, thread exit, loop close, and executor evidence.

Clean-close classification is provenance-aware. If the BG loop was open before
public shutdown, the captured BG thread exits, and the loop is already closed
by the upstream lifecycle afterward, the terminal state is
`CLOSED_BY_UPSTREAM_CLEANLY`; task enumeration is then unavailable by design,
so `BgTasksRemaining` remains `null`. The parent process is authoritative and
accepts that terminal state only with child exit code `0`, zero WS/cache/
DeviceFlow remaining tasks, and empty post-exit runtime/pending-warning scans.
If the loop was closed before public shutdown, closes while its thread is
alive, or post-exit warnings exist, the state is `CLOSED_UNCLEANLY` and the
probe fails. A loop that remains open follows the existing observed
`CLOSED_BY_CFR` drain path.

For the installed `lark-channel-sdk` 1.2.0 DeviceFlow timeout race (the SDK
public stop waits only two seconds for `DeviceFlowClient.close()`), CFR
captures DeviceFlow and HTTP ownership state before public shutdown, schedules
the feature-detected `DeviceFlowClient.close()` on the captured Channel BG
loop, and gives that compatibility pre-close a bounded 8-second budget. The
official `channel.disconnect()` still runs afterward. Pre-close timeout,
scheduling failure, the SDK public-stop timeout warning, pending
`DeviceFlowClient.close` tasks, and never-awaited warnings are hard failures;
CFR never clears `_http` or patches the SDK implementation. A pre-close result
records only boolean HTTP state and elapsed/error evidence, never credentials.

The prior real Host Allow attempt (`20260819T083735Z-3847314e`) is retained as
failure evidence, not a pass: Feishu returned error `230099` with card
extension code `200861` because `body.elements[1].tag=action` was rejected as
`CARD_JSON_V2_LEGACY_ACTION_CONTAINER`. The same run demonstrated a real
approval server request and correct no-auto-approve/fail-closed decline state;
it also exposed the probe's old teardown warning path. The corrective local
contract now requires V2 card delivery and parent-authoritative teardown
before the next Host Allow/Decline retest.

Approval live business and lifecycle verdicts are now separate machine-readable
domains. A business failure with clean transport teardown reports
`BusinessVerdict=FAIL`, `ApprovalLiveTeardownVerdict=PASS`, and final failure
without `FEISHU_APPROVAL_LIVE_TEARDOWN_FAILED`. Lifecycle warnings, leaked
threads/tasks, or an unexpected child crash set teardown `FAIL`; a transport
that never started reports teardown `NOT_RUN`. A business PASS can never mask
teardown failure. Deterministic acceptance L113-L118 covers these T1-T6
classification cases. The current authoritative status remains
`M2B=CFR_M2B_APPROVAL_LIVE_HOST_RETEST_REQUIRED`; the production shutdown Host
gate and persistent credential gate are not reopened.

Micro-corrective deterministic evidence (2026-08-19):

```text
Compile: PASS
UnitTests: PASS (203/203)
M2LocalAcceptance: PASS (L01-L150)
DeterministicGate: PASS
ApprovalBusinessVerdictIndependentFromTeardown: PASS
BusinessFailureCleanTeardown: PASS
BusinessFailureTeardownWarningDetected: PASS
BusinessPassCannotMaskTeardownFailure: PASS
ApprovalLiveTeardownNotRunContract: PASS
UnexpectedChildCrashFailClosed: PASS
ShutdownLifecycleCoreChanges: NONE
ProductionShutdownHostBaseline: UNCHANGED_PASS
PersistentCredentials: UNCHANGED_PASS
ShutdownHostRetestRequired: NO
PersistentCredentialsHostRequired: NO
M2B: CFR_M2B_APPROVAL_LIVE_HOST_RETEST_REQUIRED
M2: CFR_M2_FEISHU_CODEX_REMOTE_PARTIAL
M3: UNCHANGED_FROZEN_v2
HostNext: ALLOW
```

Previous deterministic artifacts:

```text
.tmp/m2-feishu-local/20260819T095324Z-e2db64f6
.tmp/m2-feishu/20260819T095335Z-e9b41238
```

## Targeted corrective: authoritative Host Allow evidence

The authoritative prior Host Allow artifact is preserved unchanged at:

```text
.tmp/m2b-approval-live/20260819T100803Z-c4eca20c
```

Its business evidence was real and positive through Card V2 delivery, operator
validation, `StoredDecision=accept`, JSON-RPC response, protected marker
creation/content, `ResolvedIdsContainsRequest=true`, and `InFlightEmpty=true`.
Its final Host Allow result remains a failure for these independent reasons:

```text
Allow callback: PASS
Decision: PASS
JSON-RPC response: PASS
Protected side effect: PASS
Resolution evidence: FAIL (requestId=0 was lost by truthiness normalization)
ExactlyOnce: FAIL
Post-exit teardown: FAIL
Allow Host final: FAIL
Decline: NOT_RUN
```

The artifact also records the true raw-stderr lifecycle blockers: two pending
tasks, `ExpiringCache.__del__` after loop closure, and a pending
`ExpiringCache._start_clear_cron()` task. The old parent scanner additionally
misclassified the child JSON field name `PostExitRuntimeWarnings` as a warning;
the corrective scanner now isolates structured stdout and keeps raw stderr as
the warning authority. The lifecycle blocker itself is not suppressed or
reclassified away. Approval Live cleanup now releases the Feishu transport
before closing the Codex client and records SDK/cache/BG teardown evidence for
the next Host retest; production shutdown core files remain unchanged.

The zero-safe `first_present` contract accepts numeric `0` and treats only
absent/`None` as missing. This is used by `ApprovalBridge` notification
matching and the live probe evidence path. Child processes emit only business
and pre-exit lifecycle evidence with `PENDING_PARENT_EXIT`; the parent performs
raw post-exit scanning and is the sole final teardown authority.

Latest deterministic corrective gate:

```text
Compile: PASS
UnitTests: PASS (195/195)
M2LocalAcceptance: PASS (L01-L144)
ZeroRequestIdNormalization: PASS
ZeroRequestResolvedMatch: PASS
StructuredStdoutWarningScannerIsolation: PASS
RawStderrWarningAuthority: PASS
ParentFinalTeardownAuthority: PASS
ExpiringCachePendingDetection: PASS
ApprovalLiveCacheTeardownContract: PASS
ZeroIdExactlyOnceEvidence: PASS
CardV2RegressionAfterHostEvidenceFix: PASS
ApprovalBridgeRegressionAfterHostEvidenceFix: PASS
ShutdownLifecycleCoreChanges: NONE
ProductionShutdownHostBaseline: UNCHANGED_PASS
PersistentCredentials: UNCHANGED_PASS
ShutdownHostRetestRequired: NO
PersistentCredentialsHostRequired: NO
M2B: CFR_M2B_APPROVAL_LIVE_HOST_RETEST_REQUIRED
M2: CFR_M2_FEISHU_CODEX_REMOTE_PARTIAL
M3: UNCHANGED_FROZEN_v2
HostNext: ALLOW
```

Latest corrective artifacts:

```text
.tmp/m2-feishu-local/20260819T111503Z-17272508
.tmp/m2-feishu/20260819T111453Z-fe4a12dd
```

## Final targeted corrective: resolved ID, marker bytes, and cache terminal evidence

The new authoritative Host baseline is preserved unchanged:

```text
BaselineHostRunId: 20260819T105322Z-92393ad8
Artifact: .tmp/m2b-approval-live/20260819T105322Z-92393ad8
```

Its already-proven business evidence remains PASS through card delivery,
operator validation, durable `accept`, JSON-RPC completion, protected marker
creation, turn completion, and the durable resolved snapshot. The preserved
Host failure remains:

```text
Resolved notification observed: PASS
Resolved request ID match: FAIL
ExactlyOnce: FAIL
Protected marker created: PASS
Protected marker exact: FAIL
Parent final teardown: FAIL
ExpiringCache post-exit: FAIL
Allow Host: FAIL
Decline: NOT_RUN
```

This corrective now records all resolved notification IDs in the live scope,
including raw scalar value, scalar type, source field, normalized value, safe
top-level/params key lists, target count, and expected ID. Matching is target
domain based: unrelated resolved IDs do not invalidate a later matching target
ID, while missing/wrong IDs remain fail-closed.

Marker verification is now byte-level. It compares raw bytes directly and
records expected/actual length, SHA-256, UTF-8 BOM, LF, CRLF, trailing-space,
and escaped-value diagnostics. The preserved Host marker was confirmed as
`CFR_M2B_ALLOW_OK\n` (17 bytes), while the exact expected operation is
`CFR_M2B_ALLOW_OK` (16 bytes); the mismatch is therefore confirmed as a
trailing LF, not a verifier relaxation opportunity.

Approval Live cleanup now joins the probe watcher/worker before transport
release and observes the same captured `cache._cron` task through child return.
The probe records owner-loop identity, capture/public-stop/drain terminal
states, cancellation, terminal exception evidence, and parent post-exit
warnings. A zero remaining task count without terminal task evidence cannot
pass the cache contract.

Final deterministic corrective evidence:

```text
Compile: PASS
UnitTests: PASS (195/195)
M2LocalAcceptance: PASS (L01-L144)
DeterministicGate: PASS
ResolvedNotificationObservedIdDiagnostics: PASS
ExpectedServerRequestIdContract: PASS
ObservedResolvedRequestIdNormalization: PASS
ResolvedTargetZeroIdMatch: PASS
ResolvedTargetMultipleNotificationMatch: PASS
ResolvedWrongIdFailClosed: PASS
ResolvedNotificationEvidenceContract: PASS
ZeroIdExactlyOnceEvidenceContract: PASS
MarkerExactByteContract: PASS
MarkerMismatchDiagnostics: PASS
ProtectedOperationExactByteWrite: PASS
CacheCronOwnerLoopContract: PASS
CacheCronTerminalBeforeChildReturn: PASS
CacheWrongLoopAccountingRegression: PASS
CacheOwnerLoopClosedPendingFails: PASS
ParentPostExitCacheWarningAuthority: PASS
ApprovalLiveCacheTeardownContract: PASS
ChildPreExitFinalVerdictSuppressed: PASS
ParentFinalTeardownAuthority: PASS
StructuredStdoutWarningScannerIsolation: PASS
RawStderrWarningAuthority: PASS
ApprovalLiveTeardownContract: PASS
ApprovalCardV2Contract: PASS
ApprovalBridgeRegression: PASS
NoAutoApprove: PASS
DuplicateClickExactlyOnce: PASS
ShutdownLifecycleCoreChanges: NONE
UnexpectedScopeExpansion: NO
ProductionShutdownHostBaseline: UNCHANGED_PASS
ProductionShutdownHostRegressionRequired: NO
PersistentCredentials: UNCHANGED_PASS
M2B: CFR_M2B_APPROVAL_LIVE_HOST_RETEST_REQUIRED
M2: CFR_M2_FEISHU_CODEX_REMOTE_PARTIAL
M3: UNCHANGED_FROZEN_v2
BlockingIssues:
- M2_CODEX_INTEGRATION_HOST_REQUIRED
- M1_REGRESSION_HOST_REQUIRED
- M2B_APPROVAL_LIVE_HOST_RETEST_REQUIRED
- FEISHU_CHANNEL_NATIVE_PROBE_REQUIRED
HostNext: ALLOW
```

Root-cause classification:

```text
ConfirmedResolvedIdRootCause:
Preserved Host evidence did not record raw resolved-ID source; the prior
matcher failed to prove the target domain when resolved notifications were
not collected and diagnosed as a complete set. The corrective uses all scoped
serverRequest/resolved IDs and records raw/source/normalized evidence.

ConfirmedMarkerMismatchRootCause:
Trailing LF. Actual bytes were 43-46-52-5F-4D-32-42-5F-41-4C-4C-4F-57-5F-4F-4B-0A;
expected bytes contain no LF.

ConfirmedExpiringCacheRootCause:
Captured cache cron task was not proven terminal before child return; the
exact upstream owner-loop timing cause remains NOT_CONFIRMED from the preserved
artifact. Probe cleanup integration now observes that exact task and fails
closed on non-terminal state.

ApprovalLiveProbeCleanupIntegration: FIXED_DETERMINISTIC
RemainingEvidenceGap: Fresh Host Allow must prove target ID match, exact marker
bytes, cache cron terminal-before-child-return, and clean raw stderr after exit.
```

Production shutdown core, `sdk_compat.py`, `transport.py`, credentials,
Card V2, and ApprovalBridge architecture remain unchanged in this corrective.
The next and only Host action is Allow:

```powershell
python .\scripts\run_m2b_feishu_approval_live_probe.py --mode allow --timeout 180
```

Run Decline only after Allow returns a complete Host PASS with
`FinalVerdict=PASS` and no blockers.

## Resolution authority collision corrective

The previous deterministic gate did not cover a collision between REAL
`serverRequest/resolved` notification evidence and durable resolution snapshot
evidence. Both source helpers emitted `ResolvedRequestIdMatched`, so a later
snapshot update could overwrite a live false with true and mask a wrong REAL
resolved request ID. This was a deterministic integrated correctness bug found
before Host retest, not a new Host failure.

The corrective now keeps `LiveResolvedRequestIdMatched` and
`DurableResolvedRequestIdMatched` as independent primary authorities.
`ResolvedRequestIdMatched` is retained only as the explicit legacy aggregate
(`LIVE AND DURABLE`), with `ResolvedRequestIdMatchedAuthority` set to
`AGGREGATE_LIVE_AND_DURABLE`. Notification and snapshot contracts are
independent, and the exactly-once resolution component requires both
authorities plus a single durable target and empty inflight state.

Deterministic artifact:

```text
BaselineZip: CFR(20260819-111713).zip
Compile: PASS
UnitTests: PASS (203/203)
M2LocalAcceptance: PASS (L01-L150)
DeterministicGate: PASS
ResolutionAuthoritiesSeparated: PASS
LiveResolvedRequestIdMatchContract: PASS
DurableResolvedRequestIdMatchContract: PASS
WrongLiveIdCorrectSnapshotStillFails: PASS
CorrectLiveIdWrongSnapshotStillFails: PASS
CorrectLiveIdCorrectSnapshotPasses: PASS
ExactlyOnceRequiresBothResolutionAuthorities: PASS
ResolutionFieldCollisionRegression: PASS
ResolvedNotificationEvidenceContract: PASS
ResolutionSnapshotContract: PASS
ZeroRequestIdNormalization: PASS
ResolvedNotificationObservedIdDiagnostics: PASS
MarkerExactByteContract: PASS
ProtectedOperationExactByteWrite: PASS
ApprovalLiveCacheTeardownContract: PASS
ChildPreExitFinalVerdictSuppressed: PASS
ParentFinalTeardownAuthority: PASS
StructuredStdoutWarningScannerIsolation: PASS
RawStderrWarningAuthority: PASS
ApprovalCardV2Contract: PASS
ApprovalBridgeRegression: PASS
WrongOperatorFailClosed: PASS
DuplicateClickExactlyOnce: PASS
NoAutoApprove: PASS
ShutdownLifecycleCoreChanges: NONE
ProductionShutdownHostBaseline: UNCHANGED_PASS
PersistentCredentials: UNCHANGED_PASS
M2B: CFR_M2B_APPROVAL_LIVE_HOST_RETEST_REQUIRED
M2: CFR_M2_FEISHU_CODEX_REMOTE_PARTIAL
M3: UNCHANGED_FROZEN_v2
HostNext: ALLOW
```

Artifacts:

```text
.tmp/m2-feishu-local/20260819T113231Z-f442d6d2
.tmp/m2-feishu/20260819T113255Z-93547dc5
```

Changed files in this corrective are limited to the live probe, its unit
contract tests, local acceptance, deterministic gate, and this report. The
preserved Host baseline remains
`.tmp/m2b-approval-live/20260819T105322Z-92393ad8` and was not overwritten.
No real Host Allow or Decline was run. The next and only Host action is Allow:

```powershell
python .\scripts\run_m2b_feishu_approval_live_probe.py --mode allow --timeout 180
```

Run Decline only after Allow returns a complete Host PASS with
`LiveResolvedRequestIdMatched=true`, `DurableResolvedRequestIdMatched=true`,
`ResolutionSnapshotContract=PASS`, exact marker bytes, clean cache teardown,
and no blockers.

## ExpiringCache terminal cleanup corrective

Authoritative Host baseline:

```text
RunId: 20260819T114236Z-90309a5c
Approval business path: PASS
LiveResolvedRequestIdMatched: true
DurableResolvedRequestIdMatched: true
ExactlyOnce: true
AllowMarkerContentExact: true
BusinessVerdict: PASS
Child cache terminal: FAIL
Parent post-exit lifecycle: FAIL
FinalVerdict: FAIL
Verdict: FAIL_TEARDOWN
```

The only Host blocker was the captured `ExpiringCache._start_clear_cron`
task: its owner loop was open, non-running, and distinct from the WS, BG, and
transport loops. The prior production drain counted only tasks recognized by
the SDK-origin scanner. It could therefore observe zero SDK cache tasks and
close the owner loop while the captured exact cron task remained pending. This
was confirmed deterministically with the same shape: a foreign task stayed
pending after `drain_sdk_tasks()` closed its loop.

The production lifecycle helper now treats captured task references as primary
evidence in addition to SDK-origin discovery. It includes the exact task in
capture snapshots, cancels and gathers that reference on its owner loop, uses
the thread-safe path when the owner loop is running elsewhere, and only closes
an open non-running owner loop after the captured task is terminal. The probe
now records owner-loop state, owner-thread evidence, drain mechanism, exact
task terminal evidence, and owner-loop closure evidence.

Deterministic corrective evidence:

```text
Compile: PASS
UnitTests: PASS (209/209)
M2LocalAcceptance: PASS (L01-L160)
DeterministicGate: PASS
CacheCronOwnerLoopStateContract: PASS
CacheCronOwnerThreadContract: PASS
CacheOpenNonRunningOrphanLoopDrain: PASS
CacheSameTaskTrackedToTerminal: PASS
CacheCancelWithoutDrainFails: PASS
CacheClosedPendingLoopFails: PASS
CacheTerminalThenLoopClosePasses: PASS
CacheForeignRunningLoopThreadSafeDrain: PASS
Host114236CacheShapeRegression: PASS
CacheCronTerminalBeforeChildReturn: PASS
CacheCronAwaitedToTerminal: PASS
ApprovalLiveCacheTeardownContract: PASS
ParentPostExitCacheAuthorityRegression: PASS
ResolutionAuthoritiesSeparated: PASS
ExactlyOnceRequiresBothResolutionAuthorities: PASS
MarkerExactByteContract: PASS
ProtectedOperationExactByteWrite: PASS
ApprovalCardV2Contract: PASS
WrongOperatorFailClosed: PASS
DuplicateClickExactlyOnce: PASS
NoAutoApprove: PASS
```

Production shutdown core status:

```text
ShutdownLifecycleCoreChanges: src/cfr/feishu/sdk_compat.py: captured cache task drain
ProductionShutdownHostRegressionRequired: YES
ProductionShutdownHostBaseline: UNCHANGED_PASS
```

The required Shutdown probe was attempted with RunId
`20260819T115538Z-40a4bd93`, but the current environment stopped at
`FEISHU_CREDENTIALS_REQUIRED` with `ChannelReady=NOT_RUN`; it did not execute
the real SDK shutdown lifecycle. Its artifact is preserved at
`.tmp/m2-feishu-shutdown/20260819T115538Z-40a4bd93` and is not classified as a
shutdown PASS.

The deterministic gate artifact is
`.tmp/m2-feishu/20260819T115726Z-a44620cd`. Because production shutdown core
changed, the next and only Host action is Shutdown Host Regression:

```powershell
python .\scripts\run_m2_feishu_shutdown_probe.py --timeout 30
```

Do not run Approval Allow until that Host regression reaches a complete PASS.
Decline remains forbidden until Allow PASS. The deferred UX follow-up remains:

```text
DeferredM2BUserExperienceItem: M2B_APPROVAL_DECISION_FEEDBACK_REQUIRED
DeferredM2BUxStatus: DEFERRED_UNTIL_ALLOW_DECLINE_HOST_CLOSED
```

## DeviceFlow terminal cleanup final host-blocker corrective

Authoritative preserved Host baseline:

```text
BaselineHostRunId: 20260820T040651Z-db21501c
Approval business: PASS
Resolution authorities: PASS
ExactlyOnce: PASS
Marker exactness: PASS
ExpiringCache terminal cleanup: PASS
DeviceFlow pre-close: reported PASS
DeviceFlow public stop: TIMEOUT
Post-exit warnings: FAIL
FinalVerdict: FAIL
Verdict: FAIL_TEARDOWN
```

ConfirmedDeviceFlowRootCause:

The installed `lark_channel` SDK defines `DeviceFlowClient.close` as
`async def`. CFR pre-close created and awaited one coroutine on the SDK-owned
BG loop. `FeishuChannel.disconnect()` then called the SDK synchronous
`FeishuChannel.stop()` again. That method unconditionally created a second
`DeviceFlowClient.close()` coroutine on the BG loop. Its timeout branch only
logged a warning and abandoned the concurrent future, leaving the scheduled
Task pending until the BG loop stopped; its scheduling-race branch also did
not dispose the raw coroutine. The pre-close and public-stop references were
the same DeviceFlow object, but the SDK exposes no terminal/close future state
that public stop can reuse.

The CFR compatibility boundary now captures the pre-close lifecycle and, once
terminal, performs the SDK stop lifecycle while omitting only the already
terminal second DeviceFlow close. It still disposes the safety pipeline,
stops the WS client, cancels tracked BG work, stops and joins the SDK BG loop,
and restores the SDK lifecycle state. Raw coroutine scheduling failure is
explicitly disposed; scheduled timeout paths cancel and settle before return.
The installed site-packages were not modified.

Deterministic corrective evidence:

```text
RunId: 20260820T043113Z-3ad5e860
LocalAcceptanceArtifact: .tmp/m2-feishu-local/20260820T043125Z-82ab0717
DeterministicGateArtifact: .tmp/m2-feishu/20260820T043113Z-3ad5e860
Compile: PASS
UnitTests: PASS (212/212)
M2LocalAcceptance: PASS (L01-L172)
DeterministicGate: PASS
DeviceFlowExactCloseLifecycleCapture: PASS
DeviceFlowPreCloseTerminalContract: PASS
DeviceFlowDoubleCloseIdempotent: PASS
DeviceFlowPublicStopAfterPreCloseSafe: PASS
DeviceFlowRawCoroutineLeakPrevention: PASS
DeviceFlowTimeoutTaskTerminal: PASS
DeviceFlowForeignLoopThreadSafeClose: PASS
DeviceFlowOpenNonRunningLoopClose: PASS
Host040651DeviceFlowRegression: PASS
ExpiringCacheRegressionAfterDeviceFlowFix: PASS
ApprovalBusinessRegressionAfterDeviceFlowFix: PASS
DeviceFlowPostExitWarningAuthority: PASS
ResolutionAuthoritiesSeparated: PASS
MarkerExactByteContract: PASS
ApprovalCardV2Contract: PASS
ApprovalBridgeRegressionAfterFinalTargetedFix: PASS
```

Production shutdown core changed:

```text
ShutdownLifecycleCoreChanges: src/cfr/feishu/sdk_compat.py, src/cfr/feishu/transport.py
ProductionShutdownHostRegressionRequired: YES
M2B: CFR_M2B_APPROVAL_LIVE_HOST_RETEST_REQUIRED
M2: CFR_M2_FEISHU_CODEX_REMOTE_PARTIAL
M3: UNCHANGED_FROZEN_v2
HostNext: SHUTDOWN_HOST_REGRESSION
```

Changed files:

```text
src/cfr/feishu/sdk_compat.py
src/cfr/feishu/transport.py
scripts/run_m2b_feishu_approval_live_probe.py
scripts/run_m2_feishu_shutdown_probe.py
scripts/run_m2_feishu_local_acceptance.py
scripts/run_m2_feishu_gate.py
tests/unit/feishu/test_sdk_compat.py
docs/M2_FEISHU_GATEWAY_REPORT.md
```

ApprovalBridge, Card V2, resolution logic, marker logic, ExpiringCache logic,
credentials, and M3 were unchanged. The preserved Host artifact
`.tmp/m2b-approval-live/20260820T040651Z-db21501c` was not deleted or
overwritten. No real Host gate was run in this corrective. The next and only
Host command is:

```powershell
python .\scripts\run_m2_feishu_shutdown_probe.py --timeout 30
```

Do not run Approval Allow until that Shutdown Host Regression is a complete
PASS. Decline and the deferred approval-feedback UX remain blocked until the
Allow Host PASS.

## Current M2B Approval Decision Feedback UX Closure (2026-08-20)

This section is the current status for the UX Closure pass; earlier sections
are retained as historical corrective evidence. The implementation closes the
user-visible feedback gap without reopening the already-passed approval
authority or lifecycle work.

```text
Compile: PASS
UnitTests: PASS (221/221)
M2LocalAcceptance: PASS (L01-L190)
DeterministicGate: PASS
ApprovalFeedbackRendererContract: PASS
ApprovalFeedbackStateMachineContract: PASS
ApprovalFeedbackPendingCardContract: PASS
ApprovalFeedbackAckProcessingContract: PASS
ApprovalFeedbackApprovedContract: PASS
ApprovalFeedbackDeclinedContract: PASS
ApprovalFeedbackExecutionFailedContract: PASS
ApprovalFeedbackButtonsDisabledAfterDecision: PASS
ApprovalFeedbackMonotonicStateContract: PASS
ApprovalFeedbackDuplicateSameDecisionContract: PASS
ApprovalFeedbackOppositeDecisionContract: PASS
ApprovalFeedbackWrongOperatorContract: PASS
ApprovalFeedbackUpdateFailureContract: PASS
ApprovalFeedbackOriginalMessageUpdateContract: PASS
ApprovalFeedbackNoSecretFields: PASS
DeclineMarkerAbsenceContract: PASS
DeclineMarkerExactByteContractSemantics: PASS
CurrentBlockingIssuesCleanup: PASS
ApprovalDecisionFeedbackUxContract: PASS
ApprovalCardSchema: 2.0
LegacyActionTagPresent: NO
ApprovalCardV2Contract: PASS
ResolutionAuthoritiesSeparated: PASS
ExactlyOnceRequiresBothResolutionAuthorities: PASS
MarkerExactByteContract: PASS
ApprovalLiveCacheTeardownContract: PASS
ApprovalLiveDeviceFlowTeardownContract: PASS
ShutdownLifecycleCoreChanges: NONE
TransportMessagingApiChanges: YES: original interactive card update via current Channel SDK
PersistentCredentials: UNCHANGED_PASS
ProductionShutdownHostBaseline: PASS
RunId=20260820T045904Z-26dffdbf
ApprovalAllowHostBaseline: PASS
RunId=20260820T050244Z-26c5cfe5
ApprovalDeclineHostBaseline: PASS
RunId=20260820T050440Z-5e87b8c9
M2: CFR_M2_FEISHU_CODEX_REMOTE_PARTIAL
M3: UNCHANGED_FROZEN_v2
BlockingIssues:
- M2B_APPROVAL_DECISION_FEEDBACK_REQUIRED
- M1_FINAL_REGRESSION_AFTER_M2B_REQUIRED
HostNext: ALLOW_UX
```

Latest deterministic artifacts:

```text
.tmp/m2-feishu-local/20260820T053458Z-923b48d5
.tmp/m2-feishu/20260820T053448Z-235fe267
```

Changed files in this UX pass:

```text
Card rendering changes:
  src/cfr/feishu/approval_card.py
Feedback orchestration and durable message binding:
  src/cfr/feishu/approvals.py
  src/cfr/feishu/store.py
Feishu message update API changes:
  src/cfr/feishu/replies.py
  src/cfr/feishu/transport.py
Fake transport, local acceptance, and deterministic gate:
  scripts/run_m2_feishu_local_acceptance.py
  scripts/run_m2_feishu_gate.py
UX unit contracts:
  tests/unit/feishu/test_approval_feedback_ux.py
Report metadata:
  docs/M2_FEISHU_GATEWAY_REPORT.md
```

Approval authority, ExactlyOnce, Resolution authorities, production lifecycle,
credentials, and M3 remain unchanged. `DeclineMarkerAbsent=true` is the
correct Decline semantics; exact-byte and mismatch diagnostics are `NOT_RUN`
when the marker is absent, not business failures.

The real UX Host gates were not run in this pass. The next command is Allow UX:

```powershell
python .\scripts\run_m2b_feishu_approval_live_probe.py --mode allow --timeout 180
```

Stop if the original-card update or UX verdict fails. Run Decline only after
Allow UX Host PASS; run the M1 Final Regression only after both UX Host modes
PASS.

## M2B Approval Feedback Finalization Binding Micro-Corrective (2026-08-20)

This narrowly scoped corrective closes the deterministic integration gap where
real terminal turn events could omit `requestId` and leave the original card
in `ACKNOWLEDGED_PROCESSING`. Final feedback now binds by the authoritative
`thread_id + turn_id` pair and uses one Bridge finalizer for TurnResult,
production daemon, live probe, and notification fallback paths.

```text
Compile: PASS
UnitTests: PASS (228/228)
M2LocalAcceptance: PASS (L01-L202)
DeterministicGate: PASS
ObservedTurnResultShape: TurnResult(thread_id, turn_id, status, final_agent_message, started_at, completed_at, error_message)
TurnCompletedNestedTurnIdContract: PASS
FinalFeedbackTurnBindingContract: PASS
FinalFeedbackNoRequestIdDependency: PASS
FinalFeedbackAcceptCompletedContract: PASS
FinalFeedbackDeclineContract: PASS
FinalFeedbackExecutionFailedContract: PASS
FinalFeedbackIdempotentTerminalContract: PASS
ProductionDaemonFinalFeedbackContract: PASS
ProductionDaemonInitialTurnFinalFeedbackContract: PASS
LiveProbeFinalFeedbackBindingContract: PASS
LiveProbeFinalFeedbackWaitContract: PASS
RealHostTurnCompletedShapeRegression: PASS
ApprovalFeedbackRendererContract: PASS
ApprovalFeedbackStateMachineContract: PASS
ApprovalFeedbackButtonsDisabledAfterDecision: PASS
ApprovalFeedbackMonotonicStateContract: PASS
ApprovalFeedbackDuplicateSameDecisionContract: PASS
ApprovalFeedbackOppositeDecisionContract: PASS
ApprovalFeedbackWrongOperatorContract: PASS
ApprovalFeedbackUpdateFailureContract: PASS
DeclineMarkerAbsenceContract: PASS
DeclineMarkerExactByteContractSemantics: PASS
ApprovalCardV2Contract: PASS
ResolutionAuthoritiesSeparated: PASS
ExactlyOnceRequiresBothResolutionAuthorities: PASS
MarkerExactByteContract: PASS
ApprovalLiveCacheTeardownContract: PASS
ApprovalLiveDeviceFlowTeardownContract: PASS
ShutdownLifecycleCoreChanges: NONE
TransportLifecycleChanges: NONE
PersistentCredentials: UNCHANGED_PASS
M2: CFR_M2_FEISHU_CODEX_REMOTE_PARTIAL
M3: UNCHANGED_FROZEN_v2
BlockingIssues:
- M2B_APPROVAL_DECISION_FEEDBACK_REQUIRED
- M1_FINAL_REGRESSION_AFTER_M2B_REQUIRED
HostNext: ALLOW_UX
Real Host UX: NOT_RUN
```

Deterministic artifacts:

```text
.tmp/m2-feishu-local/20260820T060009Z-720057c5
.tmp/m2-feishu/20260820T060043Z-12aa7cc4
```

Changed files in this corrective:

```text
src/cfr/feishu/store.py
src/cfr/feishu/approvals.py
src/cfr/feishu/daemon.py
scripts/run_m2b_feishu_approval_live_probe.py
scripts/run_m2_feishu_local_acceptance.py
scripts/run_m2_feishu_gate.py
tests/unit/feishu/test_approval_feedback_finalization.py
docs/M2_FEISHU_GATEWAY_REPORT.md
```

Approval authority, ExactlyOnce, Resolution, Card V2 renderer, card update
transport, ExpiringCache, DeviceFlow, `sdk_compat.py`, shutdown lifecycle,
credentials, and M3 remain unchanged. The real Feishu Allow/Decline UX Host
gates and M1 final regression were not run. The next manual Host command is:

```powershell
python .\scripts\run_m2b_feishu_approval_live_probe.py --mode allow --timeout 180
```

## Current M2B Production Shutdown Host Routing

This is the current deterministic gate state. Earlier sections are historical
corrective evidence and are not allowed to override the current routing.

```text
Compile: PASS
UnitTests: PASS (247/247)
M2LocalAcceptance: PASS (L01-L230)
DeterministicGate: PASS
NewDeterministicGateRunId: 20260820T084243Z-d708ec42
DeterministicGateArtifact: .tmp/m2-feishu/20260820T084243Z-d708ec42/result.json

GateRoutingConsistencyContract: PASS
ShutdownLifecycleCoreChanges:
- src/cfr/feishu/sdk_compat.py
- src/cfr/feishu/transport.py
ShutdownLifecycleCoreChanged: true
ProductionShutdownHostRegressionRequired: YES
ProductionShutdownHostRegressionSatisfied: NO
HistoricalProductionShutdownHostRunId: 20260820T045904Z-26dffdbf
HistoricalProductionShutdownHostVerdict: PASS
HistoricalShutdownPassDoesNotSatisfyCurrentCoreChange: PASS
ShutdownHostPriorityOverAllowUx: PASS
ImplementationReadyCannotOverrideShutdownRegression: PASS

ApprovalAllowUxHistoricalRunId: 20260820T061102Z-b65b71cd
ApprovalAllowUxHistoricalVerdict: PASS
ApprovalDeclineUxHistoricalRunId: 20260820T061707Z-10015da2
ApprovalDeclineUxFunctionalHistoricalVerdict: PASS
ApprovalDeclineAggregateHistoricalVerdict: FAIL_TEARDOWN

LastAgentShutdownProbeRunId: 20260820T064852Z-92cf46a9
LastAgentShutdownProbeDisposition: NOT_RUN_CREDENTIALS_UNAVAILABLE

BgLoopSleepOrphanCorrective: PASS_DETERMINISTIC
BgPreStopSdkCancelHelperBypassed: PASS
BgPreStopRunningLoopTerminalDrain: PASS
BgPreStopNoSleepCoroutineBarrier: PASS
BgPreStopLoopStopRequiresTerminal: PASS
Host061707SleepOrphanShapeRegression: PASS
DeclineLiveMarkerAbsenceMetadata: PASS
DeclineMarkerAbsenceContract: PASS
DeclineMarkerExactByteContractSemantics: PASS
ApprovalFeedbackRendererContract: PASS
ApprovalFeedbackStateMachineContract: PASS
FinalFeedbackTurnBindingContract: PASS
ApprovalCardV2Contract: PASS
ResolutionAuthoritiesSeparated: PASS
ExactlyOnceRequiresBothResolutionAuthorities: PASS
ApprovalLiveCacheTeardownContract: PASS
ApprovalLiveDeviceFlowTeardownContract: PASS
TransportLifecycleChanges: START_WORKER_SERIALIZATION
ApprovalAuthorityChanges: NONE
PersistentCredentials: UNCHANGED_PASS
M2: CFR_M2_FEISHU_CODEX_REMOTE_PARTIAL
M3: UNCHANGED_FROZEN_v2

StartWorkerSerializationCorrective: PASS_DETERMINISTIC
InstalledSdkVersion: lark-channel-sdk==1.2.0
ObservedStartFutureShape: asyncio.Future wrapper from loop.run_in_executor(None, self.start)
ConfirmedStartWorkerLateCleanupRootCause: CONFIRMED
ConfirmedRootCause: late _cleanup_failed_start() retained SDK BG cleanup authority after the prior pre-stop snapshot; its _drain_cancelled_bg_tasks() submitted asyncio.sleep(0) through run_coroutine_threadsafe, producing the _chain_future orphan signature.
StartFutureCancelNotWorkerTerminal: PASS
StartWorkerTerminalAuthority: START_FUTURE_NATURAL_COMPLETION
BgOwnershipHandoffCompleted: PASS
BgSchedulingBlockedBeforeDetach: PASS
BgOwnershipDetachedFromSdk: PASS
BgProducerQuiescenceContract: PASS
LateStartWorkerCleanupCannotCreateSleep: PASS
StartWorkerTerminalBeforeBgDrain: PASS
BgFinalDrainTerminalEvidence: PASS
BgCapturedLoopStopAllowed: PASS
Host072914LateSleepRaceRegression: PASS
StartFutureOwnerLoopClosedBeforeWorkerExit: false

BlockingIssues:
- M2B_SHUTDOWN_HOST_REGRESSION_REQUIRED
- M2B_APPROVAL_DECISION_FEEDBACK_REQUIRED
- M1_FINAL_REGRESSION_AFTER_M2B_REQUIRED

HostNext: SHUTDOWN_HOST_REGRESSION
```

The current production shutdown implementation now serializes the installed
SDK start-worker with BG-loop ownership. CFR blocks scheduling, captures and
detaches `_bg_loop`, `_bg_thread`, tracked futures, retry state, and the
`_start_future`, then releases WS and awaits natural worker completion while
the owner loop remains alive. Only after producer quiescence does CFR run the
final callback drain and directly stop/join the captured BG loop. The
historical shutdown PASS is retained as a baseline, but it does not satisfy
the current-code regression obligation.

The agent shutdown attempt `20260820T064852Z-92cf46a9` stopped at
`FEISHU_CREDENTIALS_REQUIRED` with `ChannelReady=NOT_RUN`; it is recorded as
`NOT_RUN_CREDENTIALS_UNAVAILABLE`, not as a lifecycle PASS or FAIL.

The only next Host action is:

```powershell
python .\scripts\run_m2_feishu_shutdown_probe.py --timeout 30
```

After a current-code Shutdown Host PASS, the sequence is Allow UX, Decline UX,
and M1 final regression. `HostNext` must remain
`SHUTDOWN_HOST_REGRESSION` until that current-code PASS is ingested.

## M2 Usable Baseline Freeze (2026-08-23)

This is the current product-status record. Historical artifacts and their raw
aggregate verdicts remain unchanged.

```text
M2FunctionalBaseline: PASS
ApprovalAllowFunctionalSmoke: PASS
ApprovalAllowFunctionalSmokeRunId: 20260823T155324Z-167bbfb3
ApprovalDeclineFunctionalSmoke: PASS
ApprovalDeclineFunctionalSmokeRunId: 20260823T155445Z-909fdc2a
M1FinalRegression: PASS
M1FinalRegressionRunId: 20260823T155552Z-6b6e22f5

M2ImplementationStatus: FUNCTIONALLY_COMPLETE_FOR_MVP
M2UsableBaseline: FROZEN
M2StrictGateStatus: PARTIAL_WITH_KNOWN_NONBLOCKING_TEARDOWN_ISSUE
M3Architecture: FROZEN_v2
M3Implementation: START_M3A
```

The functional Allow evidence records `BusinessVerdict=PASS`,
`ApprovalFeedbackUxVerdict=PASS`, `ApprovalFeedbackStateFinal=APPROVED`,
`ExactlyOnce=true`, and `StoredDecision=accept`. The functional Decline
evidence records the same business and UX success, with
`ApprovalFeedbackStateFinal=DECLINED`, `StoredDecision=decline`,
`DeclineItemDeclined=true`, `DeclineMarkerAbsent=true`, and
`DeclineMarkerAbsenceContract=PASS`.

Historical aggregate facts are preserved without rewrite:

```text
Approval Allow historical aggregate: FAIL_TEARDOWN
Approval Decline historical aggregate: FAIL_TEARDOWN
```

Both historical aggregate failures were caused only by the process-exit
`ExpiringCache._start_clear_cron` pending/never-awaited warning. No evidence
was observed of approval-decision corruption, duplicate execution, ExactlyOnce
failure, incorrect marker behavior, user-visible approval failure, M1
regression, or process hang.

### Deferred backlog

```text
M2B_EXPIRINGCACHE_PROCESS_EXIT_WARNING
Severity: NON_BLOCKING_FOR_MVP
Observed effect: process-exit teardown warning only
Not observed: approval decision corruption; duplicate execution; ExactlyOnce failure;
              incorrect marker behavior; user-visible approval failure; M1 regression;
              process hang
Disposition: DEFERRED_POST_MVP
```

This backlog entry is not an M3A blocker. M2 runtime, shutdown, Approval,
Cache, DeviceFlow, resolution, ExactlyOnce, card, marker, and SDK compatibility
code are frozen for this baseline. `STRICT_ALL_GATES_PASS` is not claimed.
