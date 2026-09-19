#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="$HOME/.config/systemd/user/cfr.service"
mkdir -p "$(dirname "$TARGET")"
cp "$ROOT/deploy/systemd/cfr.service" "$TARGET"
systemctl --user daemon-reload
systemctl --user enable --now cfr.service
systemctl --user --no-pager --full status cfr.service
