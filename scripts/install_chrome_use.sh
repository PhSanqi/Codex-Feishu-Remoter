#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="1.5.123"
SHA256="49e7a1fc94e2a99cc37f3ad6d7439d821b53de419bd2ae439e7ffca7a23d2310"
DEST="$ROOT/.local-tools/chrome-use"
ARCHIVE="$DEST/chrome-use-linux-x64.tar.gz"
URL="https://github.com/leeguooooo/chrome-use/releases/download/v${VERSION}/chrome-use-linux-x64.tar.gz"

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64|Linux-amd64) ;;
  *)
    echo "[CFR] chrome-use installer currently supports Linux x86_64 only." >&2
    exit 1
    ;;
esac

mkdir -p "$DEST"

curl_args=(--fail --location --retry 3 --retry-delay 2 --continue-at -)
if command -v gsettings >/dev/null 2>&1; then
  mode="$(gsettings get org.gnome.system.proxy mode 2>/dev/null | tr -d "'\"" || true)"
  if [[ "$mode" == "manual" ]]; then
    host="$(gsettings get org.gnome.system.proxy.socks host 2>/dev/null | tr -d "'\"" || true)"
    port="$(gsettings get org.gnome.system.proxy.socks port 2>/dev/null || true)"
    if [[ -n "$host" && "$port" =~ ^[0-9]+$ && "$port" -gt 0 ]]; then
      curl_args+=(--socks5-hostname "$host:$port")
    fi
  fi
fi

if [[ ! -x "$DEST/chrome-use" ]] || [[ "$($DEST/chrome-use --version 2>/dev/null | head -1)" != *"$VERSION"* ]]; then
  curl "${curl_args[@]}" -o "$ARCHIVE" "$URL"
  printf '%s  %s\n' "$SHA256" "$ARCHIVE" | sha256sum --check --status
  tar -xzf "$ARCHIVE" -C "$DEST"
  chmod +x "$DEST/chrome-use"
fi

"$DEST/chrome-use" --version

# Register the Native Messaging host only when it is missing.  Do not reopen
# Chrome Web Store on every bootstrap once the host/extension are configured.
status_json="$("$DEST/chrome-use" status --json 2>/dev/null || true)"
host_installed="$(python3 -c 'import json,sys
try:
    x=json.load(sys.stdin); print("1" if ((x.get("data") or {}).get("extension") or {}).get("hostInstalled") else "0")
except Exception:
    print("0")' <<<"$status_json")"
if [[ "$host_installed" != "1" ]]; then
  "$DEST/chrome-use" extension install --all-profiles >/dev/null 2>&1 || true
fi

status_json="$("$DEST/chrome-use" status --json 2>/dev/null || true)"
relay_up="$(python3 -c 'import json,sys
try:
    x=json.load(sys.stdin); print("1" if ((x.get("data") or {}).get("extension") or {}).get("relayUp") else "0")
except Exception:
    print("0")' <<<"$status_json")"

echo "[CFR] chrome-use CLI + Native Messaging host installed."
echo "[CFR] Chrome extension id: knfcmbamhjmaonkfnjhldjedeobeafmk"
if [[ "$relay_up" != "1" ]]; then
  echo "[CFR] Chrome extension is not connected yet. Install it once from:"
  echo "[CFR] https://chromewebstore.google.com/detail/chrome-use/knfcmbamhjmaonkfnjhldjedeobeafmk"
fi
