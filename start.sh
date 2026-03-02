#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[info] start.sh has been simplified. Prefer one-click mode: ./one_click_start.sh --token <TG_BOT_TOKEN>"
exec "$SCRIPT_DIR/one_click_start.sh" "$@"
