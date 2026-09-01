# Troubleshooting

## Codex is installed but CFR reports incompatibility

Run:

```powershell
python .\scripts\run_cfr.py doctor --json --gate-origin host_manual
```

Check `CodexInterfaceCompatibility` and `MissingRequiredMethods`. CFR does not
reject an installation merely because its Codex version differs from the last
validated version.

## Codex responses are unexpectedly slow

Run:

```powershell
python .\scripts\run_codex_network_preflight.py --timeout 60 --gate-origin host_manual
```

The Control Center runtime monitor also exposes transport retries/fallback,
TTFN, TTFT, CFR-controlled overhead, and model/upstream wait. A large TTFT with
small CFR overhead is not a CFR queue/cleanup bottleneck.

On Windows, a fixed system proxy can be discovered and projected into CFR-owned
Codex child processes. Global proxy mode is not required when the configured
smart/system proxy routes Codex traffic correctly.

## Control Center says the UI build is missing

```powershell
cd m3_control
npm ci
npm run build
```

Then restart `START_CFR.cmd`.

## Feishu credentials are missing

Use the Control Center setup flow, or import credentials from a temporary shell
as documented in [FEISHU_SETUP.md](FEISHU_SETUP.md). Never create a persistent
plaintext secret file.
