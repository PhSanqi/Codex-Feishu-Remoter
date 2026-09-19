#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"
TOOLS_DIR="$ROOT/.local-tools"
VENV_DIR="$ROOT/.venv"

echo "[CFR] root: $ROOT"
echo "[CFR] python: $($PYTHON_BIN --version 2>&1)"

if [[ -x "$VENV_DIR/bin/python" ]]; then
  echo "[CFR] reusing venv: $($VENV_DIR/bin/python --version 2>&1)"
elif command -v uv >/dev/null 2>&1; then
  # Resolve the requested interpreter before handing it to uv.  Passing a
  # generic name such as "python3" can otherwise select uv's managed default
  # instead of the host interpreter the operator explicitly requested.
  RESOLVED_PYTHON="$(command -v "$PYTHON_BIN")"
  uv venv "$VENV_DIR" --python "$RESOLVED_PYTHON"
else
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -c 'import sys; assert sys.version_info >= (3, 11), sys.version'
if command -v uv >/dev/null 2>&1; then
  uv pip install --python "$VENV_DIR/bin/python" -e '.[feishu]'
else
  "$VENV_DIR/bin/python" -m pip install --upgrade pip
  "$VENV_DIR/bin/python" -m pip install -e '.[feishu]'
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "[CFR] npm is required to install Codex and build Control Center." >&2
  exit 2
fi

mkdir -p "$TOOLS_DIR"
npm install --prefix "$TOOLS_DIR" --include=optional '@openai/codex@latest'

CODEX_PKG="$TOOLS_DIR/node_modules/@openai/codex/package.json"
if [[ -f "$CODEX_PKG" ]]; then
  case "$(uname -m)" in
    x86_64|amd64) CODEX_PLATFORM_PKG='@openai/codex-linux-x64' ;;
    aarch64|arm64) CODEX_PLATFORM_PKG='@openai/codex-linux-arm64' ;;
    *) CODEX_PLATFORM_PKG='' ;;
  esac
  if [[ -n "$CODEX_PLATFORM_PKG" ]]; then
    # npm occasionally omits Codex's optional platform payload on interrupted
    # or cached installs.  The platform names are aliases (for example
    # @openai/codex-linux-x64 -> npm:@openai/codex@<version>-linux-x64), not
    # separately published @latest packages. Read the exact alias from the
    # installed main package so CFR always installs a matching payload.
    CODEX_PLATFORM_SPEC="$(node -e "const p=require(process.argv[1]); process.stdout.write((p.optionalDependencies||{})[process.argv[2]]||'')" "$CODEX_PKG" "$CODEX_PLATFORM_PKG")"
    if [[ -n "$CODEX_PLATFORM_SPEC" ]]; then
      npm install --prefix "$TOOLS_DIR" "$CODEX_PLATFORM_PKG@$CODEX_PLATFORM_SPEC"
    fi
  fi
fi

if [[ ! -x "$TOOLS_DIR/node_modules/.bin/codex" ]]; then
  echo "[CFR] local Codex launcher was not installed." >&2
  exit 3
fi
"$TOOLS_DIR/node_modules/.bin/codex" --version

# Linux Chat uses chrome-use Extension + Native Messaging instead of CDP /
# Remote Debugging. The extension itself still requires one human approval.
"$ROOT/scripts/install_chrome_use.sh"

(
  cd m3_control
  npm ci
  npm run build
)

chmod +x \
  "$ROOT/START_CFR.sh" \
  "$ROOT/scripts/cfr_env.sh" \
  "$ROOT/scripts/bootstrap_linux.sh" \
  "$ROOT/scripts/install_chrome_use.sh" \
  "$ROOT/scripts/install_systemd_user.sh" \
  "$ROOT/scripts/linux_smoke.sh"

echo "[CFR] bootstrap complete"
echo "[CFR] start with: $ROOT/START_CFR.sh"
