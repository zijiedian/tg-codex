from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import time
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
    "TG_ENABLE_OUTPUT_FILE",
    "TG_ENABLE_SESSION_RESUME",
    "TG_AUTH_PASSPHRASE",
    "TG_AUTH_TTL_SECONDS",
]

DEFAULT_ENV = {
    "TG_WEBHOOK_URL": "",
    "TG_WEBHOOK_SECRET": "",
    "CODEX_COMMAND_PREFIX": "codex -a never --search exec -s danger-full-access --skip-git-repo-check",
    "CODEX_TIMEOUT_SECONDS": "21600",
    "TG_ALLOW_PLAIN_TEXT": "1",
    "TG_ALLOW_CMD_OVERRIDE": "0",
    "TG_MAX_IMAGE_BYTES": "10485760",
    "TG_MAX_BUFFERED_OUTPUT_CHARS": "200000",
    "TG_MAX_CONCURRENT_TASKS": "2",
    "TG_ENABLE_OUTPUT_FILE": "0",
    "TG_ENABLE_SESSION_RESUME": "1",
    "TG_AUTH_PASSPHRASE": "",
    "TG_AUTH_TTL_SECONDS": "7d",
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


def _discover_chat_user_ids(token: str, wait_seconds: int = 18) -> tuple[str, str]:
    _telegram_api_get(token, "getMe")
    discovered_chat_ids: set[int] = set()
    discovered_user_ids: set[int] = set()
    deadline = time.monotonic() + max(1, wait_seconds)

    while True:
        remaining = int(deadline - time.monotonic())
        timeout = max(1, min(6, remaining))
        try:
            updates_resp = _telegram_api_get(
                token,
                "getUpdates",
                params={
                    "limit": "100",
                    "timeout": str(timeout),
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
        except RuntimeError as err:
            detail = str(err).lower()
            if "can't use getupdates method while webhook is active" in detail:
                raise RuntimeError(
                    "Cannot auto-discover chat_id/user_id because webhook mode is active. "
                    "Disable webhook first (or switch to webhook deployment), then rerun "
                    "`tg-codex --token <TG_BOT_TOKEN>`."
                ) from err
            raise

        updates = updates_resp.get("result") or []
        chat_csv, user_csv = _collect_ids_from_updates(updates)
        if chat_csv:
            discovered_chat_ids.update(int(item) for item in chat_csv.split(",") if item)
        if user_csv:
            discovered_user_ids.update(int(item) for item in user_csv.split(",") if item)
        if discovered_chat_ids and discovered_user_ids:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.25)

    chat_ids = ",".join(str(value) for value in sorted(discovered_chat_ids))
    user_ids = ",".join(str(value) for value in sorted(discovered_user_ids))
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
    if not normalized_chat or not normalized_user:
        missing_parts: list[str] = []
        if not normalized_chat:
            missing_parts.append("chat_id")
        if not normalized_user:
            missing_parts.append("user_id")
        missing_text = "/".join(missing_parts)
        raise RuntimeError(
            f"Cannot auto-discover {missing_text} yet. "
            "Please send /start (or any message) to your bot from the target private chat/group, "
            "then rerun `tg-codex --token <TG_BOT_TOKEN>`."
        )
    return normalized_chat, normalized_user


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
    parser.add_argument("--token", help="TG_BOT_TOKEN (optional, used for one-line first start)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="enable reload (python mode only)")
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
    )
    return parser


def main() -> int:
    parser = build_parser()

    argv = sys.argv[1:]
    if argv and argv[0] == "start":
        argv = argv[1:]
    if argv and argv[0] == "init":
        print("Error: `init` has been removed. Use `tg-codex --token <TG_BOT_TOKEN>` to start.", file=sys.stderr)
        return 1

    args = parser.parse_args(argv)
    try:
        return int(start_service(args))
    except RuntimeError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
