#!/usr/bin/env bash
set -euo pipefail

SCRIPT_SOURCE="${BASH_SOURCE[0]:-$0}"
SCRIPT_DIR="$(cd -- "$(dirname -- "$SCRIPT_SOURCE")" >/dev/null 2>&1 && pwd -P)"
BIN="$SCRIPT_DIR/dist/tg-codex"
ENV_FILE="$SCRIPT_DIR/.env"
EXAMPLE_FILE="$SCRIPT_DIR/.env.example"

TOKEN="${TG_BOT_TOKEN:-}"
USE_RELOAD_MODE=0
POSITIONAL_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --token)
      if [[ $# -lt 2 ]]; then
        echo "Error: --token requires a value" >&2
        exit 1
      fi
      TOKEN="$2"
      shift 2
      ;;
    --reload)
      USE_RELOAD_MODE=1
      POSITIONAL_ARGS+=("$1")
      shift
      ;;
    *)
      POSITIONAL_ARGS+=("$1")
      shift
      ;;
  esac
done

if [[ ! -f "$ENV_FILE" && -z "$TOKEN" ]]; then
  cp "$EXAMPLE_FILE" "$ENV_FILE"
  chmod 600 "$ENV_FILE" || true
  echo "Created $ENV_FILE from template."
  echo "Now run: ./start.sh --token <TG_BOT_TOKEN>"
  exit 1
fi

if [[ "$USE_RELOAD_MODE" -eq 1 ]]; then
  if [[ -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
    PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python)"
  else
    echo "Error: Python not found. Install Python 3 to use --reload." >&2
    exit 1
  fi
  EXEC_CMD=("$PYTHON_BIN" "$SCRIPT_DIR/cli.py")
  echo "Reload mode enabled: running Python mode with auto-restart."
else
  if [[ ! -x "$BIN" ]]; then
    echo "Binary not found, building first..."
    "$SCRIPT_DIR/build_binary.sh"
  fi
  EXEC_CMD=("$BIN")
fi

if [[ -n "$TOKEN" ]]; then
  EXEC_CMD+=("--token" "$TOKEN")
fi
if [[ ${#POSITIONAL_ARGS[@]} -gt 0 ]]; then
  EXEC_CMD+=("${POSITIONAL_ARGS[@]}")
fi

exec "${EXEC_CMD[@]}"
