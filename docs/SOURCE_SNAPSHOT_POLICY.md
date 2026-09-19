# CFR Source Snapshot Policy

At the close of every Codex implementation or review round, run
`python .\scripts\create_source_snapshot.py <ROUND_ID>`. It creates one unique
source-only ZIP in the CFR root named
`CFR_SOURCE_SNAPSHOT_<ROUND>_<YYYYMMDD-HHMMSS>.zip`.

Stage only the reviewed source roots: root source metadata, `src`, `scripts`,
`tests`, `docs`, and the explicit `m3_control` source/config files. Exclude
VCS metadata, `.tmp`/temporary paths, `node_modules`, `dist`, caches,
virtualenvs, `*.egg-info`, runtime databases, logs, binaries, archives,
generated output, and environment or secret configuration files.

The helper verifies those exclusions, nested archives, and portable POSIX (`/`)
ZIP entry names, then reports SHA-256. A snapshot is a source-review artifact,
not a runtime backup or release package.
