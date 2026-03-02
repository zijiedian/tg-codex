#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 not found"
  exit 1
fi

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi

VENV_PY="$SCRIPT_DIR/.venv/bin/python3"
if [[ ! -x "$VENV_PY" ]]; then
  echo "Detected broken virtualenv, recreating .venv"
  rm -rf "$SCRIPT_DIR/.venv"
  python3 -m venv "$SCRIPT_DIR/.venv"
fi

"$VENV_PY" -m pip install --upgrade pip >/dev/null
"$VENV_PY" -m pip install -r requirements.txt 'pyinstaller>=6.0'

"$VENV_PY" -m PyInstaller \
  --noconfirm \
  --clean \
  --onefile \
  --name tg-codex \
  --collect-submodules uvicorn \
  --collect-submodules telegram \
  --collect-submodules httpx \
  --collect-submodules httpcore \
  --collect-submodules anyio \
  --add-data ".env.example:." \
  cli.py

chmod +x "$SCRIPT_DIR/dist/tg-codex" || true

echo "Build complete: $SCRIPT_DIR/dist/tg-codex"
echo "Run: ./dist/tg-codex --token <TG_BOT_TOKEN> --port 18000"
