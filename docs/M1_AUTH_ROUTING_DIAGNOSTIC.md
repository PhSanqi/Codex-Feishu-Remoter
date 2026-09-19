# CFR M1 Auth / Routing / Stream Transport Diagnostic

## Latest Host manual execution

Artifact: `.tmp/auth-routing-diagnostic/<RunId>/result.json` with `GateOrigin=host_manual`.

| Field | Result |
|---|---|
| LoginStatus | `LOGGED_IN` |
| AuthMode | `CHATGPT` |
| PlanType | `team` |
| RoutingConsistency | `CONSISTENT` |
| DefaultTransportProbe | completed / FinalAck `YES` |
| Verdict | `PASS` |

The explicit CFR Codex home resolved to the current user's native Codex home (for example `%USERPROFILE%\.codex` on Windows) and the child environment carried `CODEX_HOME` explicitly. Host runtime validation passed. No auth, provider, proxy, ACL, session-store, or system setting was changed.

## Prior Desktop-agent observation

The earlier Desktop-agent result remains historical: app-server `stdout EOF` with access denied while initializing the shared CFR home. Its interpretation is `HOST_RUNTIME_VALIDATION_REQUIRED`, not an auth decision or Host verdict.

## Safety boundary

Credentials, tokens, cookies, authorization headers, and response bodies are not recorded. The pinned v1.2 coexistence reference is unchanged.
