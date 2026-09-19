# M1 Codex Core Report

M1 is complete and frozen by Host evidence. The final Host regression is `20260818T025807Z-728bd42e` with `GateOrigin=host_manual` and `RuntimeExecutionContext=HOST`.

## Validation

- Reference copy: PASS; SHA256 `9f7d498cb510be38baa10422b46860b2a4599898affe2ae56cdc73460f90908b`
- Compile: PASS
- M1 freeze unit baseline: PASS (`34/34`)
- Current M1.1A unit suite: PASS (`50/50`)
- Explicit CFR `CODEX_HOME`: ACTIVE / VERIFIED
- NetworkPreflight: `PASS_DIRECT`
- WriterHandoff: PASS
- Integration A–F: PASS
- BindingRecovery: PASS; final observed/persisted/recovered offset `72800`
- BlockingIssues: NONE
- M1Verdict: `CFR_M1_CODEX_CORE_COMPLETE`

## M1.1A boundary hardening status

The M1 behavior contract remains frozen. This round adds CFR-internal durable per-thread runtime leasing, launcher/platform normalization, explicit diagnostics home consistency, read-only doctor diagnostics, portability tests, and deterministic cross-process lease probes. It does not implement Broker, Router, Workspace Arbitrator, Feishu, Chat, or Work integration.

## Current M1.1A status

Local compile, unit tests, and runtime lease probe pass. Host `doctor --live` and the post-hardening full M1 regression remain the final Host-only gates.
