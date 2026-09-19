#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$ROOT/scripts/cfr_env.sh"

PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "CFR virtual environment is missing. Run scripts/bootstrap_linux.sh first." >&2
  exit 2
fi

INTERACTIVE=1
OPEN_BROWSER=1
for arg in "$@"; do
  [[ "$arg" == "--quiet" ]] && INTERACTIVE=0
  [[ "$arg" == "--open-browser" ]] && OPEN_BROWSER=0
done

SERVICE_WAS_ACTIVE=0
if [[ "$INTERACTIVE" == "1" ]] && command -v systemctl >/dev/null 2>&1; then
  if systemctl --user is-active --quiet cfr.service 2>/dev/null; then
    SERVICE_WAS_ACTIVE=1
    systemctl --user stop cfr.service
  fi
fi

restore_service() {
  if [[ "$SERVICE_WAS_ACTIVE" == "1" ]]; then
    systemctl --user start cfr.service >/dev/null 2>&1 || true
  fi
}
trap restore_service EXIT INT TERM

EXTRA_ARGS=()
if [[ "$INTERACTIVE" == "1" && "$OPEN_BROWSER" == "1" ]]; then
  EXTRA_ARGS+=(--open-browser)
fi

if [[ "$INTERACTIVE" == "0" ]]; then
  exec "$PYTHON" "$ROOT/scripts/run_cfr_control.py" \
    --port "$CFR_CONTROL_PORT" \
    --db "$CFR_DATABASE" \
    "$@"
fi

"$PYTHON" "$ROOT/scripts/run_cfr_control.py" \
  --port "$CFR_CONTROL_PORT" \
  --db "$CFR_DATABASE" \
  "${EXTRA_ARGS[@]}" \
  "$@"
