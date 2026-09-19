# CFR Linux port status

This checkout keeps the Windows Current feature model while replacing only
Windows-only hosting details on Linux.

## Native on Linux

- Codex app-server over stdio, thread/session lifecycle, model catalog,
  reasoning/service-tier settings, approvals, runtime leases and rollout
  history.
- Feishu long connection, durable SQLite state, per-chat routing, progress,
  approvals, attachments and artifact delivery.
- Code Surface and its native Codex attachment/artifact protocol.
- Chat Surface through `chrome-use` + its Chrome extension/Native Messaging
  host. CFR uses the named `cfr-chat` session only; user tabs are never adopted.
- The `cfr-chat` session is kept in a separate Chrome window. Before CFR drives
  it, the bridge verifies that every tab in that window was created by CFR,
  then keeps that window unfocused and minimized for silent operation.
- Local Control Center on `127.0.0.1` and user-level systemd hosting.

## Intentionally platform-specific

- Embedded WebView2 and Chrome DevTools MCP are not part of the Linux Chat
  runtime. Linux does not require Remote Debugging.
- Codex Desktop URI/process-tree handoff is Windows-only. It is exposed as
  unsupported on Linux rather than emulated.
- Windows EXE/tray packaging remains Windows-only; Linux uses source + systemd.

## Installation

```bash
cd ~/codex-workspace/CFR
bash scripts/bootstrap_linux.sh
./START_CFR.sh
```

The Linux source deployment defaults its local Control Center to
`127.0.0.1:18787` on this host so it can coexist with DevSpace Control, which
already owns `127.0.0.1:8787`. Override it with `CFR_CONTROL_PORT` if needed.

After first-run configuration is complete, `run_cfr_control.py` automatically
starts Feishu when setup is ready. If Chat is the selected surface it validates
the chrome-use extension/Native Messaging relay and the isolated minimized CFR
Chat window first.

To keep CFR persistent for the login user:

```bash
./scripts/install_systemd_user.sh
```

The Control Center intentionally binds to loopback only. Use an SSH/VPN tunnel
if remote browser access is required; do not expose the control port directly.
