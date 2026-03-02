from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

import uvicorn

from app_factory import build_app
from settings import load_settings, runtime_base_dir

ENV_KEYS = [
    "TG_BOT_TOKEN",
    "TG_WEBHOOK_URL",
    "TG_WEBHOOK_SECRET",
    "TG_ALLOWED_CHAT_IDS",
    "TG_ALLOWED_USER_IDS",
    "TG_ADMIN_CHAT_IDS",
    "TG_ADMIN_USER_IDS",
    "CODEX_COMMAND_PREFIX",
    "CODEX_TIMEOUT_SECONDS",
    "TG_ALLOW_PLAIN_TEXT",
    "TG_ALLOW_CMD_OVERRIDE",
    "TG_MAX_IMAGE_BYTES",
    "TG_MAX_BUFFERED_OUTPUT_CHARS",
    "TG_MAX_CONCURRENT_TASKS",
    "TG_AUTH_PASSPHRASE",
    "TG_AUTH_TTL_SECONDS",
]


def _env_path() -> Path:
    return runtime_base_dir() / ".env"


def _load_existing_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = value.strip()
    return values


def _pick(existing: dict[str, str], key: str, override: str | None, default: str) -> str:
    if override is not None and override != "":
        return override
    if key in existing and existing[key] != "":
        return existing[key]
    return default


def _write_env(path: Path, payload: dict[str, str]) -> None:
    lines = [f"{key}={payload.get(key, )}" for key in ENV_KEYS]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def init_env(args: argparse.Namespace) -> int:
    env_path = _env_path()
    existing = _load_existing_env(env_path)

    token = _pick(existing, "TG_BOT_TOKEN", args.token, "")
    chat_ids = _pick(existing, "TG_ALLOWED_CHAT_IDS", args.chat_id, "")

    if not token:
        raise SystemExit("TG_BOT_TOKEN is required, set --token")
    if not chat_ids:
        raise SystemExit("TG_ALLOWED_CHAT_IDS is required, set --chat-id")

    user_ids = _pick(existing, "TG_ALLOWED_USER_IDS", args.user_id, chat_ids)
    admin_chat_ids = _pick(existing, "TG_ADMIN_CHAT_IDS", args.admin_chat_id, chat_ids)
    admin_user_ids = _pick(existing, "TG_ADMIN_USER_IDS", args.admin_user_id, user_ids)

    webhook_url = _pick(existing, "TG_WEBHOOK_URL", args.webhook_url, "")
    webhook_secret = _pick(existing, "TG_WEBHOOK_SECRET", args.webhook_secret, "")
    if webhook_url and not webhook_secret:
        webhook_secret = secrets.token_urlsafe(24)

    payload = {
        "TG_BOT_TOKEN": token,
        "TG_WEBHOOK_URL": webhook_url,
        "TG_WEBHOOK_SECRET": webhook_secret,
        "TG_ALLOWED_CHAT_IDS": chat_ids,
        "TG_ALLOWED_USER_IDS": user_ids,
        "TG_ADMIN_CHAT_IDS": admin_chat_ids,
        "TG_ADMIN_USER_IDS": admin_user_ids,
        "CODEX_COMMAND_PREFIX": _pick(
            existing,
            "CODEX_COMMAND_PREFIX",
            args.codex_prefix,
            "codex -a never exec --full-auto",
        ),
        "CODEX_TIMEOUT_SECONDS": _pick(existing, "CODEX_TIMEOUT_SECONDS", args.timeout, "600"),
        "TG_ALLOW_PLAIN_TEXT": _pick(existing, "TG_ALLOW_PLAIN_TEXT", args.allow_plain_text, "0"),
        "TG_ALLOW_CMD_OVERRIDE": _pick(existing, "TG_ALLOW_CMD_OVERRIDE", args.allow_cmd_override, "0"),
        "TG_MAX_IMAGE_BYTES": _pick(existing, "TG_MAX_IMAGE_BYTES", args.max_image_bytes, "10485760"),
        "TG_MAX_BUFFERED_OUTPUT_CHARS": _pick(
            existing,
            "TG_MAX_BUFFERED_OUTPUT_CHARS",
            args.max_buffered_output_chars,
            "200000",
        ),
        "TG_MAX_CONCURRENT_TASKS": _pick(existing, "TG_MAX_CONCURRENT_TASKS", args.max_concurrent_tasks, "2"),
        "TG_AUTH_PASSPHRASE": _pick(existing, "TG_AUTH_PASSPHRASE", args.auth_passphrase, ""),
        "TG_AUTH_TTL_SECONDS": _pick(existing, "TG_AUTH_TTL_SECONDS", args.auth_ttl, "43200"),
    }

    _write_env(env_path, payload)
    print(f"Wrote config: {env_path}")
    print("Tip: run `tg-codex start` (or `./one_click_start.sh`) to launch.")
    return 0


