# M3 Desktop Shell Specification (OPTIONAL_FUTURE_SHELL)

Status: `OPTIONAL_FUTURE_SHELL`
Requirement: `NOT_REQUIRED_FOR_M3`

The primary M3 experience is the browser Web Control Plane. A desktop EXE, Tauri shell, system tray, installer, and updater are deferred and are not conditions for M3 completion.

## Future shell boundary

If later approved, a desktop shell may host the same web UI and attach to the same local Control API. It must not create a second backend, a second state model, a second approval state machine, or a desktop-only execution path.

The shell may provide single-instance behavior, window lifecycle, optional tray affordances, and supervision of the CFR backend it owns. It may start or attach after a health check and must stop only its owned process identity. It must never kill arbitrary `python.exe`, `codex.exe`, or unrelated processes. Tray actions must not include “approve all”.

## Browser parity

Browser and any future desktop shell use identical `/api/v1` endpoints, `/ws/events` envelopes, read models, approval semantics, authentication, and CSRF rules. Desktop-specific behavior is a shell convenience, not a business authorization boundary.

## Deferred stages

- M3C: optional tray/shell.
- M3D: optional EXE, packaging, installer, signing, autostart, and updater.

No desktop implementation is included in this rebaseline.
