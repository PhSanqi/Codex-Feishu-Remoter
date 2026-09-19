# CFR M2 Feishu Security Model

## Operator boundary

Execution requires all of:

- Feishu App ID and App Secret resolved by `FeishuCredentialResolver` (process environment override, then persistent App ID plus OS-keyring App Secret).
- At least one `CFR_FEISHU_ALLOWED_OPEN_IDS` entry.
- At least one `CFR_FEISHU_ALLOWED_WORKSPACE_ROOTS` directory.

Unauthorized senders, app/bot senders, and out-of-scope messages are ignored without creating a Codex job. `p2p` is allowed after sender and operator checks. `group` and `topic` require `enable_group_chats=true` plus the Channel SDK's exact `mentioned_bot=true`; unknown, empty, and future-unrecognized chat types fail closed. `/cfr whoami` may report only the requesting sender's own open ID.

## Workspace boundary

`/cfr new` and `/cfr use` require an existing directory whose resolved canonical path is inside a configured allowed root. Symlink/junction escapes, files, missing directories, relative paths, and canonical paths outside the roots are rejected with `FEISHU_WORKSPACE_NOT_ALLOWED`. This is a local filesystem authorization boundary, not `workspace-arbitrator/1`.

## Command boundary

There is no `/cfr shell`, `/cfr exec`, `/cfr python`, `/cfr sql`, `/cfr db`, `/cfr secret`, or `/cfr env`. All work enters through `CodexAdapter`; raw remote command execution is not exposed.

## Secret boundary

App secrets, access tokens, cookies, authorization headers, and SDK request details are never written to SQLite, artifacts, logs, or doctor output. Settings representation redacts the secret. App IDs and identifiers are shortened in ordinary diagnostics. If the secure keyring is unavailable, credential writes fail closed; CFR has no plaintext secret-file fallback.

## Inbound and outbound idempotency

The Channel SDK is responsible for transport normalization and reconnect lifecycle; CFR remains authoritative for sender/workspace allowlists, durable inbox dedupe, and session binding. Inbound dedupe is keyed only by Feishu `message_id`; `event_id` is diagnostic metadata. Outbound logical replies reuse one deterministic UUID for retries. Failed sends are marked retryable and do not poison the reservation. The durable inbox prevents duplicate Codex turns during callback redelivery or short daemon overlap.

`feishu run --setup-only` establishes only the Channel SDK connection and can report normalized identifiers. It does not instantiate `FeishuDaemon`, recover inbox rows, start workers, create a `CodexAdapter`, or process `/cfr new` or ordinary text.

Setup-only and `feishu doctor --live` share the same app connection lease as the normal daemon. A connection owner heartbeats the lease and releases it on shutdown, so only one local Channel connection can exist for an app namespace.

Only normalized `raw_content_type=text` messages are eligible for M2 Codex execution. Unknown sender identity and bot/app senders fail closed; `file`, `image`, `post`, and missing/unknown content types are ignored as unsupported.

## Approval boundary

Unknown Codex server requests are rejected. M2B accepts only command and file-change approvals. There is no auto-approval or session-wide approval. Approval cards contain opaque approval IDs and safe summaries, are sent privately to the requesting authorized operator, and require that same operator for resolution. Expired, orphaned, duplicate, or mismatched actions cannot resolve a request.
