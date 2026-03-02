#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load key/value pairs from .env without executing it as shell code.
load_dotenv_fallback() {
  local env_file="$1"
  local line=""
  local key=""
  local value=""
  [[ -f "$env_file" ]] || return 0

  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" == *=* ]] || continue

    key="${line%%=*}"
    value="${line#*=}"
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    [[ "$key" =~ ^[A-Z0-9_]+$ ]] || continue

    # Keep external env vars as higher priority; use .env only as fallback.
    if [[ -z "${!key:-}" ]]; then
      if [[ "$value" =~ ^\".*\"$ ]]; then
        value="${value:1:${#value}-2}"
      elif [[ "$value" =~ ^\'.*\'$ ]]; then
        value="${value:1:${#value}-2}"
      fi
      printf -v "$key" '%s' "$value"
      export "$key"
    fi
  done < "$env_file"
}

load_dotenv_fallback "$SCRIPT_DIR/.env"

TOKEN="${TG_BOT_TOKEN:-}"
CHAT_IDS="${TG_ALLOWED_CHAT_IDS:-}"
USER_IDS="${TG_ALLOWED_USER_IDS:-}"
ADMIN_CHAT_IDS="${TG_ADMIN_CHAT_IDS:-}"
ADMIN_USER_IDS="${TG_ADMIN_USER_IDS:-}"
WEBHOOK_URL="${TG_WEBHOOK_URL:-}"
WEBHOOK_SECRET="${TG_WEBHOOK_SECRET:-}"
CODEX_PREFIX="${CODEX_COMMAND_PREFIX:-codex -a never exec --full-auto}"
TIMEOUT="${CODEX_TIMEOUT_SECONDS:-600}"
ALLOW_PLAIN_TEXT="${TG_ALLOW_PLAIN_TEXT:-0}"
ALLOW_CMD_OVERRIDE="${TG_ALLOW_CMD_OVERRIDE:-0}"
MAX_IMAGE_BYTES="${TG_MAX_IMAGE_BYTES:-10485760}"
MAX_BUFFERED_OUTPUT_CHARS="${TG_MAX_BUFFERED_OUTPUT_CHARS:-200000}"
MAX_CONCURRENT_TASKS="${TG_MAX_CONCURRENT_TASKS:-2}"
AUTH_PASSPHRASE="${TG_AUTH_PASSPHRASE:-}"
AUTH_TTL_SECONDS="${TG_AUTH_TTL_SECONDS:-43200}"
PORT="${PORT:-8000}"
RELOAD="${UVICORN_RELOAD:-0}"

usage() {
  cat <<EOF
Usage:
  ./start.sh --token <bot_token> [options]

Options:
  --token <value>           Telegram bot token (required)
  --chat-id <value>         Single chat id or comma-separated allowlist (required)
  --user-id <value>         Allowed Telegram user ids (default: same as --chat-id)
  --admin-chat-id <value>   Admin chat ids for /cmd (default: same as --chat-id)
  --admin-user-id <value>   Admin user ids for /cmd (default: same as --user-id)
  --webhook-url <value>     Webhook URL (optional; empty = long polling)
  --webhook-secret <value>  Webhook secret (optional)
  --codex-prefix <value>    Codex command prefix
  --timeout <seconds>       Codex timeout in seconds (default: 600)
  --allow-plain-text <0|1>  Treat plain text as prompt (default: 0)
  --allow-cmd-override <0|1>Allow /cmd to modify command prefix (default: 0)
  --max-image-bytes <n>     Max image upload size in bytes (default: 10485760)
  --max-buffered-output-chars <n> Max in-memory output chars (default: 200000)
  --max-concurrent-tasks <n> Max concurrent tasks across chats (default: 2)
  --auth-passphrase <value> Enable /auth second-factor with this passphrase
  --auth-ttl <seconds>      /auth validity window (default: 43200)
  --port <port>             Uvicorn port (default: 8000)
  --reload                  Auto-reload when code changes (dev only)
  --no-reload               Disable auto-reload (default)
  -h, --help                Show help

Example:
  ./start.sh --token 123456:ABCDEF --chat-id 12345678
EOF
}

normalize_option_token() {
  local token="$1"
  local em_dash=$'\342\200\224'
  local en_dash=$'\342\200\223'
  local minus_sign=$'\342\210\222'
  case "$token" in
    "${em_dash}"*) printf -- '--%s' "${token#"$em_dash"}" ;;
    "${en_dash}"*) printf -- '--%s' "${token#"$en_dash"}" ;;
    "${minus_sign}"*) printf -- '-%s' "${token#"$minus_sign"}" ;;
    *) printf -- '%s' "$token" ;;
  esac
}

