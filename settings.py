import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Set

from dotenv import load_dotenv

from constants import (
    DEFAULT_AUTH_TTL_SECONDS,
    DEFAULT_MAX_BUFFERED_OUTPUT_CHARS,
    DEFAULT_MAX_CONCURRENT_TASKS,
    MIN_AUTH_PASSPHRASE_LENGTH,
)
from codex_runner import _validate_codex_prefix


@dataclass
class Settings:
    bot_token: str
    webhook_url: str
    webhook_secret: str
    allowed_chat_ids: Set[int]
    allowed_user_ids: Set[int]
    admin_chat_ids: Set[int]
    admin_user_ids: Set[int]
    codex_command_prefix: str
    codex_timeout_seconds: int
    allow_plain_text: bool
    allow_cmd_override: bool
    max_image_bytes: int
    max_buffered_output_chars: int
    max_concurrent_tasks: int
    auth_passphrase: str
    auth_ttl_seconds: int


def _parse_allowed_ids(value: str) -> Set[int]:
    if not value.strip():
        return set()
    result: Set[int] = set()
    for item in value.split(","):
        item = item.strip()
        if item:
            result.add(int(item))
    return result


def runtime_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def load_settings() -> Settings:
    env_path = runtime_base_dir() / ".env"
    load_dotenv(env_path)
    if env_path.exists():
        try:
            os.chmod(env_path, 0o600)
        except OSError:
            pass
    token = os.getenv("TG_BOT_TOKEN", "").strip()
    webhook_url = os.getenv("TG_WEBHOOK_URL", "").strip()
    webhook_secret = os.getenv("TG_WEBHOOK_SECRET", "").strip()
    allowed_chat_ids = _parse_allowed_ids(os.getenv("TG_ALLOWED_CHAT_IDS", ""))
    allowed_user_ids_raw = os.getenv("TG_ALLOWED_USER_IDS", "").strip()
    allowed_user_ids = _parse_allowed_ids(allowed_user_ids_raw) if allowed_user_ids_raw else set(allowed_chat_ids)
    admin_chat_ids_raw = os.getenv("TG_ADMIN_CHAT_IDS", "").strip()
    admin_chat_ids = _parse_allowed_ids(admin_chat_ids_raw) if admin_chat_ids_raw else set(allowed_chat_ids)
    admin_user_ids_raw = os.getenv("TG_ADMIN_USER_IDS", "").strip()
    admin_user_ids = _parse_allowed_ids(admin_user_ids_raw) if admin_user_ids_raw else set(allowed_user_ids)
    codex_prefix = os.getenv("CODEX_COMMAND_PREFIX", "codex -a never exec --full-auto").strip()
    codex_timeout = int(os.getenv("CODEX_TIMEOUT_SECONDS", "600"))
    allow_plain_text = os.getenv("TG_ALLOW_PLAIN_TEXT", "0").strip().lower() in {"1", "true", "yes"}
    allow_cmd_override = os.getenv("TG_ALLOW_CMD_OVERRIDE", "0").strip().lower() in {"1", "true", "yes"}
    max_image_bytes = int(os.getenv("TG_MAX_IMAGE_BYTES", str(10 * 1024 * 1024)))
    max_buffered_output_chars = int(os.getenv("TG_MAX_BUFFERED_OUTPUT_CHARS", str(DEFAULT_MAX_BUFFERED_OUTPUT_CHARS)))
    max_concurrent_tasks = int(os.getenv("TG_MAX_CONCURRENT_TASKS", str(DEFAULT_MAX_CONCURRENT_TASKS)))
    auth_passphrase = os.getenv("TG_AUTH_PASSPHRASE", "").strip()
    auth_ttl_seconds = int(os.getenv("TG_AUTH_TTL_SECONDS", str(DEFAULT_AUTH_TTL_SECONDS)))

    if not token:
        raise RuntimeError("TG_BOT_TOKEN is required")
    if not allowed_chat_ids:
        raise RuntimeError("TG_ALLOWED_CHAT_IDS is required and cannot be empty")
    if not allowed_user_ids:
        raise RuntimeError("TG_ALLOWED_USER_IDS resolves to empty set")
    if not admin_chat_ids:
        raise RuntimeError("TG_ADMIN_CHAT_IDS resolves to empty set")
    if not admin_user_ids:
        raise RuntimeError("TG_ADMIN_USER_IDS resolves to empty set")
    if not admin_chat_ids.issubset(allowed_chat_ids):
        raise RuntimeError("TG_ADMIN_CHAT_IDS must be a subset of TG_ALLOWED_CHAT_IDS")
    if not admin_user_ids.issubset(allowed_user_ids):
        raise RuntimeError("TG_ADMIN_USER_IDS must be a subset of TG_ALLOWED_USER_IDS")
    if webhook_url and not webhook_secret:
        raise RuntimeError("TG_WEBHOOK_SECRET is required when TG_WEBHOOK_URL is set")
    if webhook_secret and len(webhook_secret) < 16:
        raise RuntimeError("TG_WEBHOOK_SECRET must be at least 16 characters")
    if max_image_bytes <= 0:
        raise RuntimeError("TG_MAX_IMAGE_BYTES must be positive")
    if max_buffered_output_chars < 20_000:
        raise RuntimeError("TG_MAX_BUFFERED_OUTPUT_CHARS must be >= 20000")
    if max_concurrent_tasks <= 0:
        raise RuntimeError("TG_MAX_CONCURRENT_TASKS must be positive")
    if auth_ttl_seconds <= 0:
        raise RuntimeError("TG_AUTH_TTL_SECONDS must be positive")
    if auth_passphrase and len(auth_passphrase) < MIN_AUTH_PASSPHRASE_LENGTH:
        raise RuntimeError(f"TG_AUTH_PASSPHRASE must be at least {MIN_AUTH_PASSPHRASE_LENGTH} characters")
    _validate_codex_prefix(codex_prefix)

    return Settings(
        bot_token=token,
        webhook_url=webhook_url,
        webhook_secret=webhook_secret,
        allowed_chat_ids=allowed_chat_ids,
        allowed_user_ids=allowed_user_ids,
        admin_chat_ids=admin_chat_ids,
        admin_user_ids=admin_user_ids,
        codex_command_prefix=codex_prefix,
        codex_timeout_seconds=codex_timeout,
        allow_plain_text=allow_plain_text,
        allow_cmd_override=allow_cmd_override,
        max_image_bytes=max_image_bytes,
        max_buffered_output_chars=max_buffered_output_chars,
        max_concurrent_tasks=max_concurrent_tasks,
        auth_passphrase=auth_passphrase,
        auth_ttl_seconds=auth_ttl_seconds,
    )
