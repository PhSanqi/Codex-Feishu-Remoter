# Code Surface runtime architecture

This document describes the post-refactor CFR Code Surface execution path.  The
goal is to keep native Codex semantics and desktop coexistence while making
Feishu delivery, progress, history, and long-running ownership failure-isolated.

## Runtime boundaries

The production path is now split into four responsibilities:

1. `FeishuGateway` is the fast trust boundary. It normalizes, authorizes,
   persists, deduplicates, and queues an inbound event.
2. `FeishuDaemon` owns queue admission, per-chat serialization, slash commands,
   the Feishu daemon lease, and lifecycle/shutdown.
3. `CodeSurfaceRuntime` owns one Code task from pending attachments through the
   native Codex turn and final delivery. Code execution is no longer embedded in
   the daemon command/lifecycle implementation.
4. `CodexAdapter` remains the native thread writer authority. Each turn acquires
   the durable CFR thread lease, starts a Codex app-server, resumes the native
   thread, runs one turn, closes the app-server, and releases writer ownership.

The per-turn app-server is intentional. Keeping it alive would reduce a fixed
startup/resume cost, but it would also retain native writer ownership and break
the existing CFR/Codex Desktop coexistence contract. Growing conversation cost
must be controlled at the thread/context level instead of by pooling that
writer.

## Code task lifecycle

For an ordinary Feishu Code message:

1. The inbox row is claimed and the Feishu chat lock serializes the task.
2. Pending user attachments are validated and prepared. Images use native
   `localImage`; ordinary files are staged beneath `.cfr/attachments` only when
   needed.
3. `CodeSurfaceRuntime` begins telemetry and a best-effort progress card.
4. `CodexAdapter` acquires the process-local and durable thread writer leases.
5. Codex app-server resumes the native thread with the Feishu chat's persisted
   native permission preset and runs the turn.
6. Approval feedback is finalized from the real native terminal result.
7. Final text/files/images are delivered before the terminal progress-card
   update is requested. Optional progress can therefore never outrank the user
   deliverable.
8. Input attachment cache is cleaned and writer ownership is released.

A task is marked completed only after this runtime returns a delivery result to
the daemon. A provider delivery failure therefore remains visible as a failed
inbox job rather than being silently reported as completed.

## Native approval modes

Code Surface exposes `/approval` as a per-Feishu-chat selection over Codex's
current app-server permission fields; CFR does not emulate approval policy:

- `ask` / `全部请求`: `approvalPolicy=on-request`,
  `approvalsReviewer=user`, `sandbox=workspace-write`.
- `auto` / `替我审批`: `approvalPolicy=on-request`,
  `approvalsReviewer=auto_review`, `sandbox=workspace-write`.
- `full` / `全部开放权限`: `approvalPolicy=never`,
  `sandbox=danger-full-access`.

The selected preset is reapplied on each native `thread/start` or
`thread/resume`, so a per-turn app-server does not silently revert the mode.
`full` is intentionally explicit because it disables approval prompts and the
workspace sandbox.

Current Codex v2 `item/fileChange/requestApproval` requests no longer contain
`cwd` or `changedPaths`. CFR therefore takes the authoritative active workspace
from `CodeSurfaceRuntime` and correlates `item/fileChange/patchUpdated` by
`threadId/turnId/itemId` to recover changed paths when Codex emitted that
notification. If no patch projection is available, the approval card says that
the current protocol omitted per-path details rather than displaying an
invented `<unknown>` operation.

## Final result and artifact delivery

Final text and artifacts are independent delivery channels. A completed native
turn may legitimately have no text when its result is an image/file.

The artifact collector accepts only trusted local roots:

- the active workspace;
- newly created generic OS-temp deliverables;
- the exact native Codex `generated_images/<thread_id>` root for the active
  thread.

It recognizes Markdown links, inline paths, and normal Windows/relative local
paths. Native generated images are detected independently of the final assistant
text. If the user explicitly asks for an image and the current turn only views
an earlier generated image, CFR can resend the latest image from that same
thread-specific generated-image root.

