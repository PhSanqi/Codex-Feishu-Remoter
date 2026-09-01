# Architecture

CFR is local-first. It does not replace Codex with a parallel execution engine.

```text
Feishu
  |
  v
CFR Feishu gateway
  |
  +--> operator/workspace authorization
  +--> per-chat serialization and durable local binding
  |
  v
Native Codex app-server
  |
  v
Codex thread / turn / tools

Control Center <--- process-local CFR read models and native Codex events
```

## Execution authority

Native Codex remains authoritative for model execution, threads, turns, model
catalogs, reasoning effort, service tier, tool/connector execution, and native
runtime notifications.

CFR owns integration concerns around that authority:

- Feishu ingress/egress;
- operator and workspace policy;
- local Feishu-to-thread bindings;
- single-writer/runtime-lease coordination;
- progress projection;
- safe runtime telemetry;
- Control Center presentation.

## Process boundary

CFR and Codex Desktop share durable Codex threads, not one permanent app-server
process. CFR opens native app-server clients when required and releases them
after ownership is no longer needed. Desktop handoff opens the existing thread
only when the CFR turn/writer state is safe.

## Runtime observability

The Control Center projects native lifecycle events and CFR-owned timing into a
bounded read model. It can show queue/runtime preparation, TTFN, TTFT, token and
context usage, safe tool lifecycle, transport retry/fallback state, and a short
activity timeline.

Raw chain-of-thought, raw tool payloads, credentials, and private command data
are not runtime-monitor content.

## Compatibility

CFR prefers native capability detection over version branching. Doctor can
generate and inspect the installed app-server schema and verifies the methods
CFR actually requires.

The detailed historical coexistence contract required by current diagnostics
is retained under `docs/reference/`.
