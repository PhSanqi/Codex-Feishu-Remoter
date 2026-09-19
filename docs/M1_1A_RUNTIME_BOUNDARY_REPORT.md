# CFR M1.1A Runtime Boundary Hardening

## Local gate

Latest local boundary artifact: `.tmp/m1-1-boundary/20260818T040950Z-c6513f47/`.

- M1 freeze / protocol SHA: PASS
- Compile: PASS
- Unit tests: PASS (`58/58`)
- Source checkout CLI bootstrap: PASS
- Installed CLI metadata: PASS (`cfr = cfr.cli.main:cli`)
- Launcher unit tests: PASS (`4/4`)
- Platform unit tests: PASS (`2/2`)
- Launcher normalization: PASS (targeted evidence)
- Platform capabilities: PASS (targeted evidence)
- Fake app-server cold-start isolation: PASS
- Auth unit has no real Codex dependency: PASS
- Canonical path portability: PASS
- Windows extended-prefix and UNC handling: PASS
- Durable runtime lease probe: PASS
  - ConcurrentAcquireBlocked
  - ReleaseHandoff
  - CrashLeaseLeftBehind
  - PreExpiryAcquireBlocked
  - CrashExpiryRecovery
  - StaleReleaseFenced
  - HeartbeatPreventsExpiry
- Post-hardening M1 Host regression: PASS (`M1Verdict=CFR_M1_CODEX_CORE_COMPLETE`, `BlockingIssues=[]`)
- Doctor live: PASS (`GateOrigin=host_manual`, `RuntimeExecutionContext=HOST`, `LiveModelProbe=PASS`)
- Blocking issues: NONE

## Implemented boundaries

- `CfrThreadRuntimeLeaseManager` is durable, fenced, generation-based, heartbeat-aware, and scoped to one native Codex thread. It is CFR-internal and is not a Broker or Workspace Arbitrator lease.
- `CodexLauncher` supports explicit executable, `CFR_CODEX_BIN`, and platform-aware PATH resolution for `codex.cmd`, `codex.exe`, and `codex` without `shell=True`.
- `routing_config_files()` and runtime diagnostics use the same resolved CFR Codex home as the child process and report home consistency.
- `cfr doctor` is read-only by default; `--live` is the only mode that performs one model-backed probe.
- Path comparison uses a comparison-only canonical key for Windows extended prefixes and case normalization; stored rollout paths are not rewritten.
- Extended-prefix parsing is syntax-first, so raw POSIX fixtures are normalized before host-specific resolution.
- The crash-recovery probe now exits immediately after acquisition without release or heartbeat, proves pre-TTL blocking, then proves TTL reclamation at generation `A+1`.
- `:memory:` runtime-lease databases are explicitly documented and diagnosed as test-only non-durable coordination.
- Source checkout now has an explicit `scripts/run_cfr.py` bootstrap; installed packages expose the `cfr` console entry point through setuptools `src` discovery.

## Host gate required

Run in a normal Windows PowerShell/Terminal:

```powershell
python .\scripts\run_cfr.py doctor --live --gate-origin host_manual
```

M1.1A is complete: the bootstrap-backed Host Doctor Live gate and post-hardening M1 regression are both evidenced as PASS. M2 preserves the frozen M1 behavioral contracts, but its optional server-request plumbing still requires a post-M2 Host M1 regression before M2 completion.

macOS and Linux live validation remain `NOT_RUN_NO_HOST`.
