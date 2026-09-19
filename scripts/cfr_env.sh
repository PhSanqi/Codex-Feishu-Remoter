#!/usr/bin/env bash
# Shared Linux runtime environment for CFR source deployments.

set -o errexit
set -o nounset
set -o pipefail

CFR_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export CFR_ROOT

if [[ -d "$CFR_ROOT/.local-tools/node_modules/.bin" ]]; then
  export PATH="$CFR_ROOT/.local-tools/node_modules/.bin:$PATH"
fi

if [[ -x "$CFR_ROOT/.local-tools/node_modules/.bin/codex" ]]; then
  export CFR_CODEX_BIN="${CFR_CODEX_BIN:-$CFR_ROOT/.local-tools/node_modules/.bin/codex}"
fi

# DevSpace Control already owns 127.0.0.1:8787 on this Linux host. Keep the
# CFR source runtime on its own local-only port so both control planes can
# coexist without startup races.
export CFR_CONTROL_PORT="${CFR_CONTROL_PORT:-18787}"
export CFR_DATABASE="${CFR_DATABASE:-cfr.sqlite3}"
export PYTHONUNBUFFERED=1
