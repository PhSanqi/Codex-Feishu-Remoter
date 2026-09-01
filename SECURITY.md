# Security

CFR is local-first software that can access Codex, Feishu, local workspaces,
and connected tools. Treat credentials and local runtime state as private.

Do not publish or attach App Secrets, access tokens, Codex authentication
files, local databases, rollout/session files, private workspace contents, or
diagnostic artifacts containing user data to a public GitHub issue.

Feishu App Secrets are intentionally stored through the operating-system
secret store. CFR has no plaintext secret-file fallback. App IDs are not
treated as secrets, but are still machine/user configuration rather than
source-code defaults.

If GitHub private vulnerability reporting is enabled for the repository, use
that channel for security-sensitive reports. Otherwise, open a public issue
containing only a minimal, redacted description and request a private contact
channel before sharing reproduction material.

The supported public-release model is fresh installation. Copying Codex
authentication state, CFR SQLite state, or OS secret-store contents between
machines is outside the supported migration boundary.
