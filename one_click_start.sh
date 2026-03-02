#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$SCRIPT_DIR/dist/tg-codex"
ENV_FILE="$SCRIPT_DIR/.env"
EXAMPLE_FILE="$SCRIPT_DIR/.env.example"

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$EXAMPLE_FILE" "$ENV_FILE"
  chmod 600 "$ENV_FILE" || true
  echo "Created $ENV_FILE from template."
  echo "Please edit .env first, then rerun ./one_click_start.sh"
  exit 1
fi

if [[ ! -x "$BIN" ]]; then
  echo "Binary not found, building first..."
  "$SCRIPT_DIR/build_binary.sh"
fi

exec "$BIN" start "$@"
