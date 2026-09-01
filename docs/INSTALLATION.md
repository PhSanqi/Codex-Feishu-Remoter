# Installation

This guide describes a clean installation on a new machine. CFR does not
require state migration from an existing installation.

## 1. Install prerequisites

Install:

- Python 3.11+;
- Node.js/npm;
- the native Codex CLI;
- Git;
- a Feishu desktop/mobile client if Feishu is used for operation.

Log in to Codex normally before configuring CFR.

## 2. Clone and install CFR

```powershell
git clone <repository-url>
cd CFR
python -m pip install -e ".[feishu]"
```

Build the browser Control Center:

```powershell
cd m3_control
npm ci
npm run build
cd ..
```

`m3_control/dist/` is generated locally and is intentionally not stored in Git.

## 3. Verify Codex compatibility

```powershell
python .\scripts\run_cfr.py doctor --json --gate-origin host_manual
```

The installed Codex version number is evidence only. The important result is:

```text
CodexInterfaceCompatibility = PASS
```

CFR checks the native app-server methods it actually requires rather than
hard-coding a single Codex version.

## 4. Verify network routing

```powershell
python .\scripts\run_codex_network_preflight.py --timeout 60 --gate-origin host_manual
```

When a fixed Windows/environment proxy is configured, CFR projects the detected
HTTP/HTTPS proxy only into CFR-owned Codex child processes. Proxy product names
and localhost ports are not hard-coded.

PAC/WPAD-only routing remains native Codex/OS authority.

## 5. Configure Feishu

Follow [FEISHU_SETUP.md](FEISHU_SETUP.md). The public deployment model is
Bring Your Own Feishu App: the user or organization owns the Feishu custom app
and its credentials.

## 6. Start CFR

```text
START_CFR.cmd
```

The Control Center opens on localhost. Complete:

1. Feishu credential configuration;
2. operator binding;
3. allowed-workspace selection;
4. Feishu runtime startup.

## What is intentionally not migrated

For a normal project move, do not copy:

- `cfr.sqlite3` or other CFR runtime databases;
- `~/.codex` authentication/session state;
- Codex rollout files;
- Feishu pairing state;
- OS keyring/credential-manager entries;
- `.tmp/` diagnostics or runtime evidence.

Create fresh local configuration instead.
