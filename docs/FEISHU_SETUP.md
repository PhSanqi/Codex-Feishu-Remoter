# Feishu App Setup

CFR uses a **user-owned Feishu custom app**. CFR does not operate a shared
multi-tenant Feishu backend and does not automate the Feishu developer console.

## 1. Create the app

In Feishu Open Platform:

1. Create an enterprise self-built/custom app.
2. Enable bot capability.
3. Configure the supported long-connection/event mode.
4. Subscribe to `im.message.receive_v1`.
5. If approval cards are used, enable the required card callback and subscribe
   to `card.action.trigger`.
6. Grant only the message/card scopes required by this deployment.
7. Publish or approve the app as required by the tenant.

Do not request broad message-history or group permissions unless the deployment
actually needs them.

## 2. Obtain credentials

Copy the App ID and App Secret from the Feishu developer console.

The App Secret must never be committed to Git, pasted into project files, or
stored in a plaintext `.env` file for normal use.

CFR's credential contract is:

- App ID: local non-secret CFR configuration;
- App Secret: operating-system secret store;
- environment variables: temporary import/CI/debug override only.

The Control Center can be used for normal setup. A command-line import path also
exists for a temporary PowerShell session:

```powershell
$env:CFR_FEISHU_APP_ID="cli_xxx"
$env:CFR_FEISHU_APP_SECRET="<APP_SECRET>"
python .\scripts\run_cfr.py feishu credentials import-env
Remove-Item Env:CFR_FEISHU_APP_ID
Remove-Item Env:CFR_FEISHU_APP_SECRET
python .\scripts\run_cfr.py feishu credentials status
```

`credentials status` never returns the App Secret.

## 3. Bind an operator

Current releases support the six-digit private-chat pairing flow from the
Control Center:

1. Choose **Bind my Feishu account**.
2. Send the displayed `绑定 <code>` message to the CFR bot in a private chat.
3. Confirm the detected account in the Control Center.

The resulting operator identity is stored only as local authorization state.

A future QR/OAuth flow may replace this pairing UX, but it does not replace the
need to create and authorize the Feishu app first.

## 4. Select allowed workspaces

Add only local directories that CFR is allowed to use for Codex work. Paths are
canonicalized and checked against the allowlist; paths outside the configured
roots fail closed.

## 5. Start the Feishu runtime

From the Control Center, start Feishu after credentials, operator binding, and
workspace roots are configured.

For diagnostics only:

```powershell
python .\scripts\run_cfr.py feishu doctor
python .\scripts\run_feishu_sdk_probe.py
python .\scripts\run_cfr.py feishu run --setup-only --show-identifiers
```
