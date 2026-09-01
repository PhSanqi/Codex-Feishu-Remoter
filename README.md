# CFR

CFR is a local-first bridge between Feishu and the native Codex runtime. It
keeps code execution on the user's machine, exposes a browser Control Center,
and lets an authorized Feishu operator run work in explicitly allowed local
workspaces.

## What it provides

- Native Codex threads and turns rather than a parallel execution engine.
- Feishu private-chat task delivery with compact progress cards and full final
  replies.
- Persistent local bindings between Feishu conversations and Codex threads.
- Native model, reasoning, service-tier, steer, redirect, and stop controls
  where supported by the installed Codex app-server interface.
- A local Control Center for runtime status, latency attribution, token/context
  usage, safe tool activity, and Codex Desktop thread handoff.
- Local-first security: operator allowlists, workspace allowlists, OS-backed
  secret storage, and no plaintext App Secret fallback.
- Capability-based Codex compatibility checks and proxy-aware network
  diagnostics.

## Requirements

- Windows 11 is the currently validated host platform.
- Python 3.11 or newer.
- Node.js/npm for building the Control Center frontend.
- A working native Codex CLI installation and an authenticated Codex account.
- A user-owned Feishu custom app with bot capability enabled.

## Quick start

```powershell
git clone <your-cfr-repository-url>
cd CFR

python -m pip install -e ".[feishu]"

cd m3_control
npm ci
npm run build
cd ..

python .\scripts\run_cfr.py doctor --json --gate-origin host_manual
python .\scripts\run_codex_network_preflight.py --timeout 60 --gate-origin host_manual
```

Create and configure a Feishu custom app using
[`docs/FEISHU_SETUP.md`](docs/FEISHU_SETUP.md), then start CFR with:

```text
START_CFR.cmd
```

The browser Control Center guides local Feishu credential setup, operator
binding, and allowed-workspace selection. Existing machines can continue using
the six-digit Feishu pairing flow.

## Project migration

CFR supports **project migration, not runtime-state migration**. On a new
machine, clone the source and configure fresh local state. Do not copy CFR
SQLite databases, Codex authentication/session databases, Feishu pairing state,
or OS credential-store contents as part of a normal project move.

No source edit should be required for a different Codex install path or a
different fixed system-proxy port. CFR discovers the installed Codex interface
and the host proxy route at runtime.

See [`docs/INSTALLATION.md`](docs/INSTALLATION.md) for the complete clean-machine
flow.

## Documentation

- [Installation and clean-machine setup](docs/INSTALLATION.md)
- [Feishu app setup](docs/FEISHU_SETUP.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Security model](docs/SECURITY_MODEL.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Public release boundary](docs/PUBLIC_RELEASE_BOUNDARY.md)

## Development checks

```powershell
python -m unittest discover -s tests/unit -p "test*.py"
python scripts/check_public_release.py

cd m3_control
npm ci
npm run build
```

Generated frontend assets, runtime databases, diagnostics, source snapshots,
and historical experiment evidence are intentionally excluded from Git.

## License

MIT. See [LICENSE](LICENSE).
