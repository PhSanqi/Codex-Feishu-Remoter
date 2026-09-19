# CFR M1 Freeze Baseline

This file freezes the externally observable CFR M1 Codex Core behavior. It is a regression reference, not a ban on internal refactoring. Any M1.1 change that affects the contracts below must rerun the full Host M1 regression.

## Baseline evidence

| Field | Value |
|---|---|
| Milestone | CFR M1 Codex Core |
| Status | COMPLETE / FROZEN REGRESSION BASELINE |
| Final Host RunId | `20260818T025807Z-728bd42e` |
| GateOrigin | `host_manual` |
| RuntimeExecutionContext | `HOST` |
| Codex Version | `codex-cli 0.147.0` |
| Reference Platform | Windows 10 / current host |
| Reference Protocol | v1.2 |
| Protocol SHA256 | `9f7d498cb510be38baa10422b46860b2a4599898affe2ae56cdc73460f90908b` |
| M1 Unit Baseline | 34/34 PASS |
| Current M1.1A Unit Suite | 74/74 PASS |
| SourceCommit | UNKNOWN (workspace is not a Git checkout) |
| SourceTreeClean | UNKNOWN |

## M1 acceptance

| Gate | Result |
|---|---|
| LocalAcceptance | PASS |
| LocalBindingAcceptance | PASS |
| NetworkPreflight | PASS_DIRECT |
| WriterHandoffBeforePrimaryClose | EXPECTED_ACTIVE_WRITER |
| WriterHandoffAfterPrimaryExit | PASS |
| IntegrationA_Create | PASS |
| IntegrationA_RuntimeRelease | PASS |
| IntegrationB_Resume | PASS |
| IntegrationC_RolloutOffset | PASS |
| IntegrationD_Dedupe | PASS |
| IntegrationE_ActiveWriter | PASS |
| IntegrationF_Stop | PASS |
| BindingRecovery | PASS |
| BlockingIssues | NONE |
| M1Verdict | CFR_M1_CODEX_CORE_COMPLETE |

## Frozen behavioral contracts

- New conversations complete the first turn before naming and binding persistence, retain a real rollout, then gracefully close the CFR-owned app-server.
- Existing conversations resume the same native thread through a fresh app-server before starting a turn.
- Native active-writer truth is reported as `EXTERNAL_WRITER_ACTIVE`; CFR does not force, fork, copy sessions, or edit rollout JSONL.
- CFR-created app-server processes are CFR-owned and must reach confirmed process exit on operation completion.
- Rollout consumption is read-only, binary-offset based, partial-line safe, and persists the cursor after consumption.
- Binding recovery recreates durable state, watcher, and fresh native-thread resume without historical replay.
- Same-process active turns use `turn/interrupt`; standalone CLI stop does not claim cross-process control.
- CFR owns its resolved `CODEX_HOME`, CFR SQLite state, and native thread identifiers internally.
- `CFR_CODEX_HOME` resolution is explicit home > `CFR_CODEX_HOME` > user home `.codex`; ambient `CODEX_HOME` does not decide CFR ownership.

## Boundaries not implemented

`cfr-broker/1`, `workspace-arbitrator/1`, Cross-System Workspace Lease, Repository Lease, RouteBinding, Model Route Policy, Broker northbound transport, Broker authority/event cursor, Feishu Gateway, ChatAdapter, and WorkAdapter remain unimplemented.

The pinned v1.2 reference is immutable. If its SHA changes, stop with `REFERENCE_PROTOCOL_MUTATED` and do not auto-rewrite it.
