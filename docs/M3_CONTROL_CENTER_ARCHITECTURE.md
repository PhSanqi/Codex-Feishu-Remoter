# CFR Local Control Plane Architecture (FROZEN_v2)

Status: `M3Architecture: FROZEN_v2`
Primary experience: `WEB_CONTROL_PLANE`
Primary launcher: `LOCAL_BACKEND_LAUNCHER`
Desktop EXE required: `NO`
Optional tray shell: `DEFERRED`
Implementation: `M3AStatus: CLOSED`, `M3BStatus: CLOSED`, `M3CStatus: CLOSED`
`M3Status: COMPLETE_FOR_PRIMARY_WEB_CONTROL_PLANE`
`DailyUseLauncherStatus: READY`
`M3DPackagingStatus: OPTIONAL_NOT_STARTED`

## Product boundary

M3 is a local backend launcher plus a loopback web control plane. The browser is the primary management experience. Tauri, tray, EXE, installer, updater, Windows service, and system-wide daemon are optional future shells and are not committed architecture requirements.

The architecture remains frozen. M3A and M3B implement the loopback Control
Plane without adding an alternate Feishu/Codex execution path, desktop shell,
packaging, or remote exposure.

## Frozen topology

```text
Browser
  | http://127.0.0.1:<port>, HTTP + WebSocket
  v
CFR Web Control UI (React/TS/Vite)
  |
  v
CFR Control API (loopback only, Python/FastAPI recommendation)
  |
  +-- CFR Supervisor / Runtime Controller
  +-- Config Facade
  +-- ControlReadModel
  |
  +-- Feishu Gateway / ApprovalBridge
  +-- CFR Codex Core
```

The next product layer sits above this frozen Code path rather than replacing
it:

```text
CFR
 |
 +-- Chat  -> real ChatGPT Chat authority (not connected yet)
 +-- Work  -> real ChatGPT Work authority (not connected yet)
 `-- Code  -> existing native Codex path (current)
```

Control Center exposes this as an `Execution Surface` catalog. This selector is
deliberately separate from Codex collaboration `/mode`; adding Chat or Work must
not reinterpret Codex Default/Plan or route those names through Codex as an
emulation layer.

The current local entry point is `python .\scripts\run_cfr_control.py`; the
daily-use wrapper is `START_CFR.cmd`. It preflights built UI assets, starts the
loopback Control Plane, distinguishes an existing CFR instance from a foreign
port owner, and can open a fresh bootstrap URL. Existing-instance reauthentication
and persistent service behavior remain deferred.

## Runtime semantics

CFR Codex Core continues to use a fresh app-server per operation or continuation. The UI therefore exposes `Codex Available`, `Codex Authenticated`, and `Accept Codex Tasks`; it must not present a misleading persistent `Codex Server ON/OFF` switch.

Runtime controls are:

- Remote Execution: ON/OFF.
- Accept New Tasks: ON/OFF.
- Feishu Gateway: START, STOP, RECONNECT.
- Drain: stop accepting new work and allow active work to finish.
- Run Doctor. Graceful Stop remains deferred until workload observability has
  an explicit contract.

The Details view may hand an existing idle binding to Codex Desktop through
the registered `codex://threads/<FULL_THREAD_ID>` OS protocol. This is local
navigation only: the browser supplies only the bound thread ID, while the
backend rechecks binding existence, active-turn state, and writer ownership.
It does not start an app-server, resume a thread, create a turn, or change CFR
state.

## ControlReadModel domains

The read model is a sanitized projection over existing CFR services and stores.
M3B implements Dashboard/Runtime, Feishu status, Sessions, current-process
Jobs, durable Approvals, installed Codex Models/Capabilities, Doctor, and
new-thread Codex model defaults. Security, MCP/Tools/Skills, diagnostics,
logs, usage, and generalized configuration management are not implemented
Control Center views. The UI never opens SQLite or imports BindingStore,
FeishuStore, CodexAdapter, or ApprovalBridge internals.

The Jobs projection uses the existing registry's public process-local snapshot.
Each active or recent Turn may include a bounded `runtime` observation with the
effective model, reasoning effort, service tier, terminal-safe status/stage,
phase durations, token/context totals, one current/recent safe tool summary, and
at most 20 safe activity events. It is not a durable history or a second runtime
authority. The existing 1200 ms Sessions/Jobs poll is the only refresh path.

