from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
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

DEFAULT_ENV = {
    "TG_WEBHOOK_URL": "",
    "TG_WEBHOOK_SECRET": "",
    "CODEX_COMMAND_PREFIX": "codex -a never exec --full-auto",
    "CODEX_TIMEOUT_SECONDS": "600",
    "TG_ALLOW_PLAIN_TEXT": "0",
    "TG_ALLOW_CMD_OVERRIDE": "0",
    "TG_MAX_IMAGE_BYTES": "10485760",
    "TG_MAX_BUFFERED_OUTPUT_CHARS": "200000",
    "TG_MAX_CONCURRENT_TASKS": "2",
    "TG_AUTH_PASSPHRASE": "",
    "TG_AUTH_TTL_SECONDS": "43200",
}

ID_ITEM_RE = re.compile(r"^-?\d+$")


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


def _pick(existing: dict[str, str], key: str, override: str | None, default: str = "") -> str:
    if override is not None and override != "":
        return override
    if key in existing and existing[key] != "":
        return existing[key]
    return default


def _normalize_id_csv(raw: str) -> str:
    if not raw:
        return ""
    values = [part.strip() for part in raw.split(",") if part.strip()]
    if not values:
        return ""

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value.startswith("<") and value.endswith(">"):
            return ""
        if not ID_ITEM_RE.fullmatch(value):
            return ""
        if value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return ",".join(normalized)


def _write_env(path: Path, payload: dict[str, str]) -> None:
    lines = [f"{key}={payload.get(key, '')}" for key in ENV_KEYS]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _telegram_api_get(token: str, method: str, params: dict[str, str] | None = None) -> dict:
    query = ""
    if params:
        query = "?" + urllib.parse.urlencode(params)
    url = f"https://api.telegram.org/bot{token}/{method}{query}"

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "tg-codex",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram API {method} failed: HTTP {err.code} {detail}") from err
    except urllib.error.URLError as err:
        raise RuntimeError(f"Telegram API {method} failed: {err}") from err

    if not data.get("ok"):
        raise RuntimeError(f"Telegram API {method} failed: {data.get('description', 'unknown error')}")
    return data


def _collect_ids_from_updates(updates: list[dict]) -> tuple[str, str]:
    chat_ids: set[int] = set()
    user_ids: set[int] = set()

    def collect_from_message(msg: dict | None) -> None:
        if not isinstance(msg, dict):
            return
        chat = msg.get("chat")
        if isinstance(chat, dict) and isinstance(chat.get("id"), int):
            chat_ids.add(chat["id"])
        sender = msg.get("from")
        if isinstance(sender, dict) and isinstance(sender.get("id"), int):
            user_ids.add(sender["id"])

    for update in updates:
        if not isinstance(update, dict):
            continue

        for key in ("message", "edited_message", "channel_post", "edited_channel_post"):
            collect_from_message(update.get(key))

        callback_query = update.get("callback_query")
        if isinstance(callback_query, dict):
            collect_from_message(callback_query.get("message"))
            sender = callback_query.get("from")
            if isinstance(sender, dict) and isinstance(sender.get("id"), int):
                user_ids.add(sender["id"])

        for key in ("my_chat_member", "chat_member"):
            member_update = update.get(key)
            if isinstance(member_update, dict):
                chat = member_update.get("chat")
                if isinstance(chat, dict) and isinstance(chat.get("id"), int):
                    chat_ids.add(chat["id"])
                sender = member_update.get("from")
                if isinstance(sender, dict) and isinstance(sender.get("id"), int):
                    user_ids.add(sender["id"])

    chat_csv = ",".join(str(value) for value in sorted(chat_ids))
    user_csv = ",".join(str(value) for value in sorted(user_ids))
    return chat_csv, user_csv


