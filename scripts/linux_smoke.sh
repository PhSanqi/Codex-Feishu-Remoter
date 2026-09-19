#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
source "$ROOT/scripts/cfr_env.sh"

PYTHON="$ROOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON=python3

echo '== platform =='
"$PYTHON" - <<'PY'
from cfr.platform import detect_platform_capabilities
print(detect_platform_capabilities())
PY

echo '== unit tests =='
PYTHONPATH=src "$PYTHON" -m unittest discover -s tests/unit -p 'test*.py'

echo '== shell syntax =='
bash -n START_CFR.sh scripts/cfr_env.sh scripts/bootstrap_linux.sh scripts/install_systemd_user.sh

echo '== Control UI =='
test -f m3_control/dist/index.html

echo '== Codex =='
"${CFR_CODEX_BIN:-codex}" --version

echo 'Linux smoke: PASS'
