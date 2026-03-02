# tg-codex

`tg-codex` is a Telegram -> Codex CLI bridge service built with FastAPI + python-telegram-bot.

## What It Does

- Run Codex tasks from Telegram (`/run <prompt>`)
- Stream output in-place by editing one Telegram message
- Better diff/patch rendering for file updates
- Support image input (photo/document image)
- Auto-resume one Codex session per chat
- Always upload full final output as `codex-output-*.txt`

## One-Click Start (Recommended)

1. First-time build and run:

```bash
./one_click_start.sh
```

- If `.env` is missing, it auto-creates from `.env.example` and asks you to fill it.
- If binary is missing, it auto-builds `dist/tg-codex`.
- Then it starts the service directly.

2. Next runs:

```bash
./one_click_start.sh
```

## Binary Build

Build standalone binary:

```bash
./build_binary.sh
```

Output:

- `dist/tg-codex`

Run binary directly:

```bash
./dist/tg-codex start --host 0.0.0.0 --port 8000
```

Initialize/update `.env` via binary (optional):

```bash
./dist/tg-codex init --token <TG_BOT_TOKEN> --chat-id <CHAT_ID> --user-id <USER_ID>
```

## Automated GitHub Release

This repo includes GitHub Actions workflow:

- `.github/workflows/release.yml`

How to trigger:

1. Push a tag like `v1.0.0`:

```bash
git tag v1.0.0
git push origin v1.0.0
```

2. Or run workflow manually from **Actions -> Build And Release** with `tag`.

Workflow output:

- Build binaries for macOS / Linux / Windows
- Package archives and generate `SHA256SUMS.txt`
- Create GitHub Release and upload downloadable assets

## Python Mode (No Binary)

```bash
cp .env.example .env
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python cli.py start --host 0.0.0.0 --port 8000
```

## Key Environment Variables

- `TG_BOT_TOKEN` (required)
- `TG_ALLOWED_CHAT_IDS` (required)
- `TG_ALLOWED_USER_IDS` (required)
- `TG_ADMIN_CHAT_IDS` / `TG_ADMIN_USER_IDS` (optional; default to allowlist)
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

Ignored by git:

- `.env`
- `.venv/`
- `build/`, `dist/`, `*.spec`
- `chat_sessions.json`
- `outputs/`
- `incoming_media/`
- runtime logs/cache files

Never commit real bot tokens, webhook secrets, or production chat/user IDs.

## License

MIT License. See `LICENSE`.
