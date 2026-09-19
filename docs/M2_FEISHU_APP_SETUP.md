# Feishu App Setup for CFR M2

This document contains placeholders only. Never paste a real App Secret into source, logs, artifacts, or a Codex conversation.

## Local preparation

Install the optional SDK on the Host that will run the daemon:

```powershell
python -m pip install -e ".[feishu]"
```

Conversational transport uses the official `lark-channel-sdk` (`lark_channel.FeishuChannel`, stable `1.x`). `lark-oapi` remains an optional companion for other OpenAPI calls and is not the live conversational transport. Check installed metadata without contacting Feishu:

```powershell
python .\scripts\run_m2_feishu_sdk_probe.py
```

The probe is a Host gate: `Verdict=PASS` exits `0`; missing `lark-channel-sdk` or a required API reports `Verdict=FAIL` and exits `2`. Do not continue to the real Codex or M1 Host gates after a probe failure.

After the SDK probe passes and credentials are set locally, `feishu doctor --live` performs a real Channel readiness check and disconnects without creating a Codex daemon. The normal `feishu run` command remains a long-running daemon after readiness.

Set credentials and local authorization in the current user-managed PowerShell session. Environment values are an override; persist the rotated pair into the OS secure store with `import-env` before clearing the environment:

```powershell
$env:CFR_FEISHU_APP_ID="cli_xxx"
$env:CFR_FEISHU_APP_SECRET="<APP_SECRET>"
$env:CFR_FEISHU_ALLOWED_OPEN_IDS="ou_xxx"
$env:CFR_FEISHU_ALLOWED_WORKSPACE_ROOTS="C:\Projects"
$env:CFR_FEISHU_ENABLE_GROUPS="false"
python .\scripts\run_cfr.py feishu credentials import-env
python .\scripts\run_cfr.py feishu credentials status
```

`credentials status` never returns the App Secret. Do not use `setx` or a plaintext file for secrets in this milestone. After clearing the two credential environment variables, verify the fresh-process path with `python .\scripts\run_feishu_credential_store_probe.py --live`.

## Developer Console checklist

1. Create or select an enterprise self-built Feishu app.
2. Enable bot capability and publish an app version.
3. Configure event subscriptions to use the official v2 long connection.
4. Subscribe to `im.message.receive_v1`.
5. Configure card callbacks and subscribe to `card.action.trigger` when ApprovalBridge is enabled.
6. Request only the message scopes required by the deployment; do not request group-wide history unless needed.
7. Obtain the operator's `open_id` and add it to the allowlist.
8. Add only existing local workspace roots to the workspace allowlist.

## Connection sequence

```powershell
cd '<CFR_PROJECT_ROOT>'
python .\scripts\run_cfr.py feishu doctor
python .\scripts\run_cfr.py feishu run --setup-only --show-identifiers
python .\scripts\run_cfr.py feishu run
```

`--setup-only` establishes the long-connection process boundary and reports readiness; it does not create a Codex turn or write a project. Ctrl+C stops the local process. M2 does not install a Windows service, scheduled task, or startup hook.

If the SDK extra or credentials are absent, the safe result is `FEISHU_SDK_NOT_INSTALLED` or `FEISHU_LIVE_SETUP_REQUIRED`; it is not a live PASS.
