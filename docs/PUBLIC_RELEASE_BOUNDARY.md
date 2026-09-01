# CFR Public Release Boundary

Status: **authoritative for public GitHub preparation**.

This document defines what may be published, what remains local-only, and what
a new machine is expected to configure. It does not change CFR runtime
behavior.

## Product model

CFR is local-first and self-hosted.

- Code execution authority remains the installed native Codex runtime.
- Feishu integration uses a user-owned Feishu custom app (BYO Feishu App).
- CFR does not operate a shared multi-tenant Feishu backend.
- Project migration is supported; runtime/account state migration is not.

## Supported migration boundary

A new machine starts from source plus fresh local configuration.

Expected one-time setup:

1. Install Python and project dependencies.
2. Install Codex and log in normally.
3. Install/configure the user's network proxy when required.
4. Clone CFR and build the Control Center.
5. Create or select a Feishu custom app owned by the user/organization.
6. Enter the Feishu App ID and App Secret through CFR's credential flow.
7. Bind the intended Feishu operator and choose allowed workspace roots.

Not migrated:

- CFR SQLite databases and prior bindings.
- Codex native thread/session history.
- `~/.codex` authentication/state databases.
- OS keyring / credential-manager entries.
- Feishu pairing state.

No source-code edit should be required on the new machine.

## Feishu provisioning boundary

Public CFR uses **Bring Your Own Feishu App**.

The user/administrator remains responsible for the Feishu control-plane step:

- create the custom app in Feishu Open Platform;
- enable bot capability;
- grant the documented minimum permissions;
- configure the supported event/long-connection mode;
- publish/approve the app as required by the tenant;
- obtain the App ID and App Secret.

CFR may guide and validate these steps, but must not automate the Feishu
developer console through browser scripting.

Operator binding is a separate step. The existing six-digit pairing flow is a
supported baseline. A future QR/OAuth binding flow may replace that operator
pairing UX, but QR/OAuth does **not** remove the need for an existing Feishu
application and is not required for the first public release.

## Secrets and local state

Never publish:

- Feishu App Secret;
- tenant/user access tokens;
- authorization headers;
- Codex `auth.json` or equivalent authentication material;
- CFR SQLite databases;
- Codex rollout/session files;
- OS credential-store exports;
- private workspace contents captured by diagnostics;
- runtime evidence containing user-specific identifiers or messages.

The current credential contract is intentional:

- Feishu App ID: local non-secret CFR configuration;
- Feishu App Secret: OS secret store only;
- environment variables: temporary override/CI/debug path only.

## Public source contents

Expected in the public repository:

- `src/` production source;
- `tests/` deterministic tests and sanitized fixtures;
- `scripts/` portable build/diagnostic/release scripts;
- `m3_control/src/` and build configuration;
- maintained architecture and user documentation;
- launchers required for normal local operation;
- `pyproject.toml`, `LICENSE`, `SECURITY.md`, and repository metadata.

Excluded from the public release boundary:

- `.tmp/` runtime evidence;
- `*.sqlite3` databases;
- `CFR_SOURCE_SNAPSHOT_*.zip` acceptance snapshots;
- Python caches and test caches;
- `m3_control/node_modules/`;
- generated `m3_control/dist/` assets;
- historical `experiments/` and one-off acceptance evidence unless rewritten
  into a sanitized, reproducible maintained test/document;
- machine-specific absolute paths and historical native Thread/rollout IDs.

The development repository may temporarily contain some excluded historical
material. That does not make it eligible for publication; the public-release
gate must pass before publishing a branch/tag.

## Codex compatibility policy

CFR must couple to native capabilities, not a hard-coded Codex version.

- No `if version == ...` production routing for normal compatibility.
- Doctor/schema probes verify the native methods CFR actually requires.
- A newly installed Codex version is accepted when the capability checks and
  focused native probes pass.
- Experimental Codex capabilities must not become mandatory when a stable
  process-local or protocol-level alternative exists.

The currently tested Codex version may be documented, but it is evidence, not
the compatibility contract.

## Network portability policy

CFR must not hard-code a proxy product or localhost proxy port.

For fixed system proxies, CFR may discover the current OS/environment proxy and
project it only into CFR-owned Codex child processes. Native Codex/OS behavior
remains the authority for PAC/WPAD-only configurations.

Changing computers or proxy applications must not require a source edit.

## Release gate

Before a public GitHub push/tag, run:

```text
python scripts/check_public_release.py
```

The gate is intentionally independent from `START_CFR.cmd`; it cannot block or
modify normal CFR runtime behavior.

PASS means the tracked source tree satisfies the mechanical portion of this
boundary. It does not replace a final human review for credentials, licensing,
third-party notices, documentation accuracy, or private user content.