def _discover_chat_user_ids(token: str) -> tuple[str, str]:
    _telegram_api_get(token, "getMe")
    updates_resp = _telegram_api_get(
        token,
        "getUpdates",
        params={
            "limit": "100",
            "timeout": "1",
            "allowed_updates": json.dumps(
                [
                    "message",
                    "edited_message",
                    "channel_post",
                    "edited_channel_post",
                    "callback_query",
                    "my_chat_member",
                    "chat_member",
                ]
            ),
        },
    )
    updates = updates_resp.get("result") or []
    chat_ids, user_ids = _collect_ids_from_updates(updates)
    if not chat_ids or not user_ids:
        raise RuntimeError(
            "Cannot auto-discover chat_id/user_id yet. "
            "Please send /start to your bot from the target chat/user once, "
            "then rerun `tg-codex --token <TG_BOT_TOKEN>`."
        )
    return chat_ids, user_ids


def _build_payload(existing: dict[str, str], overrides: dict[str, str]) -> dict[str, str]:
    payload: dict[str, str] = {}
    for key in ENV_KEYS:
        value = overrides.get(key)
        if value is None:
            value = existing.get(key, "")
        if value == "":
            value = DEFAULT_ENV.get(key, "")
        payload[key] = value
    return payload


def _resolve_and_fill_ids(token: str, chat_ids: str, user_ids: str) -> tuple[str, str]:
    normalized_chat = _normalize_id_csv(chat_ids)
    normalized_user = _normalize_id_csv(user_ids)
    if normalized_chat and normalized_user:
        return normalized_chat, normalized_user

    discovered_chat, discovered_user = _discover_chat_user_ids(token)
    if not normalized_chat:
        normalized_chat = discovered_chat
    if not normalized_user:
        normalized_user = discovered_user
    return normalized_chat, normalized_user


def init_env(args: argparse.Namespace) -> int:
    env_path = _env_path()
    existing = _load_existing_env(env_path)

    token = _pick(existing, "TG_BOT_TOKEN", args.token)
    if not token:
        raise SystemExit("TG_BOT_TOKEN is required, set --token")

    existing_chat = _normalize_id_csv(_pick(existing, "TG_ALLOWED_CHAT_IDS", None))
    existing_user = _normalize_id_csv(_pick(existing, "TG_ALLOWED_USER_IDS", None))
    try:
        chat_ids, user_ids = _discover_chat_user_ids(token)
    except RuntimeError:
        if args.token is None and existing_chat and existing_user:
            chat_ids, user_ids = existing_chat, existing_user
        else:
            raise

    admin_chat_ids = _normalize_id_csv(_pick(existing, "TG_ADMIN_CHAT_IDS", None)) or chat_ids
    admin_user_ids = _normalize_id_csv(_pick(existing, "TG_ADMIN_USER_IDS", None)) or user_ids

    webhook_url = _pick(existing, "TG_WEBHOOK_URL", args.webhook_url)
    webhook_secret = _pick(existing, "TG_WEBHOOK_SECRET", args.webhook_secret)
    if webhook_url and not webhook_secret:
        webhook_secret = secrets.token_urlsafe(24)

    payload = _build_payload(
        existing=existing,
        overrides={
            "TG_BOT_TOKEN": token,
            "TG_WEBHOOK_URL": webhook_url,
            "TG_WEBHOOK_SECRET": webhook_secret,
            "TG_ALLOWED_CHAT_IDS": chat_ids,
            "TG_ALLOWED_USER_IDS": user_ids,
            "TG_ADMIN_CHAT_IDS": admin_chat_ids,
            "TG_ADMIN_USER_IDS": admin_user_ids,
            "CODEX_COMMAND_PREFIX": _pick(existing, "CODEX_COMMAND_PREFIX", args.codex_prefix),
            "CODEX_TIMEOUT_SECONDS": _pick(existing, "CODEX_TIMEOUT_SECONDS", args.timeout),
            "TG_ALLOW_PLAIN_TEXT": _pick(existing, "TG_ALLOW_PLAIN_TEXT", args.allow_plain_text),
            "TG_ALLOW_CMD_OVERRIDE": _pick(existing, "TG_ALLOW_CMD_OVERRIDE", args.allow_cmd_override),
            "TG_MAX_IMAGE_BYTES": _pick(existing, "TG_MAX_IMAGE_BYTES", args.max_image_bytes),
            "TG_MAX_BUFFERED_OUTPUT_CHARS": _pick(
                existing,
                "TG_MAX_BUFFERED_OUTPUT_CHARS",
                args.max_buffered_output_chars,
            ),
            "TG_MAX_CONCURRENT_TASKS": _pick(existing, "TG_MAX_CONCURRENT_TASKS", args.max_concurrent_tasks),
            "TG_AUTH_PASSPHRASE": _pick(existing, "TG_AUTH_PASSPHRASE", args.auth_passphrase),
            "TG_AUTH_TTL_SECONDS": _pick(existing, "TG_AUTH_TTL_SECONDS", args.auth_ttl),
        },
    )
    _write_env(env_path, payload)
    print(f"Wrote config: {env_path}")
    print(f"Auto-detected TG_ALLOWED_CHAT_IDS={chat_ids}")
    print(f"Auto-detected TG_ALLOWED_USER_IDS={user_ids}")
    print("Tip: run `tg-codex --port 18000` (or `./one_click_start.sh --token <TG_BOT_TOKEN>`) to launch.")
    return 0