User input staging under `.cfr/attachments` is excluded from output discovery.
Generic stale temp files are excluded. The workspace is deliberately not
recursively scanned after every turn; repository size must not become a new
per-turn latency/privacy cost.

Blank final text is never sent to Feishu. This avoids provider error `230001`
(`invalid message content`) for artifact-only turns.

Critical final delivery gets the normal outbound timeout and one idempotent
retry for timeout/unknown transient failures. The deterministic Feishu UUID is
unchanged across that retry. Format, permission, and disconnected errors are not
blindly retried.

## Streaming progress

Progress is explicitly lower priority than final delivery.

- Native Codex event volume is coalesced into one latest in-memory projection.
- The progress worker reads that projection every two seconds; token/delta event
  frequency therefore cannot create Feishu API frequency or progress-thread CPU
  churn.
- Terminal state wakes the worker immediately, but final user content is sent
  before the terminal-card request is prioritized.
- Card updates have a maximum three-second provider wait, shorter than critical
  final delivery. A slow/failed progress update disables that progress stream
  only; it does not fail the Codex turn.
- Context-window percentage is projected into the Code progress card when the
  native `thread/tokenUsage/updated` event is available.

## Lease and shutdown semantics

The durable Code thread lease is fail-closed on proven ownership loss, but a
single transient SQLite writer error no longer means ownership was lost. CFR
keeps the last confirmed lease only until its confirmed expiry and retries the
heartbeat. Once that expiry has passed, an unavailable heartbeat fails closed.

The Feishu daemon lease follows the same principle. A fatal daemon lease loss is
surfaced through the supervisor as `degraded`; it cannot remain visually
`running` while workers have already stopped.

Shutdown first stops admission and active Codex work, then waits for Feishu
workers to drain while transport/database resources are still open. CFR refuses
to tear those resources out from under live workers. This removes the prior path
where a late final reply could race transport shutdown and create un-awaited
Channel coroutines.

At the Channel boundary, any coroutine rejected because the transport is already
stopped is explicitly closed, so the error path itself cannot produce
`coroutine was never awaited` warnings.

## Conversation history

CFR does not duplicate full native conversation transcripts into its SQLite
database.

Code history is read from the native Codex rollout with `/history [1-50]`. The
reader seeks only the last 4 MiB, discards a partial first JSONL record if needed,
and deduplicates Codex's duplicate visible event/response records. Therefore the
history-read cost is bounded and does not scale with the entire rollout file.

Chat history uses `/history [1-50]` against the currently bound native ChatGPT
conversation. `/chats` lists visible ChatGPT conversations and `/chat ...` opens
one before history is read.

This deliberately keeps CFR's own database small and lets each execution
surface remain authoritative for its own history.

## Long-thread time and disk behavior

Adding turns to the same Code thread increases two different native costs:

- **Model/context cost:** later turns carry more conversation context. Cached
  input can reduce provider-side recomputation, but the context window still
  fills and later turns can become slower or hit compaction/context limits.
- **Native rollout disk cost:** Codex appends native events, tool output, and
  attachment/image records to the JSONL rollout. Large tool/image records can
  dominate disk growth.

Keeping the CFR backend process alive is not itself the cause of that growth.
Restarting CFR also does not reset it, because the same native thread is resumed.
Use `/new` when the current task no longer needs the accumulated thread context.

`/status` reports the native rollout size and the most recent context percentage
when telemetry is available. At 80% or higher CFR explicitly warns that context
pressure is high.

## Failure isolation matrix

| Failure | Code task | Final delivery | CFR daemon |
| --- | --- | --- | --- |
| Progress-card timeout/failure | continues | continues | continues |
| Transient final outbound timeout | completed native turn | one idempotent retry | continues |
| Invalid/permission final outbound response | native result retained | job reports failed | continues |
| Blank assistant text + artifact | completed | artifact only | continues |
| Transient thread-lease SQLite contention before confirmed expiry | continues/retries | unaffected | continues |
| Proven thread lease loss | fails closed | error path | continues for other threads |
| Proven daemon lease loss | admission/workers stop | no new delivery work | supervisor reports degraded |
| Worker still alive during stop deadline | stop fails closed | resources kept open | no unsafe teardown |
