# CFR Coexistence Boundary Status

This status records the CFR side of the pinned v1.2 coexistence reference. The reference is documentation only; Broker and Feishu integration are not implemented by this repository.

| Layer | Status |
|---|---|
| Layer A - M1 Codex Core | COMPLETE / FROZEN REGRESSION BASELINE |
| Layer B - CFR Platform / Runtime Boundary | COMPLETE / M1.1A FROZEN |
| Layer C - CFR to Broker coexistence | NOT IMPLEMENTED |
| Layer D - Feishu product integration | M2 FUNCTIONALLY COMPLETE FOR MVP / USABLE BASELINE FROZEN |

| Protocol Area | CFR Status | This Round |
|---|---|---|
| CFR native thread ownership | ACTIVE | verified/documented |
| CFR to Desktop shared CFR `CODEX_HOME` | ACTIVE | explicit home resolver |
| Explicit CFR `CODEX_HOME` child injection | ACTIVE / PASS | host manual artifact confirms explicit child injection and runtime initialization |
| CFR-created app-server process ownership | ACTIVE / VERIFIED | graceful close and confirmed process exit |
| CFR-local in-process writer state | ACTIVE / VERIFIED | `WriterLeaseManager`; process-local only |
| CFR cross-process thread runtime lease | IMPLEMENTED / HOST VERIFIED | durable per-thread lease probe and M1.1A Host regression pass |
| Host/Desktop validation boundary | ACTIVE | `GateOrigin` and `RuntimeExecutionContext` are report-only metadata |
| Platform capabilities / launcher normalization | IMPLEMENTED / UNIT PASS | portable capability model and `CFR_CODEX_BIN` resolution |
| Doctor read-only diagnostics | IMPLEMENTED / WARN IN DESKTOP SANDBOX | Host `--live` validation pending |
| Feishu direct Codex gateway | M2 FUNCTIONALLY COMPLETE FOR MVP | Usable baseline frozen; strict gate remains partial only for a deferred nonblocking teardown warning |
| CFR DB isolation concept | ACTIVE | documented |
| CFR default DB path `%LOCALAPPDATA%\cfr` | DEFERRED | not implemented |
| Broker `CODEX_HOME` | OUT OF CFR SCOPE | reference only |
| Workspace Arbitrator | NOT IMPLEMENTED | deferred |
| `workspace-arbitrator/1` | NOT IMPLEMENTED | deferred |
| RouteBinding v1.2 | NOT IMPLEMENTED | deferred |
| Model Route Policy | NOT IMPLEMENTED | deferred |
| `cfr-broker/1` | NOT IMPLEMENTED | deferred |
| Human-required northbound | NOT IMPLEMENTED | deferred |
| Broker event cursor | NOT IMPLEMENTED | deferred |
| Broker authority epoch | OUT OF CURRENT CFR M1 | deferred |
| Cross-System Workspace Lease | NOT IMPLEMENTED | deferred |
| Repository Lease | NOT IMPLEMENTED | deferred |

## Ownership boundaries

- CFR owns its native Codex thread, CFR SQLite state, ingress, and local writer lifecycle.
- Broker, Router, and Workspace Arbitrator are not vendored or called by this M1 repository.
- CFR's `WriterLeaseManager` is CFR-local runtime state; it is not `workspace-arbitrator/1`.
- `CfrThreadRuntimeLeaseManager` is CFR-internal, durable, per-native-thread coordination; it is not a Workspace Lease or Broker fencing token.
- Native provider thread IDs remain inside the owning subsystem.
- No Broker database, session store, auth store, or credential is read or written by CFR.

## Future alignment policy

1. v1.2 is currently pinned as reference.
2. CFR does not vendor Broker implementation.
3. When Router/Broker integration begins, re-review the protocol version.
4. If a newer protocol supersedes v1.2, keep the old reference for history, add a new pinned reference, update the implementation status mapping, and do not silently edit the old reference.
5. Runtime conformance is proven by tests, not by document naming.

## M2 usable-baseline status (2026-08-23)

`M2FunctionalBaseline=PASS`, `M2UsableBaseline=FROZEN`, and
`M2StrictGateStatus=PARTIAL_WITH_KNOWN_NONBLOCKING_TEARDOWN_ISSUE`.
The deferred issue is `M2B_EXPIRINGCACHE_PROCESS_EXIT_WARNING`, a process-exit
warning with no observed decision, ExactlyOnce, marker, user-visible approval,
M1, or hang impact. It does not block `M3Implementation=START_M3A` under the
existing `M3Architecture=FROZEN_v2` boundary.

## M3B Control Center closure (2026-08-25)

`M3BStatus=CLOSED`. The loopback Control Center is a Control Plane only: it
projects existing runtime, Feishu session, job, approval, and Codex settings
authorities and sends only the fixed lifecycle, session-unbind, and new-thread
model-default commands defined by its authenticated API. It does not replace
Chat, Feishu, native Codex threads, ApprovalBridge, or the CFR execution plane.

M3B does not create a second session, job, or approval authority. Sessions
remain in `FeishuStore` (with native Codex bindings preserved on unbind), jobs
remain the current CFR registry/turn-result view, and approvals remain durable
Feishu/ApprovalBridge records with Feishu-only decisions.

M3C is closed for the primary web Control Plane; daily use now proceeds to
`CFR_DAILY_USE_ACCEPTANCE`.