def _prepare_env_for_start(token_override: str | None) -> tuple[str, bool]:
    env_path = _env_path()
    existing = _load_existing_env(env_path)

    token = _pick(existing, "TG_BOT_TOKEN", token_override, os.getenv("TG_BOT_TOKEN", "").strip())
    if not token:
        raise RuntimeError(
            "TG_BOT_TOKEN is required. Run once with `tg-codex --token <TG_BOT_TOKEN>`."
        )

    chat_ids = _normalize_id_csv(existing.get("TG_ALLOWED_CHAT_IDS", ""))
    user_ids = _normalize_id_csv(existing.get("TG_ALLOWED_USER_IDS", ""))
    resolved_chat, resolved_user = _resolve_and_fill_ids(token, chat_ids, user_ids)

    admin_chat = _normalize_id_csv(existing.get("TG_ADMIN_CHAT_IDS", "")) or resolved_chat
    admin_user = _normalize_id_csv(existing.get("TG_ADMIN_USER_IDS", "")) or resolved_user
    auth_passphrase = _pick(existing, "TG_AUTH_PASSPHRASE", None)
    generated_auth_passphrase = False
    if not auth_passphrase:
        auth_passphrase = secrets.token_urlsafe(18)
        generated_auth_passphrase = True
    webhook_url = _pick(existing, "TG_WEBHOOK_URL", None)
    webhook_secret = _pick(existing, "TG_WEBHOOK_SECRET", None)
    if webhook_url and not webhook_secret:
        webhook_secret = secrets.token_urlsafe(24)

    payload = _build_payload(
        existing=existing,
        overrides={
            "TG_BOT_TOKEN": token,
            "TG_ALLOWED_CHAT_IDS": resolved_chat,
            "TG_ALLOWED_USER_IDS": resolved_user,
            "TG_ADMIN_CHAT_IDS": admin_chat,
            "TG_ADMIN_USER_IDS": admin_user,
            "TG_AUTH_PASSPHRASE": auth_passphrase,
            "TG_WEBHOOK_SECRET": webhook_secret,
        },
    )
    _write_env(env_path, payload)
    return auth_passphrase, generated_auth_passphrase


def start_service(args: argparse.Namespace) -> int:
    if getattr(sys, "frozen", False) and args.reload:
        print("reload is not supported in frozen binary mode, forcing --no-reload")
        args.reload = False

    auth_passphrase, generated_auth_passphrase = _prepare_env_for_start(getattr(args, "token", None))
    if generated_auth_passphrase:
        print("First start detected. Copy this command to Telegram to authenticate:")
        print(f"/auth {auth_passphrase}")

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

    init_parser = subparsers.add_parser("init", help="initialize or update .env (token only)")
    init_parser.add_argument("--token", help="TG_BOT_TOKEN")
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
    start_parser.add_argument("--token", help="TG_BOT_TOKEN (optional, used for one-line first start)")
    start_parser.add_argument("--host", default="0.0.0.0")
    start_parser.add_argument("--port", type=int, default=8000)
    start_parser.add_argument("--reload", action="store_true", help="enable reload (python mode only)")
    start_parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
    )
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
    try:
        return int(handler(args))
    except RuntimeError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
