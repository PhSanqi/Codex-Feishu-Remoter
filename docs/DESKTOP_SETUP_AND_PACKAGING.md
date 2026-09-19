# CFR Desktop EXE and first-run setup

## Target

CFR Desktop keeps the existing local Control API and React Control Center as the
single control plane. The Windows EXE is a desktop shell around that control
plane; it does not introduce a second backend or duplicate CFR execution logic.

The desktop window uses the system WebView2 runtime through pywebview. The local
API remains loopback-only and keeps the existing bootstrap session + CSRF model.
Runtime data is stored under the platform CFR config/data directory rather than
beside the EXE. One Windows top-level window owns the visible Control Center plus
a persistent hidden ChatGPT automation WebView2 child. Normal product UI does
not expose a ChatGPT page switcher or a second ChatGPT top-level window. CFR only
reveals the automation page temporarily when an interactive login is required.

## Startup contract

The EXE always starts the Control Center first. It starts Feishu automatically
only when every required setup check is ready.

Required for both Code and Chat:

- Codex executable is discoverable.
- Codex login status is confirmed.
- The installed Codex payload has passed CFR's compatibility doctor. Validation
  is bound to the generated app-server schema fingerprint and installed runtime
  content revision, so a same-version Codex payload update also invalidates the
  previous validation.
- Feishu Channel SDK is present in the packaged runtime.
- A user-owned Feishu App ID and App Secret are configured.
- At least one Feishu operator is paired.
- At least one allowed workspace exists and every configured workspace path is
  still valid.
- The selected network policy is valid.

Additional requirements when the default Surface is Chat depend on the selected
browser backend:

- `npx` is required for the pinned Chrome DevTools MCP bridge in both modes.
- `embedded`: the hidden ChatGPT WebView2 automation page must complete one
  interactive login. CFR temporarily reveals it only while login is required.
  Google Chrome is not required for this backend.
- `dedicated`: Google Chrome must be available and the retained CFR-dedicated
  Chrome profile must have completed interactive login.
- `auto`: CFR Desktop uses the embedded ChatGPT page whenever that page is
  available. A retained dedicated-Chrome login is preserved but never selected
  merely because it already exists. External Chrome is started only when the
  user explicitly selects `dedicated`.

CFR never reads or migrates ChatGPT credentials. Embedded WebView2 state lives
under the CFR WebView2 user-data folder, while the older dedicated Chrome profile
remains separate and is not deleted or rewritten during migration.

## Persistent machine configuration

Non-secret configuration is stored in the platform CFR `config.json`:

- default Surface: `code` or `chat`;
- Chat browser backend: `auto`, `embedded`, or `dedicated`;
- Codex Desktop launcher preference: `auto`, `codexhost`, or `stock`;
- network policy: `auto`, `direct`, or fixed `proxy`;
- Feishu App ID;
- paired Feishu operator IDs;
- allowed workspace roots;
- Codex compatibility validation metadata.

The Feishu App Secret is stored only in the OS keyring. It is never written to
`config.json`, the repository, logs, or the packaged EXE.

`auto` network mode preserves the current portable behavior: explicit
`HTTP_PROXY` / `HTTPS_PROXY` environment variables win, otherwise CFR uses the
machine's detected fixed proxy or direct route. `direct` and `proxy` are explicit
per-machine overrides saved by the user.

## Git / new machine behavior

The repository intentionally does not contain machine login state. A fresh clone
or installed EXE on another machine therefore enters the same setup/readiness
screen. The user configures that machine once; subsequent launches read the local
saved state.

Do not commit or migrate as source state:

- CFR SQLite runtime database;
- `~/.codex` authentication/history state;
- ChatGPT Chrome profile;
- Feishu App Secret;
- local proxy credentials.

## Development run

Build the web UI first:

```text
cd m3_control
npm.cmd run build
```

Then install desktop + Feishu extras and launch:

```text
python -m pip install -e ".[desktop,feishu]"
python scripts\run_cfr_desktop.py
```

The browser launcher remains available for development:

```text
python scripts\run_cfr_control.py --open-browser
```

It now follows the same setup gate: incomplete machines open the Control Center
without automatically starting Feishu or Chat runtime components.

## Build one Windows EXE

Run:

```text
powershell -ExecutionPolicy Bypass -File scripts\build_windows_exe.ps1
```

The script creates an isolated build virtual environment under `.tmp`, installs
the declared Python runtime/build dependencies there, installs the locked
Control Center npm dependencies when `node_modules` is absent, always rebuilds
the React production assets from source, bundles those assets plus the pinned
CFR reference contract, and produces:

```text
desktop_dist\CFR.exe
```

`desktop_dist` and PyInstaller working data are local build artifacts and are not
committed.

## Setup UI responsibilities

The `启动配置` page is both the first-run wizard and the later settings surface.
It exposes the same persisted state after installation, so users can return to it
to change:

- default Code / Chat Surface;
- Chat browser backend without deleting the retained Chrome profile;
- Codex Desktop restart source, including CodexHost-preserving `auto` mode;
- network mode / fixed proxy;
- Feishu App credentials;
- Feishu operator pairing;
- allowed workspace roots;
- Codex login and compatibility validation;
- ChatGPT login for the selected Chat browser backend.

The normal `运行` page is disabled while required setup checks are incomplete.

The Windows shell creates one top-level CFR window. The Control Center remains
user-facing while a second WebView2 child stays alive but hidden for Chat
automation. This removes human/automation contention over the same visible
ChatGPT DOM. The UI defaults to the light theme; an explicitly saved dark-theme
preference is still respected.
