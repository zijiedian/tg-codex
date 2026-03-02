#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$SCRIPT_DIR/dist/tg-codex"
ENV_FILE="$SCRIPT_DIR/.env"
EXAMPLE_FILE="$SCRIPT_DIR/.env.example"

TOKEN="${TG_BOT_TOKEN:-}"
START_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --token)
      TOKEN="${2:-}"
      shift 2
      ;;
    *)
      START_ARGS+=("$1")
      shift
      ;;
  esac
done

if [[ ! -x "$BIN" ]]; then
  echo "Binary not found, building first..."
  "$SCRIPT_DIR/build_binary.sh"
fi

if [[ ! -f "$ENV_FILE" ]]; then
  if [[ -z "$TOKEN" ]]; then
    cp "$EXAMPLE_FILE" "$ENV_FILE"
    chmod 600 "$ENV_FILE" || true
    echo "Created $ENV_FILE from template."
    echo "Now run: ./one_click_start.sh --token <TG_BOT_TOKEN>"
    exit 1
  fi
fi

if [[ -n "$TOKEN" ]]; then
  exec "$BIN" --token "$TOKEN" "${START_ARGS[@]}"
fi
exec "$BIN" "${START_ARGS[@]}"