while [[ $# -gt 0 ]]; do
  arg="$(normalize_option_token "$1")"
  case "$arg" in
    --token)
      TOKEN="${2:-}"
      shift 2
      ;;
    --chat-id)
      CHAT_IDS="${2:-}"
      shift 2
      ;;
    --user-id)
      USER_IDS="${2:-}"
      shift 2
      ;;
    --admin-chat-id)
      ADMIN_CHAT_IDS="${2:-}"
      shift 2
      ;;
    --admin-user-id)
      ADMIN_USER_IDS="${2:-}"
      shift 2
      ;;
    --webhook-url)
      WEBHOOK_URL="${2:-}"
      shift 2
      ;;
    --webhook-secret)
      WEBHOOK_SECRET="${2:-}"
      shift 2
      ;;
    --codex-prefix)
      CODEX_PREFIX="${2:-}"
      shift 2
      ;;
    --timeout)
      TIMEOUT="${2:-}"
      shift 2
      ;;
    --allow-plain-text)
      ALLOW_PLAIN_TEXT="${2:-}"
      shift 2
      ;;
    --allow-cmd-override)
      ALLOW_CMD_OVERRIDE="${2:-}"
      shift 2
      ;;
    --max-image-bytes)
      MAX_IMAGE_BYTES="${2:-}"
      shift 2
      ;;
    --max-buffered-output-chars)
      MAX_BUFFERED_OUTPUT_CHARS="${2:-}"
      shift 2
      ;;
    --max-concurrent-tasks)
      MAX_CONCURRENT_TASKS="${2:-}"
      shift 2
      ;;
    --auth-passphrase)
      AUTH_PASSPHRASE="${2:-}"
      shift 2
      ;;
    --auth-ttl)
      AUTH_TTL_SECONDS="${2:-}"
      shift 2
      ;;
    --port)
      PORT="${2:-}"
      shift 2
      ;;
    --reload)
      RELOAD="1"
      shift 1
      ;;
    --no-reload)
      RELOAD="0"
      shift 1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      usage
      exit 1
      ;;
  esac
done

if [[ -z "$TOKEN" ]]; then
  echo "Error: --token is required"
  usage
  exit 1
fi

if [[ -z "$CHAT_IDS" ]]; then
  echo "Error: --chat-id is required for security"
  usage
  exit 1
fi

if [[ -z "$ADMIN_CHAT_IDS" ]]; then
  ADMIN_CHAT_IDS="$CHAT_IDS"
fi

if [[ -z "$USER_IDS" ]]; then
  USER_IDS="$CHAT_IDS"
fi

if [[ -z "$ADMIN_USER_IDS" ]]; then
  ADMIN_USER_IDS="$USER_IDS"
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 not found"
  exit 1
fi

if ! command -v codex >/dev/null 2>&1; then
  echo "Error: codex CLI not found"
  exit 1
fi

cd "$SCRIPT_DIR"

if [[ -z "$WEBHOOK_SECRET" ]]; then
  WEBHOOK_SECRET="$(python3 - <<PY
import secrets
print(secrets.token_urlsafe(24))
PY
)"
fi

cat > .env <<EOF
TG_BOT_TOKEN=$TOKEN
TG_WEBHOOK_URL=$WEBHOOK_URL
TG_WEBHOOK_SECRET=$WEBHOOK_SECRET
TG_ALLOWED_CHAT_IDS=$CHAT_IDS
TG_ALLOWED_USER_IDS=$USER_IDS
TG_ADMIN_CHAT_IDS=$ADMIN_CHAT_IDS
TG_ADMIN_USER_IDS=$ADMIN_USER_IDS
CODEX_COMMAND_PREFIX=$CODEX_PREFIX
CODEX_TIMEOUT_SECONDS=$TIMEOUT
TG_ALLOW_PLAIN_TEXT=$ALLOW_PLAIN_TEXT
TG_ALLOW_CMD_OVERRIDE=$ALLOW_CMD_OVERRIDE
TG_MAX_IMAGE_BYTES=$MAX_IMAGE_BYTES
TG_MAX_BUFFERED_OUTPUT_CHARS=$MAX_BUFFERED_OUTPUT_CHARS
TG_MAX_CONCURRENT_TASKS=$MAX_CONCURRENT_TASKS
TG_AUTH_PASSPHRASE=$AUTH_PASSPHRASE
TG_AUTH_TTL_SECONDS=$AUTH_TTL_SECONDS
EOF
chmod 600 .env || true

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
"$VENV_PY" -m pip install -r requirements.txt

echo "tg-codex is starting on port $PORT"
if [[ -n "$WEBHOOK_URL" ]]; then
  echo "Mode: webhook"
else
  echo "Mode: long polling"
fi
if [[ "$RELOAD" == "1" ]]; then
  echo "Reload: enabled"
else
  echo "Reload: disabled"
fi

if [[ "$RELOAD" == "1" ]]; then
  exec "$VENV_PY" -m uvicorn main:app --host 0.0.0.0 --port "$PORT" --reload
else
  exec "$VENV_PY" -m uvicorn main:app --host 0.0.0.0 --port "$PORT"
fi
