# Security Model

## Operator boundary

Feishu execution requires configured app credentials, at least one authorized
operator identity, and at least one allowed local workspace root. Unauthorized
senders and unsupported message scopes fail closed before a Codex job is
created.

## Workspace boundary

Workspace paths are resolved canonically and must remain inside an explicitly
allowed root. Missing paths, files where a directory is required, relative-path
escapes, symlink/junction escapes, and paths outside the allowlist are rejected.

## Command boundary

CFR does not expose a remote raw-shell endpoint. Work enters through the native
Codex execution path and its existing approval model.

## Secret boundary

The Feishu App Secret is stored through the OS secret store. CFR deliberately
has no plaintext secret-file fallback. Access tokens, cookies, authorization
headers, and SDK request details must not be written to SQLite, diagnostics,
source snapshots, or Git.

## Runtime telemetry boundary

Allowed telemetry includes safe lifecycle metadata such as stage, timing,
token/context counters, and safe tool names/status. It must not expose raw
reasoning text, prompts, tool arguments/results, credentials, or private shell
contents.

## Reporting vulnerabilities

See the repository root [SECURITY.md](../SECURITY.md).
