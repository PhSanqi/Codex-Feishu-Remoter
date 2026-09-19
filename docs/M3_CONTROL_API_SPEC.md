# CFR Local Control Plane API (v2)

Status: `M3BStatus: CLOSED` (2026-08-25). API version: `/api/v1`.

## Transport and bootstrap

The Control API binds to `127.0.0.1` only. `run_cfr_control.py` starts the
local backend and prints its bootstrap URL:

`http://127.0.0.1:<port>/?bootstrap=<nonce>`

The bootstrap nonce is single-use. The server validates it, creates a random session of at least 256 bits, sets an HttpOnly SameSite=Strict cookie, invalidates the nonce, and redirects to a clean URL. Sessions are short-lived/renewable. Mutations require CSRF protection through a CSRF token or same-origin double-submit.

## Read endpoints

```text
GET /api/v1/status
GET /api/v1/runtime
GET /api/v1/sessions
GET /api/v1/bindings
GET /api/v1/jobs
GET /api/v1/approvals
GET /api/v1/models
GET /api/v1/surfaces
GET /api/v1/capabilities
GET /api/v1/settings
GET /api/v1/feishu
GET /api/v1/doctor
```

Responses are sanitized read models. Models and capabilities come from the installed runtime registry where available. Secret-bearing settings expose only masked identifiers and configured yes/no state.

`GET /api/v1/sessions` is the current Feishu chat-binding view. `GET /api/v1/bindings` is the durable CFR/Codex native-thread binding catalog; it exposes thread metadata and state only, never rollout contents or prompt/response data.

The Feishu projection preserves the backward-compatible `workspace_roots`
list and also returns `workspace_roots_valid` and `invalid_workspace_roots`.
These are a preflight view only: they use the existing Feishu workspace
directory validation and never mutate stored policy. Activity is bounded and
process-local; rows may include timestamp, level, component, stage, event/code,
and a sanitized human-readable message. It is not an endpoint for stdout, SDK
logs, tracebacks, secrets, or message payloads.

`GET /api/v1/approvals` is a bounded (latest 100), authenticated, read-only
projection of durable CFR approval records. Each item contains only approval,
thread, and turn identifiers; durable approval state; decision; feedback
state; request kind; and created/updated timestamps. It excludes requester and
operator identities, request IDs, commands, payloads, workspaces, card data,
and credentials. Approval decisions remain Feishu-only: the Control Center has
no approval mutation endpoint.

## Mutations

```text
POST /api/v1/runtime/remote-execution
POST /api/v1/runtime/accept-new-work
POST /api/v1/runtime/drain

POST /api/v1/feishu/start
POST /api/v1/feishu/stop
POST /api/v1/feishu/reconnect

POST /api/v1/feishu/pairing/start
POST /api/v1/feishu/pairing/cancel
POST /api/v1/feishu/pairing/confirm

POST /api/v1/sessions/{chat}/unbind
POST /api/v1/threads/{thread_id}/open-desktop
```

`POST /api/v1/threads/{thread_id}/open-desktop` is an authenticated,
CSRF-protected navigation request for an existing durable binding. It accepts
no URI or request body. The binding must have no active turn and an `idle`
writer; CFR then asks the OS to open exactly
`codex://threads/<FULL_THREAD_ID>`. It does not create or resume a turn and
does not mutate binding or desktop synchronization state.

```text
PUT /api/v1/settings/codex/model-defaults
```

This authenticated, CSRF-protected mutation accepts only `model`,
`reasoning_effort`, and `service_tier` (each a string or JSON `null`). CFR
runtime-validates them against installed Codex `model/list`, then sends exactly
the fixed `model`, `model_reasoning_effort`, and `service_tier` keys to Codex
`config/batchWrite` with `replace`. It writes no TOML directly, accepts no
arbitrary key path, re-reads the effective settings afterward, and applies to
new threads only. `okOverridden` remains a successful write with a sanitized
override warning. There is no `/shell`, `/exec`, command runner, arbitrary
configuration editor, or frontend-direct SQLite endpoint.

## Deliberately not implemented in M3B

There are no Control API endpoints for Feishu credential mutation, approval
actions, turn interruption, logs, usage, MCP, Skills, security settings,
arbitrary configuration, or WebSocket events. Approval decisions remain
Feishu-only. Those capabilities require their own authority and workload
contracts before they can be exposed.

## Layering

```text
UI -> Control API -> service layer -> CFR core/stores
```

API handlers use stable `/api/v1` contracts and do not import internal stores into the frontend.

## Feishu first-user pairing

The three pairing mutations use the same local session and CSRF checks as all
other Control mutations. Pairing is connection-only: it starts the existing
Channel transport under the existing Feishu connection lease, but creates no
Feishu daemon, Codex turn, inbox row, session, approval, or authorization.

`start` needs only configured App ID and App Secret. It creates an in-memory,
six-digit code valid for five minutes. A private, text-only Feishu message
`绑定 <code>` captures one sender identity as a masked candidate. The candidate
is never authorized by the message itself and the temporary connection is then
released.

`confirm` accepts exactly `{ "workspace_root": "..." }`. The candidate is held
server-side; the browser cannot submit an open ID or arbitrary allowlist. A
successful local confirmation atomically adds the candidate and canonical
existing workspace root to the non-secret persistent policy. Environment
allowlist variables still override that policy when non-empty.
