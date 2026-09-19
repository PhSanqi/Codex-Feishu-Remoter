# CFR Local Control Plane Security (FROZEN_v2)

## Local boundary

The Control API binds only to `127.0.0.1`. It has no LAN binding, public
tunnel, cloud relay, or remote control panel.

## Bootstrap, session, and CSRF

`run_cfr_control.py` supplies a one-time high-entropy bootstrap nonce. Its
successful exchange creates fresh session and CSRF tokens, sets an HttpOnly
`SameSite=Strict` session cookie, redirects to a clean URL, and invalidates the
nonce. Every mutation requires both the authenticated local session and the
CSRF double-submit token.

## Implemented M3B safety boundaries

- The browser obtains data only through `ControlReadModel`; it never opens
  SQLite or receives store internals.
- Fixed Control API commands contain no shell, `/exec`, arbitrary command
  runner, or frontend-direct SQLite operation.
- Codex defaults accept only model, reasoning effort, and service tier, validate
  them against the installed runtime, and use its fixed `config/batchWrite`
  keys. They accept no arbitrary configuration path or TOML write.
- Feishu credential status is sanitized. The Control Center has no credential
  read or write endpoint and never returns, copies, logs, or displays an App
  Secret.
- Approvals are a bounded durable read model. Allow, Decline, and Cancel remain
  Feishu-only; there is no browser approval mutation, auto-approve policy,
  accept-for-session, execpolicy amendment, or network-policy amendment.

## Deferred surfaces

Security configuration, MCP/Skills management, logs, usage, WebSocket events,
and remote Control Center exposure are not implemented M3B surfaces. They
require separate authority and sanitization reviews before exposure.

## Feishu first-user pairing

First-user pairing is a local bootstrap path, not normal remote execution. It
requires all of: authenticated loopback Control Center, CSRF, a one-time
in-memory six-digit pairing code, a private Feishu user message, and a second
local Control Center confirmation. The code, raw sender open ID, and candidate
exist only in Supervisor memory; the read model exposes only a masked candidate.

The pairing connection uses the normal `feishu:<app_namespace>` lease and is
mutually exclusive with the normal Feishu runtime. It never enters Gateway,
creates an inbox row/session/approval/Codex turn, or auto-authorizes a sender.
Existing Feishu session binding is routing state, not authorization, and is
never used as a pairing source. App Secrets remain keyring-only; only the
non-secret allowed-open-ID and workspace-root policies are persisted.

The Runtime Console is a bounded process-local sanitized operator timeline.
It exposes structured CFR-owned lifecycle stages and safe error codes, never
raw exception tracebacks, secrets, tokens, pairing codes, identifiers, message
content, or SDK/stdout logs. Invalid workspace roots remain visible as policy
errors and require an explicit authenticated user removal or replacement.