Runtime durations use monotonic timestamps; wall timestamps exist only for UI
display. `TTFN` is Turn start to first meaningful native activity, `TTFT` is
Turn start to first answer delta, `CFR pre-turn` is task execution start to
native Turn start, and `CFR post-turn` is native Turn completion through final
reply completion. Retryable Codex stream errors and the native WebSocket-to-HTTPS
fallback warning are projected only as sanitized transport state/count/timing;
raw network error details are never retained. Missing phases remain JSON `null`
and render as an em dash. The dominant owner is computed by the backend from
the available non-overlapping phase candidates; the frontend only formats the
projection.

CFR-owned Codex child processes use the host's detected fixed proxy through
standard child-process proxy environment variables. This is deliberately
independent of any one Codex experimental feature flag and does not pin a proxy
port. Installed Codex compatibility is checked from the runtime-generated
app-server schema and required native methods rather than an exact CLI version.

The operational budgets are cold CFR-controlled overhead <= 10 s, warm CFR
pre-turn <= 3 s, warm CFR post-turn <= 2 s, and preferred combined warm CFR
overhead <= 5 s. All process-local telemetry is hard bounded. Installed Codex
model latency and tool execution are reported separately from CFR overhead and
do not justify weakening writer or runtime ownership semantics.

Telemetry stores no prompt, answer, raw reasoning, command body, tool arguments,
tool results, patch contents, approval payload, credential, chat identity, or
correlation/message ID. Tool observations are allowlisted to a category, safe
name, status, and elapsed time. Control Center remains read-only for telemetry.

## Dynamic capabilities and configuration

Model identity and model-specific options are runtime-owned. `cfr.control.model_registry`
normalizes Codex `model/list` into CFR's stable projection; model slugs, reasoning
efforts, service tiers, input modalities, multi-agent generation, availability,
and upgrade targets are never hardcoded in the UI or Feishu command layer.
Unrecognized model metadata is retained under `extensions` for diagnostics, but
does not become executable CFR behavior until the installed Codex protocol exposes
a corresponding operation. This lets a newly rolled-out model appear without a CFR
release while preserving fail-closed behavior for unsupported protocol features.

Other installed-runtime capabilities continue to come directly from permission
profiles, experimental-feature catalogs, and the generated app-server schema. CFR
must prefer these native catalogs over a parallel product/version matrix.

Configuration layers are:

```text
System Boundary -> Global Defaults -> Workspace Profile -> Session Override -> Turn Override
```

Each setting exposes effective value, effective source, scope, writable state, managed/locked state, and restart-required state. Codex sources may include Packaged, System, Enterprise, User, Profile, Project, Session, or Turn, depending on installed runtime support.

## Frozen implementation sequence

- M3A: Local Control API, Supervisor, and Web Dashboard. CLOSED.
- M3B: sessions, jobs, models/capabilities, defaults, and approvals management
  read models. CLOSED.
- M3C: CLOSED - local launcher and daily-use workflow implemented.
- M3D: OPTIONAL - packaging, EXE, installer, and updater.

Next: `CFR_DAILY_USE_ACCEPTANCE`.

## Daily-use corrective (V3)

First-time Feishu pairing persists only the non-secret operator/workspace policy,
then waits for the pairing connection to release before automatically starting
the normal single-owner runtime. A timeout keeps the saved policy and never
opens a second transport. The Control Center exposes only a bounded,
process-local sanitized activity timeline; it never returns pairing codes,
open IDs, raw messages, SDK logs, or App Secrets.

Allowed workspace roots are a persistent policy edited only while Feishu is
stopped. Environment policy remains an explicit read-only override. Slash
messages are authorized and scope-checked first, then handled as controls
before durable inbox persistence; unknown slash commands fail closed and can
never become Codex tasks. `M3D_1_WINDOWS_TRAY_EXE_PLANNED` and
`NATIVE_WORKSPACE_FOLDER_PICKER_DEFERRED_TO_M3D` remain deferred.

## Control Center operability corrective

The local UI has three in-component views: Runtime (the default daily-use
view), Configuration, and Details. Runtime keeps lifecycle controls, compact
current-work summaries, and a bounded process-local sanitized runtime console;
it is not raw stdout or SDK logging. Configuration owns the editable workspace
policy and new-thread Codex defaults, while Details contains Sessions, Jobs,
Approvals, Models, Capabilities, and the existing Doctor result.

The Feishu read model retains `workspace_roots` and additionally exposes
`workspace_roots_valid` plus `invalid_workspace_roots`. They use the same
existing-directory semantics as Feishu execution validation. Invalid roots are
never repaired automatically; the UI blocks Start until the user removes or
replaces them through the existing authenticated mutation path.
