# tg-codex

`tg-codex` is a Telegram -> Codex CLI bridge service based on FastAPI + python-telegram-bot.

## Features

- Execute Codex tasks from Telegram (`/run <prompt>`)
- Stream running output by editing one Telegram message
- Detect and render patch/diff output more clearly
- Support image input (photo/document image)
- Persist and auto-resume Codex session per chat
- Upload full final output as `codex-output-*.txt`

## Security Defaults

- Chat/user allowlist required (`TG_ALLOWED_CHAT_IDS`, `TG_ALLOWED_USER_IDS`)
- Optional second-factor auth via `/auth` (`TG_AUTH_PASSPHRASE`)
- Admin-only runtime command override (`/cmd`)
- Command prefix validation blocks dangerous flags

## Quick Start

```bash
./start.sh --token <TG_BOT_TOKEN> --chat-id <TG_CHAT_ID> --user-id <TG_USER_ID>
```

`start.sh` will:

1. Load/create `.env`
2. Create `.venv` if missing
3. Install dependencies
4. Start service (long polling by default)

## Manual Start

```bash
cp .env.example .env
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Important Environment Variables

- `TG_BOT_TOKEN` (required)
- `TG_ALLOWED_CHAT_IDS` (required)
- `TG_ALLOWED_USER_IDS` (required)
- `TG_ADMIN_CHAT_IDS` / `TG_ADMIN_USER_IDS` (optional, default to allowlist)
- `CODEX_COMMAND_PREFIX` (default: `codex -a never exec --full-auto`)
- `CODEX_TIMEOUT_SECONDS`
- `TG_MAX_CONCURRENT_TASKS`
- `TG_MAX_BUFFERED_OUTPUT_CHARS`
- `TG_AUTH_PASSPHRASE` / `TG_AUTH_TTL_SECONDS`

## Telegram Commands

- `/start`
- `/id`
- `/run <prompt>`
- `/status`
- `/cancel`
- `/auth <passphrase>`
- `/cmd` / `/cmd <prefix>` / `/cmd reset`

## Sensitive Data Hygiene

This repository is configured to ignore runtime/sensitive artifacts, including:

- `.env`
- `.venv/`
- `chat_sessions.json`
- `outputs/`
- `incoming_media/`
- runtime logs/cache

Do **not** commit real bot tokens, webhook secrets, or production chat/user IDs.

## License

MIT License. See `LICENSE`.