def start_service(args: argparse.Namespace) -> int:
    if getattr(sys, "frozen", False) and args.reload:
        print("reload is not supported in frozen binary mode, forcing --no-reload")
        args.reload = False

    settings = load_settings()
    app, _ = build_app(settings)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tg-codex", description="Telegram to Codex bridge")
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="initialize or update .env")
    init_parser.add_argument("--token", help="TG_BOT_TOKEN")
    init_parser.add_argument("--chat-id", help="TG_ALLOWED_CHAT_IDS, comma-separated")
    init_parser.add_argument("--user-id", help="TG_ALLOWED_USER_IDS, comma-separated")
    init_parser.add_argument("--admin-chat-id", help="TG_ADMIN_CHAT_IDS, comma-separated")
    init_parser.add_argument("--admin-user-id", help="TG_ADMIN_USER_IDS, comma-separated")
    init_parser.add_argument("--webhook-url", help="TG_WEBHOOK_URL")
    init_parser.add_argument("--webhook-secret", help="TG_WEBHOOK_SECRET")
    init_parser.add_argument("--codex-prefix", help="CODEX_COMMAND_PREFIX")
    init_parser.add_argument("--timeout", help="CODEX_TIMEOUT_SECONDS")
    init_parser.add_argument("--allow-plain-text", choices=["0", "1"], help="TG_ALLOW_PLAIN_TEXT")
    init_parser.add_argument("--allow-cmd-override", choices=["0", "1"], help="TG_ALLOW_CMD_OVERRIDE")
    init_parser.add_argument("--max-image-bytes", help="TG_MAX_IMAGE_BYTES")
    init_parser.add_argument("--max-buffered-output-chars", help="TG_MAX_BUFFERED_OUTPUT_CHARS")
    init_parser.add_argument("--max-concurrent-tasks", help="TG_MAX_CONCURRENT_TASKS")
    init_parser.add_argument("--auth-passphrase", help="TG_AUTH_PASSPHRASE")
    init_parser.add_argument("--auth-ttl", help="TG_AUTH_TTL_SECONDS")
    init_parser.set_defaults(handler=init_env)

    start_parser = subparsers.add_parser("start", help="start tg-codex service")
    start_parser.add_argument("--host", default="0.0.0.0")
    start_parser.add_argument("--port", type=int, default=8000)
    start_parser.add_argument("--reload", action="store_true", help="enable reload (python mode only)")
    start_parser.add_argument("--log-level", default="info", choices=["critical", "error", "warning", "info", "debug", "trace"])
    start_parser.set_defaults(handler=start_service)

    return parser


def main() -> int:
    parser = build_parser()

    argv = sys.argv[1:]
    if argv and argv[0] in {"-h", "--help"}:
        parser.print_help()
        return 0
    if not argv or argv[0].startswith("-"):
        argv = ["start", *argv]

    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 1
    return int(handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
