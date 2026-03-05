import asyncio
import html
import hmac
import json
import mimetypes
import os
import re
import shlex
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from codex_runner import _validate_codex_prefix, run_codex_stream
from constants import (
    ANSI_ESCAPE_RE,
    CODE_INDENT_RE,
    CODE_KEYWORD_RE,
    DEFAULT_AUTH_TTL_SECONDS,
    DIFF_HEADER_RE,
    EDIT_THROTTLE_SECONDS,
    FINAL_OUTPUT_CHUNK_LIMIT,
    IDLE_EDIT_THROTTLE_SECONDS,
    LONG_SECRET_RE,
    MARKDOWN_BULLET_RE,
    MARKDOWN_FENCE_CLOSE_RE,
    MARKDOWN_FENCE_RE,
    MARKDOWN_HEADING_RE,
    MARKDOWN_ORDERED_RE,
    MARKDOWN_RULE_RE,
    MIN_AUTH_PASSPHRASE_LENGTH,
    OUTPUT_FILE_MIN_CHARS,
    PAGE_SESSION_TTL_SECONDS,
    PATCH_ADD_PREFIX,
    PATCH_BEGIN_MARKER,
    PATCH_DELETE_PREFIX,
    PATCH_END_MARKER,
    PATCH_END_OF_FILE_MARKER,
    PATCH_MOVE_PREFIX,
    PATCH_UPDATE_PREFIX,
    PREVIEW_DIVIDER_RE,
    PREVIEW_LINE_CHAR_LIMIT,
    PREVIEW_NOISE_PATTERNS,
    REQUEST_DEDUP_SECONDS,
    SENSITIVE_OPTION_RE,
    SESSION_ID_RE,
    SHELL_PROMPT_RE,
    STREAM_PREVIEW_LIMIT,
    STREAM_PROGRESS_IO_TIMEOUT_SECONDS,
    STREAM_PREVIEW_LINE_LIMIT,
    TELEGRAM_MESSAGE_LIMIT,
    THINKING_DETAIL_MAX_CHARS,
    THINKING_DETAIL_MAX_LINES,
    THINKING_SPINNER_FRAMES,
    TRACE_SECTION_MARKERS,
    TRACE_SKIP_SECTION_MARKERS,
)
from settings import Settings, runtime_base_dir


@dataclass
class PageSession:
    chat_id: int
    message_id: int
    pages: list[str]
    created_at: float
    last_access: float
    current_index: int = 0


@dataclass
class TraceSection:
    marker: str
    lines: list[str]

    @property
    def content(self) -> str:
        return "\n".join(self.lines).strip()


@dataclass
class SkillInfo:
    name: str
    description: str
    skill_md: Path
    is_system: bool


def clip_for_telegram(text: str, limit: int = 3600) -> str:
    if len(text) <= limit:
        return text
    return "...\n" + text[-limit:]


def clip_for_inline(text: str, limit: int = 240) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


class Bridge:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.tasks: Dict[int, asyncio.Task] = {}
        self.default_codex_prefix = settings.codex_command_prefix
        self.codex_prefix = settings.codex_command_prefix
        self.recent_requests: Dict[tuple[int, int], float] = {}
        self.auth_sessions: Dict[tuple[int, int], float] = {}
        base_dir = runtime_base_dir()
        self.media_dir = base_dir / "incoming_media"
        self.output_dir = base_dir / "outputs"
        self.env_path = base_dir / ".env"
        self.sessions_path = base_dir / "chat_sessions.json"
        self.workdirs_path = base_dir / "chat_workdirs.json"
        self.page_sessions_path = base_dir / "page_sessions.json"
        self.chat_sessions: Dict[int, str] = self._load_chat_sessions()
        self.chat_workdirs: Dict[int, str] = self._load_chat_workdirs()
        self.page_sessions: Dict[tuple[int, int], PageSession] = self._load_page_sessions()

    def _load_chat_sessions(self) -> Dict[int, str]:
        if not self.sessions_path.exists():
            return {}
        try:
            raw = json.loads(self.sessions_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        sessions: Dict[int, str] = {}
        for chat_key, session_id in raw.items():
            if not isinstance(chat_key, str) or not isinstance(session_id, str):
                continue
            if not SESSION_ID_RE.fullmatch(f"session id: {session_id}"):
                continue
            try:
                sessions[int(chat_key)] = session_id
            except ValueError:
                continue
        return sessions

    def _save_chat_sessions(self) -> None:
        payload = {str(chat_id): session_id for chat_id, session_id in self.chat_sessions.items()}
        self.sessions_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        try:
            os.chmod(self.sessions_path, 0o600)
        except OSError:
            pass

    def _set_chat_session(self, chat_id: int, session_id: str) -> None:
        self.chat_sessions[chat_id] = session_id
        self._save_chat_sessions()

    def _clear_chat_session(self, chat_id: int) -> bool:
        existed = chat_id in self.chat_sessions
        if existed:
            self.chat_sessions.pop(chat_id, None)
            self._save_chat_sessions()
        return existed

    def _load_chat_workdirs(self) -> Dict[int, str]:
        if not self.workdirs_path.exists():
            return {}
        try:
            raw = json.loads(self.workdirs_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}

        workdirs: Dict[int, str] = {}
        for chat_key, workdir in raw.items():
            if not isinstance(chat_key, str) or not isinstance(workdir, str):
                continue
            normalized = workdir.strip()
            if not normalized:
                continue
            try:
                chat_id = int(chat_key)
            except ValueError:
                continue
            workdirs[chat_id] = normalized
        return workdirs

    def _save_chat_workdirs(self) -> None:
        payload = {str(chat_id): workdir for chat_id, workdir in self.chat_workdirs.items()}
        self.workdirs_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        try:
            os.chmod(self.workdirs_path, 0o600)
        except OSError:
            pass

    def _load_page_sessions(self) -> Dict[tuple[int, int], PageSession]:
        if not self.page_sessions_path.exists():
            return {}
        try:
            raw = json.loads(self.page_sessions_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, list):
            return {}

        now = time.time()
        sessions: Dict[tuple[int, int], PageSession] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                chat_id = int(item.get("chat_id"))
                message_id = int(item.get("message_id"))
                created_at = float(item.get("created_at", now))
                last_access = float(item.get("last_access", created_at))
                current_index = int(item.get("current_index", 0))
            except (TypeError, ValueError):
                continue

            pages_raw = item.get("pages")
            if not isinstance(pages_raw, list):
                continue
            pages = [page for page in pages_raw if isinstance(page, str) and page.strip()]
            if not pages:
                continue

            if now - last_access > PAGE_SESSION_TTL_SECONDS:
                continue

            if current_index < 0:
                current_index = 0
            if current_index >= len(pages):
                current_index = len(pages) - 1

            key = (chat_id, message_id)
            sessions[key] = PageSession(
                chat_id=chat_id,
                message_id=message_id,
                pages=pages,
                created_at=created_at,
                last_access=last_access,
                current_index=current_index,
            )
        return sessions

    def _save_page_sessions(self) -> None:
        payload = [
            {
                "chat_id": session.chat_id,
                "message_id": session.message_id,
                "pages": session.pages,
                "created_at": session.created_at,
                "last_access": session.last_access,
                "current_index": session.current_index,
            }
            for session in sorted(
                self.page_sessions.values(),
                key=lambda value: (value.chat_id, value.message_id),
            )
        ]
        self.page_sessions_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
        try:
            os.chmod(self.page_sessions_path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _parse_toggle_value(raw: str) -> Optional[bool]:
        normalized = raw.strip().lower()
        if normalized in {"1", "true", "yes", "on", "enable", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disable", "disabled"}:
            return False
        return None

    @staticmethod
    def _parse_auth_ttl_setting(raw: str) -> Optional[tuple[int, str]]:
        value = raw.strip()
        match = re.fullmatch(r"(?i)(\d+)\s*([smhd]?)", value)
        if not match:
            return None

        amount = int(match.group(1))
        unit = (match.group(2) or "").lower()
        multipliers = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
        seconds = amount * multipliers[unit]
        if seconds <= 0:
            return None

        env_value = f"{amount}{unit}" if unit else str(amount)
        return seconds, env_value

    def _upsert_env_settings(self, updates: Dict[str, str]) -> None:
        existing_lines: list[str] = []
        if self.env_path.exists():
            existing_lines = self.env_path.read_text(encoding="utf-8").splitlines()

        remaining = dict(updates)
        output_lines: list[str] = []
        for raw_line in existing_lines:
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#") or "=" not in raw_line:
                output_lines.append(raw_line)
                continue
            key, _ = raw_line.split("=", 1)
            normalized_key = key.strip()
            if normalized_key in remaining:
                output_lines.append(f"{normalized_key}={remaining.pop(normalized_key)}")
                continue
            output_lines.append(raw_line)

        for key in sorted(remaining):
            output_lines.append(f"{key}={remaining[key]}")

        payload = "\n".join(output_lines).rstrip("\n") + "\n"
        self.env_path.write_text(payload, encoding="utf-8")
        try:
            os.chmod(self.env_path, 0o600)
        except OSError:
            pass

        for key, value in updates.items():
            os.environ[key] = value

    def _get_chat_workdir(self, chat_id: int) -> Optional[Path]:
        raw = self.chat_workdirs.get(chat_id, "").strip()
        if not raw:
            return None
        try:
            path = Path(raw).expanduser().resolve()
        except OSError:
            return None
        if not path.exists() or not path.is_dir():
            return None
        return path

    def _set_chat_workdir(self, chat_id: int, workdir: Path) -> None:
        self.chat_workdirs[chat_id] = str(workdir)
        self._save_chat_workdirs()

    def _clear_chat_workdir(self, chat_id: int) -> bool:
        existed = chat_id in self.chat_workdirs
        if existed:
            self.chat_workdirs.pop(chat_id, None)
            self._save_chat_workdirs()
        return existed

    def _resolve_target_workdir(self, chat_id: int, raw_path: str) -> Path:
        normalized = raw_path.strip()
        if not normalized:
            raise ValueError("directory path cannot be empty")
        base = self._get_chat_workdir(chat_id) or runtime_base_dir()
        candidate = Path(normalized).expanduser()
        target = candidate if candidate.is_absolute() else (base / candidate)
        resolved = target.resolve()
        if not resolved.exists():
            raise ValueError(f"directory does not exist: {resolved}")
        if not resolved.is_dir():
            raise ValueError(f"not a directory: {resolved}")
        return resolved

    def _effective_workdir(self, chat_id: int) -> Path:
        return self._get_chat_workdir(chat_id) or runtime_base_dir()

    @staticmethod
    def _skills_root_dir() -> Path:
        codex_home = os.getenv("CODEX_HOME", "").strip()
        if codex_home:
            base = Path(codex_home).expanduser()
        else:
            base = Path.home() / ".codex"
        return base / "skills"

    @staticmethod
    def _parse_skill_frontmatter(text: str) -> tuple[str, str]:
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            return "", ""

        name = ""
        description = ""
        for raw in lines[1:]:
            line = raw.strip()
            if line == "---":
                break
            if ":" not in raw:
                continue
            key, value = raw.split(":", 1)
            key = key.strip().lower()
            value = value.strip().strip('"').strip("'")
            if key == "name" and value and not name:
                name = value
            elif key == "description" and value and not description:
                description = value
        return name, description

    def _discover_installed_skills(self) -> tuple[Path, list[SkillInfo]]:
        skills_root = self._skills_root_dir()
        if not skills_root.exists() or not skills_root.is_dir():
            return skills_root, []

        skills: list[SkillInfo] = []
        for skill_md in sorted(skills_root.rglob("SKILL.md")):
            if not skill_md.is_file():
                continue
            try:
                relative = skill_md.relative_to(skills_root)
            except ValueError:
                continue

            relative_dir = relative.parent
            path_name = relative_dir.as_posix()
            if path_name.startswith(".system/"):
                path_name = path_name.split("/", 1)[1]
            if path_name in {"", "."}:
                path_name = skill_md.parent.name or "unknown-skill"

            metadata_name = ""
            metadata_desc = ""
            try:
                content = skill_md.read_text(encoding="utf-8")
            except OSError:
                content = ""
            if content:
                metadata_name, metadata_desc = self._parse_skill_frontmatter(content)

            name = metadata_name.strip() or path_name
            description = metadata_desc.strip() or "No description."
            is_system = bool(relative_dir.parts) and relative_dir.parts[0] == ".system"
            skills.append(
                SkillInfo(
                    name=name,
                    description=description,
                    skill_md=skill_md.resolve(),
                    is_system=is_system,
                )
            )

        skills.sort(key=lambda item: (item.is_system, item.name.lower()))
        return skills_root, skills

    @staticmethod
    def _format_skill_name_lines(skills: list[SkillInfo], start_index: int = 1) -> list[str]:
        lines: list[str] = []
        for idx, skill in enumerate(skills, start=start_index):
            lines.append(f"{idx}. <code>{html.escape(skill.name)}</code>")
        return lines

    @staticmethod
    def _truncate_text(text: str, limit: int = 120) -> str:
        stripped = " ".join(text.split())
        if len(stripped) <= limit:
            return stripped
        return stripped[: max(0, limit - 3)].rstrip() + "..."

    @staticmethod
    def _extract_session_id(output: str) -> Optional[str]:
        found = SESSION_ID_RE.findall(output)
        if not found:
            return None
        return found[-1]

    @staticmethod
    def _build_resume_command(prefix: list[str], session_id: str, prompt: str) -> Optional[list[str]]:
        cmd = list(prefix)
        if "exec" in cmd:
            exec_idx = cmd.index("exec")
            if exec_idx + 1 < len(cmd) and not cmd[exec_idx + 1].startswith("-"):
                # Prefix already specifies a concrete exec subcommand; avoid rewriting unexpectedly.
                return None
            # Keep all exec-level options in-place, then append resume subcommand.
            cmd.extend(["resume", session_id, prompt])
            return cmd
        cmd.extend(["exec", "resume", session_id, prompt])
        return cmd

    def _resolve_codex_command(self, chat_id: int, prompt: str) -> tuple[list[str], str, Path]:
        base = _validate_codex_prefix(self.codex_prefix)
        session_id = self.chat_sessions.get(chat_id)
        workdir = self._effective_workdir(chat_id)
        if session_id and self.settings.enable_session_resume:
            resume_cmd = self._build_resume_command(base, session_id, prompt)
            if resume_cmd is not None:
                return resume_cmd, session_id, workdir
        return base + [prompt], "", workdir

    def is_allowed(self, chat_id: int) -> bool:
        return chat_id in self.settings.allowed_chat_ids

    def is_admin(self, chat_id: int) -> bool:
        return chat_id in self.settings.admin_chat_ids

    def is_user_allowed(self, user_id: Optional[int]) -> bool:
        return user_id is not None and user_id in self.settings.allowed_user_ids

    def is_admin_user(self, user_id: Optional[int]) -> bool:
        return user_id is not None and user_id in self.settings.admin_user_ids

    def _is_update_authorized(self, update: Update, require_admin: bool = False) -> bool:
        chat = update.effective_chat
        if chat is None or not self.is_allowed(chat.id):
            return False
        user = update.effective_user
        user_id = user.id if user else None
        if not self.is_user_allowed(user_id):
            return False
        if require_admin and (not self.is_admin(chat.id) or not self.is_admin_user(user_id)):
            return False
        return True

    @staticmethod
    def _mask_sensitive_args(args: list[str]) -> list[str]:
        redacted: list[str] = []
        mask_next = False
        for arg in args:
            if mask_next:
                redacted.append("***")
                mask_next = False
                continue

            lowered = arg.lower()
            if "=" in arg:
                key, value = arg.split("=", 1)
                if SENSITIVE_OPTION_RE.search(key):
                    redacted.append(f"{key}=***")
                    continue
                if SENSITIVE_OPTION_RE.search(lowered):
                    redacted.append(f"{key}=***")
                    continue
                if LONG_SECRET_RE.fullmatch(value):
                    redacted.append(f"{key}=***")
                    continue
                redacted.append(arg)
                continue

            if SENSITIVE_OPTION_RE.search(lowered):
                redacted.append(arg)
                mask_next = True
                continue

            if LONG_SECRET_RE.fullmatch(arg):
                redacted.append("***")
                continue

            redacted.append(arg)
        return redacted

    def _redacted_command_text(self, command: str) -> str:
        try:
            args = shlex.split(command)
        except ValueError:
            return command
        return shlex.join(self._mask_sensitive_args(args))

    def _is_second_factor_enabled(self) -> bool:
        return bool(self.settings.auth_passphrase)

    def _auth_key(self, update: Update) -> Optional[tuple[int, int]]:
        if update.effective_chat is None or update.effective_user is None:
            return None
        return (update.effective_chat.id, update.effective_user.id)

    def _cleanup_auth_sessions(self) -> None:
        now = time.monotonic()
        stale_keys = [key for key, expires_at in self.auth_sessions.items() if expires_at <= now]
        for key in stale_keys:
            self.auth_sessions.pop(key, None)

    def _auth_seconds_left(self, update: Update) -> int:
        if not self._is_second_factor_enabled():
            return 0
        key = self._auth_key(update)
        if key is None:
            return 0
        self._cleanup_auth_sessions()
        expires_at = self.auth_sessions.get(key, 0.0)
        return max(0, int(expires_at - time.monotonic()))

    async def _ensure_second_factor(self, update: Update) -> bool:
        if not self._is_second_factor_enabled():
            return True
        if self._auth_seconds_left(update) > 0:
            return True
        await self.send_html(
            update,
            "<b>Second-factor required</b>\n"
            "Use <code>/auth &lt;passphrase&gt;</code> to unlock execution.",
        )
        return False

    @staticmethod
    def _code_inline(value: str) -> str:
        return f"<code>{html.escape(value)}</code>"

    @staticmethod
    def _code_block(value: str) -> str:
        return f"<pre>{html.escape(value)}</pre>"

    @staticmethod
    def _code_block_with_language(value: str, language: str) -> str:
        safe_lang = language.strip().lower()
        if not re.fullmatch(r"[a-z0-9_+-]{1,32}", safe_lang):
            return Bridge._code_block(value)
        return f"<pre><code class=\"language-{safe_lang}\">{html.escape(value)}</code></pre>"

    async def send_html(self, update: Update, text: str) -> None:
        if update.effective_message is None:
            return
        await update.effective_message.reply_text(
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    async def _send_message_draft_raw(
        self,
        chat_id: int,
        draft_id: int,
        text: str,
        message_thread_id: Optional[int] = None,
    ) -> None:
        if not text.strip():
            return

        payload: dict[str, str] = {
            "chat_id": str(chat_id),
            "draft_id": str(draft_id),
            "text": text,
            "parse_mode": str(ParseMode.HTML),
        }
        if message_thread_id is not None:
            payload["message_thread_id"] = str(message_thread_id)

        def _post() -> None:
            url = f"https://api.telegram.org/bot{self.settings.bot_token}/sendMessageDraft"
            body = urllib.parse.urlencode(payload).encode("utf-8")
            request = urllib.request.Request(
                url=url,
                data=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                    "User-Agent": "tg-codex",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    result = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as err:
                detail = err.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"sendMessageDraft HTTP {err.code}: {detail}") from err
            except urllib.error.URLError as err:
                raise RuntimeError(f"sendMessageDraft request failed: {err}") from err
            except json.JSONDecodeError as err:
                raise RuntimeError("sendMessageDraft returned invalid JSON") from err

            if not isinstance(result, dict):
                raise RuntimeError("sendMessageDraft returned malformed payload")
            if result.get("ok"):
                return
            error_code = result.get("error_code")
            description = str(result.get("description", "unknown error"))
            if error_code is None:
                raise RuntimeError(f"sendMessageDraft failed: {description}")
            raise RuntimeError(f"sendMessageDraft failed ({error_code}): {description}")

        await asyncio.to_thread(_post)

    @staticmethod
    def _clean_output(output: str) -> str:
        text = ANSI_ESCAPE_RE.sub("", output).replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
        lines = text.splitlines()
        compact: list[str] = []
        previous: Optional[str] = None
        for line in lines:
            if line == previous:
                continue
            compact.append(line)
            previous = line
        return "\n".join(compact).strip()

    @staticmethod
    def _format_preview_lines(lines: list[str]) -> str:
        formatted: list[str] = []
        for line in lines:
            normalized = line.replace("\t", "  ").rstrip()
            if len(normalized) > PREVIEW_LINE_CHAR_LIMIT:
                normalized = normalized[: PREVIEW_LINE_CHAR_LIMIT - 1] + "…"
            formatted.append(normalized)
        return "\n".join(formatted).strip()

    @staticmethod
    def _slice_preview_lines(lines: list[str], max_lines: int) -> list[str]:
        if len(lines) <= max_lines:
            return list(lines)

        start = len(lines) - max_lines
        preview_lines = list(lines[start:])

        in_fence = False
        last_opening = "```"
        for line in lines[:start]:
            stripped = line.strip()
            if not MARKDOWN_FENCE_RE.match(stripped):
                continue
            if in_fence:
                in_fence = False
                last_opening = "```"
            else:
                in_fence = True
                last_opening = stripped or "```"

        if in_fence:
            preview_lines.insert(0, last_opening)

        if preview_lines and MARKDOWN_FENCE_CLOSE_RE.match(preview_lines[0].strip()) and not in_fence:
            preview_lines = preview_lines[1:]

        fence_open = False
        for line in preview_lines:
            stripped = line.strip()
            if not MARKDOWN_FENCE_RE.match(stripped):
                continue
            fence_open = not fence_open
        if fence_open:
            preview_lines.append("```")

        return preview_lines

    @staticmethod
    def _is_preview_noise_line(line: str) -> bool:
        stripped = line.strip()
        if not stripped:
            return False
        return any(pattern.match(stripped) for pattern in PREVIEW_NOISE_PATTERNS)

    @staticmethod
    def _normalize_trace_marker(line: str) -> Optional[str]:
        stripped = line.strip().lower()
        if not stripped:
            return None

        candidate = stripped.rstrip(":")
        if candidate in TRACE_SECTION_MARKERS:
            return candidate

        for prefix in ("role:", "section:", "trace:", "tool:"):
            if not stripped.startswith(prefix):
                continue
            candidate = stripped[len(prefix) :].strip().rstrip(":")
            if candidate in TRACE_SECTION_MARKERS:
                return candidate

        if stripped.startswith("[") and "]" in stripped:
            candidate = stripped.split("]", 1)[1].strip().rstrip(":")
            if candidate in TRACE_SECTION_MARKERS:
                return candidate

        return None

    def _parse_trace_sections(self, lines: list[str]) -> list[TraceSection]:
        sections: list[TraceSection] = []
        current_marker: Optional[str] = None
        current_lines: list[str] = []

        for line in lines:
            marker = self._normalize_trace_marker(line)
            if marker is not None:
                if current_marker and current_lines:
                    sections.append(TraceSection(marker=current_marker, lines=current_lines))
                current_marker = marker
                current_lines = []
                continue

            if current_marker is None:
                continue

            lowered = line.strip().lower()
            if lowered.startswith("tokens used"):
                if current_lines:
                    sections.append(TraceSection(marker=current_marker, lines=current_lines))
                current_marker = None
                current_lines = []
                continue

            if self._is_preview_noise_line(line):
                continue
            current_lines.append(line)

        if current_marker and current_lines:
            sections.append(TraceSection(marker=current_marker, lines=current_lines))

        return sections

    @staticmethod
    def _is_prose_line(line: str) -> bool:
        stripped = line.strip()
        if not stripped:
            return False
        if MARKDOWN_HEADING_RE.match(line) or MARKDOWN_BULLET_RE.match(line) or MARKDOWN_ORDERED_RE.match(line):
            return True
        if stripped.startswith(("diff --git ", "index ", "--- ", "+++ ", "@@")):
            return False
        if Bridge._line_looks_like_code(line):
            return False
        if any("\u4e00" <= ch <= "\u9fff" for ch in stripped):
            return True
        if any(ch in stripped for ch in ("。", "，", "：", "；", "！", "？")):
            return True
        if re.search(r"[A-Za-z]", stripped) and " " in stripped and not re.search(r"[{}();=<>\[\]]", stripped):
            return True
        return False

    def _strip_accidental_outer_fence(self, text: str) -> str:
        normalized = text.strip()
        if "```" not in normalized:
            return normalized

        lines = normalized.splitlines()
        if len(lines) < 3:
            return normalized

        opening = lines[0].strip()
        closing = lines[-1].strip()
        if not MARKDOWN_FENCE_RE.match(opening) or not MARKDOWN_FENCE_CLOSE_RE.match(closing):
            return normalized

        body_lines = lines[1:-1]
        if not body_lines or any(MARKDOWN_FENCE_RE.match(line.strip()) for line in body_lines):
            return normalized

        opening_info = re.sub(r"^\s*`{3,}", "", opening).strip()
        if not opening_info:
            return normalized

        prose_hits = sum(1 for line in body_lines if self._is_prose_line(line))
        if prose_hits < 2:
            return normalized
        return "\n".join(body_lines).strip()

    def _fence_embedded_diff_blocks(self, text: str) -> str:
        normalized = text.strip()
        if not normalized:
            return normalized

        lines = normalized.splitlines()
        output: list[str] = []
        idx = 0
        in_fence = False
        while idx < len(lines):
            line = lines[idx]
            if MARKDOWN_FENCE_RE.match(line):
                in_fence = not in_fence
                output.append(line)
                idx += 1
                continue

            if in_fence:
                output.append(line)
                idx += 1
                continue

            stripped = line.strip()
            if not DIFF_HEADER_RE.match(stripped):
                output.append(line)
                idx += 1
                continue

            start = idx
            idx += 1
            saw_hunk = stripped.startswith("@@")
            while idx < len(lines):
                line = lines[idx]
                if MARKDOWN_FENCE_RE.match(line):
                    break
                stripped = line.strip()
                if DIFF_HEADER_RE.match(stripped):
                    if stripped.startswith("@@"):
                        saw_hunk = True
                    idx += 1
                    continue
                if line.startswith((" ", "+", "-")):
                    idx += 1
                    continue
                if saw_hunk and (line.startswith("    ") or line.startswith("\t")):
                    # Some outputs drop the leading diff marker; keep indented code as part of the hunk.
                    idx += 1
                    continue
                if not stripped and saw_hunk:
                    idx += 1
                    continue
                break

            block = "\n".join(lines[start:idx]).strip()
            if not block:
                continue
            if not self._looks_like_unfenced_diff(block):
                output.extend(lines[start:idx])
                continue
            if output and output[-1]:
                output.append("")
            output.append("```diff")
            output.extend(lines[start:idx])
            output.append("```")

        return "\n".join(output).strip()

    def _retag_fenced_diff_blocks(self, text: str) -> str:
        normalized = text.strip()
        if not normalized or "```" not in normalized:
            return normalized

        lines = normalized.splitlines()
        output: list[str] = []
        idx = 0
        while idx < len(lines):
            line = lines[idx]
            if not MARKDOWN_FENCE_RE.match(line):
                output.append(line)
                idx += 1
                continue

            opening = line.strip()
            body: list[str] = []
            idx += 1
            while idx < len(lines) and not MARKDOWN_FENCE_RE.match(lines[idx]):
                body.append(lines[idx])
                idx += 1
            closing = lines[idx] if idx < len(lines) else "```"
            if idx < len(lines):
                idx += 1

            body_text = "\n".join(body).strip()
            if body_text and self._looks_like_unfenced_diff(body_text):
                opening = "```diff"

            output.append(opening)
            output.extend(body)
            output.append(closing)

        return "\n".join(output).strip()

    def _ensure_diff_fence(self, text: str) -> str:
        normalized = text.strip()
        if not normalized or "```" in normalized:
            return normalized
        if self._looks_like_unfenced_diff(normalized):
            return f"```diff\n{normalized}\n```"
        return normalized

    def _normalize_preview_content(self, text: str) -> str:
        normalized = self._convert_apply_patch_sections(text.strip())
        normalized = self._strip_accidental_outer_fence(normalized)
        normalized = self._retag_fenced_diff_blocks(normalized)
        normalized = self._fence_embedded_diff_blocks(normalized)
        return self._ensure_diff_fence(normalized)

    @staticmethod
    def _looks_like_shell_command_line(line: str) -> bool:
        stripped = line.strip()
        if not stripped:
            return False
        if stripped.startswith(("$ ", "# ", "> ")):
            return True

        command = stripped.split()[0]
        if command.endswith(":"):
            return False

        common_shell_commands = {
            "bash",
            "sh",
            "zsh",
            "python",
            "python3",
            "pip",
            "uv",
            "git",
            "npm",
            "pnpm",
            "yarn",
            "go",
            "cargo",
            "docker",
            "kubectl",
            "ls",
            "cat",
            "cp",
            "mv",
            "rm",
            "mkdir",
            "sed",
            "awk",
            "grep",
            "rg",
            "curl",
            "wget",
            "chmod",
            "chown",
        }
        if command in common_shell_commands:
            return True

        if re.fullmatch(r"(?:\./|\.\./|/)?[A-Za-z0-9._/-]+", command) and len(command) >= 2:
            if any(ch in stripped for ch in ("|", "&&", "||", ";", ">", "<")):
                return True
            if len(stripped.split()) >= 2:
                return True
        return False

    def _format_exec_section(self, content: str) -> str:
        normalized = self._normalize_preview_content(content)
        normalized = self._strip_leading_command_echo(normalized)
        if not normalized:
            return ""
        if "```" in normalized:
            return normalized

        lines = [line for line in normalized.splitlines() if line.strip()]
        if lines and self._looks_like_shell_command_line(lines[0]):
            return f"```bash\n{normalized}\n```"
        return f"```\n{normalized}\n```"

    def _strip_leading_command_echo(self, content: str) -> str:
        lines = content.splitlines()
        first_nonempty = next((idx for idx, line in enumerate(lines) if line.strip()), None)
        if first_nonempty is None:
            return ""

        tail = lines[first_nonempty:]
        nonempty_count = sum(1 for line in tail if line.strip())
        if nonempty_count < 2:
            return "\n".join(tail).strip()

        if self._looks_like_shell_command_line(tail[0]):
            stripped_tail = "\n".join(tail[1:]).strip()
            if stripped_tail:
                return stripped_tail
        return "\n".join(tail).strip()

    @staticmethod
    def _strip_thinking_echo_lines(content: str) -> str:
        lines = content.splitlines()
        kept = [line for line in lines if line.strip().lower() not in {"thinking", "thinking..."}]
        return "\n".join(kept).strip()

    @staticmethod
    def _patch_header_lines(old_path: str, new_path: str) -> list[str]:
        old_ref = "/dev/null" if old_path == "/dev/null" else f"a/{old_path}"
        new_ref = "/dev/null" if new_path == "/dev/null" else f"b/{new_path}"
        return [
            f"diff --git {old_ref} {new_ref}",
            f"--- {old_ref}",
            f"+++ {new_ref}",
        ]

    def _convert_apply_patch_block(self, lines: list[str]) -> list[str]:
        output: list[str] = ["```diff"]
        idx = 0
        has_diff_content = False

        while idx < len(lines):
            stripped = lines[idx].strip()

            old_path: Optional[str] = None
            new_path: Optional[str] = None
            if stripped.startswith(PATCH_UPDATE_PREFIX):
                old_path = stripped[len(PATCH_UPDATE_PREFIX) :].strip()
                new_path = old_path
                idx += 1
                if idx < len(lines):
                    move_line = lines[idx].strip()
                    if move_line.startswith(PATCH_MOVE_PREFIX):
                        new_path = move_line[len(PATCH_MOVE_PREFIX) :].strip() or old_path
                        idx += 1
            elif stripped.startswith(PATCH_ADD_PREFIX):
                new_path = stripped[len(PATCH_ADD_PREFIX) :].strip()
                old_path = "/dev/null"
                idx += 1
            elif stripped.startswith(PATCH_DELETE_PREFIX):
                old_path = stripped[len(PATCH_DELETE_PREFIX) :].strip()
                new_path = "/dev/null"
                idx += 1
            else:
                idx += 1
                continue

            if not old_path or not new_path:
                continue

            has_diff_content = True
            if len(output) > 1:
                output.append("")
            output.extend(self._patch_header_lines(old_path, new_path))

            while idx < len(lines):
                body_line = lines[idx]
                body_stripped = body_line.strip()
                if (
                    body_stripped.startswith(PATCH_UPDATE_PREFIX)
                    or body_stripped.startswith(PATCH_ADD_PREFIX)
                    or body_stripped.startswith(PATCH_DELETE_PREFIX)
                ):
                    break
                if body_stripped == PATCH_END_OF_FILE_MARKER:
                    idx += 1
                    continue
                if body_line.startswith(("@@", "+", "-", " ")):
                    output.append(body_line)
                idx += 1

        output.append("```")
        return output if has_diff_content else []

    def _convert_apply_patch_sections(self, text: str) -> str:
        if PATCH_BEGIN_MARKER not in text:
            return text

        lines = text.splitlines()
        output: list[str] = []
        idx = 0
        while idx < len(lines):
            stripped = lines[idx].strip()
            if stripped != PATCH_BEGIN_MARKER:
                output.append(lines[idx])
                idx += 1
                continue

            idx += 1
            patch_lines: list[str] = []
            while idx < len(lines) and lines[idx].strip() != PATCH_END_MARKER:
                patch_lines.append(lines[idx])
                idx += 1
            if idx < len(lines) and lines[idx].strip() == PATCH_END_MARKER:
                idx += 1

            converted = self._convert_apply_patch_block(patch_lines)
            if converted:
                output.extend(converted)
            else:
                output.append(PATCH_BEGIN_MARKER)
                output.extend(patch_lines)
                output.append(PATCH_END_MARKER)

        return "\n".join(output).strip()

    @staticmethod
    def _latest_section_from_sections(sections: list[TraceSection], markers: set[str]) -> Optional[TraceSection]:
        for section in reversed(sections):
            if section.marker in markers and section.content:
                return section
        return None

    def _sanitize_output_for_preview(self, cleaned: str, status: str) -> str:
        lines = cleaned.splitlines()
        sections = self._parse_trace_sections(lines)
        if status == "Running":
            latest_section = self._latest_section_from_sections(sections, {"assistant", "codex", "thinking", "exec"})
            if latest_section:
                marker = latest_section.marker
                content = latest_section.content
                if marker in {"assistant", "codex"}:
                    return self._normalize_preview_content(content)
                if marker == "thinking":
                    thinking_content = self._strip_thinking_echo_lines(content)
                    if thinking_content:
                        return f"thinking\n{thinking_content}"
                    return "thinking..."
                if marker == "exec":
                    return self._format_exec_section(content)
            return ""

        assistant_section = self._latest_section_from_sections(sections, {"assistant", "codex"})
        if assistant_section:
            return self._normalize_preview_content(assistant_section.content)
        exec_section = self._latest_section_from_sections(sections, {"exec"})
        if exec_section:
            return self._format_exec_section(exec_section.content)

        filtered: list[str] = []
        index = 0
        in_banner = False
        banner_rule_count = 0

        while index < len(lines):
            line = lines[index]
            stripped = line.strip()
            lowered = stripped.lower()

            if stripped.startswith("OpenAI Codex v"):
                in_banner = True
                banner_rule_count = 0
                index += 1
                continue

            if in_banner:
                if PREVIEW_DIVIDER_RE.match(stripped):
                    banner_rule_count += 1
                    if banner_rule_count >= 2:
                        in_banner = False
                index += 1
                continue

            if lowered.startswith("tokens used"):
                if status != "Running":
                    break
                index += 1
                continue

            marker = self._normalize_trace_marker(line)
            if marker in TRACE_SKIP_SECTION_MARKERS:
                index += 1
                while index < len(lines):
                    next_line = lines[index]
                    next_marker = self._normalize_trace_marker(next_line)
                    if next_marker is not None or next_line.strip().lower().startswith("tokens used"):
                        break
                    index += 1
                continue

            if self._is_preview_noise_line(line):
                index += 1
                continue

            filtered.append(line)
            index += 1

        sanitized = "\n".join(filtered).strip()
        if not sanitized:
            return ""
        return self._normalize_preview_content(sanitized)

    @staticmethod
    def _format_inline_markup(text: str) -> str:
        def render_plain(chunk: str) -> str:
            escaped = html.escape(chunk)
            escaped = re.sub(r"\*\*([^\n*]+)\*\*", r"<b>\1</b>", escaped)
            escaped = re.sub(r"\*([^\n*]+)\*", r"<i>\1</i>", escaped)
            escaped = re.sub(r"~~([^\n~]+)~~", r"<s>\1</s>", escaped)
            return escaped

        chunks = re.split(r"(`[^`\n]+`)", text)
        rendered: list[str] = []
        for chunk in chunks:
            if len(chunk) >= 2 and chunk.startswith("`") and chunk.endswith("`"):
                rendered.append(f"<code>{html.escape(chunk[1:-1])}</code>")
            else:
                rendered.append(render_plain(chunk))
        return "".join(rendered)

    @staticmethod
    def _is_strong_code_line(line: str) -> bool:
        stripped = line.strip()
        if not stripped:
            return False
        if CODE_INDENT_RE.match(line):
            return True
        if SHELL_PROMPT_RE.match(line):
            return True
        if CODE_KEYWORD_RE.match(stripped):
            return True
        if stripped in {"{", "}", "[", "]", "()", "[]", "{}", "};"}:
            return True
        return False

    @staticmethod
    def _line_looks_like_code(line: str) -> bool:
        stripped = line.strip()
        if not stripped:
            return False
        if MARKDOWN_HEADING_RE.match(line) or MARKDOWN_BULLET_RE.match(line) or MARKDOWN_ORDERED_RE.match(line):
            return False
        if stripped.startswith(">"):
            return False
        if Bridge._is_strong_code_line(line):
            return True

        token_hits = sum(
            token in stripped for token in ("{", "}", "=>", "->", "::", "()", "[]", "==", "!=", "<=", ">=", " = ", ";")
        )
        if token_hits >= 2:
            return True

        if stripped.startswith(("SELECT ", "INSERT ", "UPDATE ", "DELETE ", "CREATE ", "ALTER ", "DROP ")):
            return True

        if stripped.startswith(("git ", "npm ", "pnpm ", "yarn ", "pip ", "python ", "go ", "cargo ", "kubectl ")):
            return True
        return False

    def _should_start_auto_code_block(self, lines: list[str], index: int) -> bool:
        if not self._line_looks_like_code(lines[index]):
            return False
        if self._is_strong_code_line(lines[index]):
            return True
        prev_is_code = index > 0 and self._line_looks_like_code(lines[index - 1])
        next_is_code = index + 1 < len(lines) and self._line_looks_like_code(lines[index + 1])
        return prev_is_code or next_is_code

    def _render_preview_html(self, preview: str) -> str:
        if not preview:
            return "<i>暂无输出</i>"

        lines = preview.splitlines()
        html_lines: list[str] = []
        in_code_block = False
        code_lines: list[str] = []
        code_language = ""
        for line in lines:
            if in_code_block:
                if MARKDOWN_FENCE_CLOSE_RE.match(line):
                    code_body = "\n".join(code_lines)
                    if code_language:
                        html_lines.append(self._code_block_with_language(code_body, code_language))
                    else:
                        html_lines.append(self._code_block(code_body))
                    code_lines = []
                    code_language = ""
                    in_code_block = False
                else:
                    code_lines.append(line)
                continue

            if MARKDOWN_FENCE_RE.match(line):
                opening = line.strip()
                fence_info = re.sub(r"^\s*`{3,}", "", opening).strip()
                code_language = fence_info.split()[0] if fence_info else ""
                in_code_block = True
                continue

            stripped = line.strip()
            if not stripped:
                html_lines.append("")
                continue

            if MARKDOWN_RULE_RE.match(line):
                html_lines.append("———")
                continue

            heading_match = MARKDOWN_HEADING_RE.match(line)
            if heading_match:
                html_lines.append(f"<b>{self._format_inline_markup(heading_match.group(2).strip())}</b>")
                continue

            bullet_match = MARKDOWN_BULLET_RE.match(line)
            if bullet_match:
                html_lines.append(f"• {self._format_inline_markup(bullet_match.group(1).strip())}")
                continue

            ordered_match = MARKDOWN_ORDERED_RE.match(line)
            if ordered_match:
                number, content = ordered_match.groups()
                html_lines.append(f"{number}. {self._format_inline_markup(content.strip())}")
                continue

            if stripped.startswith(">"):
                quote_content = stripped.lstrip(">").strip()
                html_lines.append(f"<i>{self._format_inline_markup(quote_content)}</i>")
                continue

            html_lines.append(self._format_inline_markup(line))

        if code_lines:
            code_body = "\n".join(code_lines)
            if code_language:
                html_lines.append(self._code_block_with_language(code_body, code_language))
            else:
                html_lines.append(self._code_block(code_body))

        return "\n".join(html_lines).strip()

    def _build_preview(self, output: str, status: str) -> tuple[str, int, int, bool, int]:
        cleaned = self._clean_output(output)
        sanitized = self._sanitize_output_for_preview(cleaned, status) if cleaned else ""
        if not sanitized:
            waiting_text = "thinking..." if status == "Running" else "(无输出)"
            return waiting_text, 0, 0, False, 0

        lines = sanitized.splitlines()
        line_count = len(lines)
        char_count = sum(len(line) for line in lines) + max(0, line_count - 1)
        preview_lines = self._slice_preview_lines(lines, STREAM_PREVIEW_LINE_LIMIT)
        preview = self._format_preview_lines(preview_lines)
        clipped = line_count > STREAM_PREVIEW_LINE_LIMIT
        if len(preview) > STREAM_PREVIEW_LIMIT:
            preview = clip_for_telegram(preview, limit=STREAM_PREVIEW_LIMIT)
            clipped = True
        return preview, line_count, char_count, clipped, len(preview_lines)

    def _format_thinking_detail_html(self, detail: str, compact: bool = False) -> str:
        lines = [line.strip() for line in detail.splitlines() if line.strip()]
        if not lines:
            return ""

        max_lines = 1 if compact else THINKING_DETAIL_MAX_LINES
        max_chars = 90 if compact else THINKING_DETAIL_MAX_CHARS
        tail = lines[-max_lines:]
        clipped_tail = [clip_for_inline(line, limit=max_chars) for line in tail]

        rendered: list[str] = []
        if len(lines) > len(tail):
            rendered.append("<i>…</i>")
        rendered.extend(f"• {self._format_inline_markup(line)}" for line in clipped_tail)
        return "\n".join(rendered)

    @staticmethod
    def _format_elapsed_seconds(elapsed_seconds: float) -> str:
        total = max(0, int(elapsed_seconds))
        hours = total // 3600
        minutes = (total % 3600) // 60
        seconds = total % 60
        if hours:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    @staticmethod
    def _append_elapsed_footer(body_html: str, elapsed_text: str, compact: bool = False) -> str:
        footer = f"<i><code>{elapsed_text}</code></i>" if compact else f"<i>{elapsed_text}</i>"
        if not body_html.strip():
            return footer
        return f"{body_html}\n{footer}"

    def _format_stream_text(self, status: str, output: str, elapsed_seconds: float) -> str:
        preview, *_ = self._build_preview(output, status)
        preview_stripped = preview.strip()
        preview_lower = preview_stripped.lower()
        elapsed_text = self._format_elapsed_seconds(elapsed_seconds)

        if status == "Running" and (preview_lower == "thinking..." or preview_lower.startswith("thinking\n")):
            thinking_detail = ""
            if "\n" in preview_stripped:
                thinking_detail = preview_stripped.split("\n", 1)[1].strip()
            frame = THINKING_SPINNER_FRAMES[int(elapsed_seconds * 2) % len(THINKING_SPINNER_FRAMES)]
            dots = "." * (int(elapsed_seconds * 2) % 3 + 1)
            detail_html = self._format_thinking_detail_html(thinking_detail, compact=False)
            if detail_html:
                text = f"<i>{html.escape(frame)} thinking{dots}</i>\n{detail_html}"
            else:
                text = f"<i>{html.escape(frame)} thinking{dots}</i>"
            text = self._append_elapsed_footer(text, elapsed_text, compact=True)
            if len(text) <= TELEGRAM_MESSAGE_LIMIT:
                return text
            compact_detail_html = self._format_thinking_detail_html(thinking_detail, compact=True)
            if compact_detail_html:
                compact = f"<i>{html.escape(frame)} thinking{dots}</i>\n{compact_detail_html}"
                return self._append_elapsed_footer(compact, elapsed_text, compact=True)
            return self._append_elapsed_footer(f"<i>{html.escape(frame)} thinking{dots}</i>", elapsed_text, compact=True)

        render_preview = preview
        if self._looks_like_unfenced_diff(render_preview):
            render_preview = f"```diff\n{render_preview}\n```"
        preview_html = self._render_preview_html(render_preview)

        text = preview_html
        if status == "Running":
            text = self._append_elapsed_footer(preview_html, elapsed_text)

        if len(text) <= TELEGRAM_MESSAGE_LIMIT:
            return text

        # Safety fallback for Telegram max message length: reduce preview aggressively.
        hard_limit_preview = clip_for_telegram(preview, limit=900)
        hard_limit_render = hard_limit_preview
        if self._looks_like_unfenced_diff(hard_limit_render):
            hard_limit_render = f"```diff\n{hard_limit_render}\n```"
        hard_limit_html = self._render_preview_html(hard_limit_render)
        if status == "Running":
            return self._append_elapsed_footer(hard_limit_html, elapsed_text)
        return hard_limit_html

    @staticmethod
    def _diff_metrics(lines: list[str]) -> tuple[int, int, int, bool, str]:
        nonempty = 0
        first_nonempty = ""
        diff_header_hits = 0
        plus_count = 0
        minus_count = 0
        saw_hunk = False
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if not first_nonempty:
                first_nonempty = stripped
            nonempty += 1
            if stripped.startswith("diff --git "):
                diff_header_hits += 2
            elif stripped.startswith(
                (
                    "--- ",
                    "+++ ",
                    "@@",
                    "index ",
                    "new file mode ",
                    "deleted file mode ",
                    "rename from ",
                    "rename to ",
                    "similarity index ",
                    "old mode ",
                    "new mode ",
                )
            ):
                diff_header_hits += 1
                if stripped.startswith("@@"):
                    saw_hunk = True
            elif stripped.startswith("+") and not stripped.startswith("+++"):
                plus_count += 1
            elif stripped.startswith("-") and not stripped.startswith("---"):
                minus_count += 1
        return nonempty, diff_header_hits, plus_count + minus_count, saw_hunk, first_nonempty

    @staticmethod
    def _is_diff_candidate(lines: list[str]) -> bool:
        nonempty, diff_header_hits, diff_body_hits, saw_hunk, first_nonempty = Bridge._diff_metrics(lines)
        if nonempty == 0:
            return False

        if first_nonempty and not first_nonempty.startswith(
            (
                "diff --git ",
                "index ",
                "--- ",
                "+++ ",
                "@@",
                "new file mode ",
                "deleted file mode ",
                "rename from ",
                "rename to ",
            )
        ):
            if nonempty > 6:
                return False

        diff_lines = diff_header_hits + diff_body_hits
        density = diff_lines / nonempty
        if saw_hunk and diff_body_hits >= 2 and density >= 0.5:
            return True
        if diff_header_hits >= 3 and density >= 0.45 and (diff_body_hits >= 1 or saw_hunk):
            return True
        if diff_header_hits >= 2 and diff_body_hits >= 2 and density >= 0.5:
            return True
        if nonempty <= 10 and diff_header_hits >= 4 and density >= 0.4:
            return True
        return False

    @staticmethod
    def _candidate_diff_windows(lines: list[str]) -> list[list[str]]:
        windows: list[list[str]] = []
        if lines:
            windows.append(lines)
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith(
                (
                    "diff --git ",
                    "index ",
                    "--- ",
                    "+++ ",
                    "@@",
                    "new file mode ",
                    "deleted file mode ",
                    "rename from ",
                    "rename to ",
                )
            ):
                tail = lines[idx:]
                if len(tail) >= 3:
                    windows.append(tail)
        return windows

    @staticmethod
    def _looks_like_unfenced_diff(text: str) -> bool:
        if "```" in text:
            return False

        lines = [line.rstrip() for line in text.splitlines() if line.strip()]
        if not lines:
            return False

        for window in Bridge._candidate_diff_windows(lines):
            if Bridge._is_diff_candidate(window):
                return True
        return False

    @staticmethod
    def _split_plain_text_chunks(text: str, limit: int) -> list[str]:
        normalized = text.strip()
        if not normalized:
            return []
        if len(normalized) <= limit:
            return [normalized]

        chunks: list[str] = []
        remaining = normalized
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break

            split_at = remaining.rfind("\n\n", 0, limit)
            if split_at < int(limit * 0.5):
                split_at = remaining.rfind("\n", 0, limit)
            if split_at < int(limit * 0.3):
                split_at = limit

            chunk = remaining[:split_at].strip()
            if not chunk:
                chunk = remaining[:limit]
                split_at = len(chunk)

            chunks.append(chunk)
            remaining = remaining[split_at:].lstrip("\n")

        return chunks

    @staticmethod
    def _split_fenced_block_chunks(block: str, limit: int) -> list[str]:
        lines = block.splitlines()
        if len(lines) < 2:
            return Bridge._split_plain_text_chunks(block, limit)

        opening = lines[0]
        has_closing = bool(lines and MARKDOWN_FENCE_CLOSE_RE.match(lines[-1].strip()))
        closing = lines[-1] if has_closing else "```"
        body_lines = lines[1:-1] if has_closing else lines[1:]

        scaffold_len = len(opening) + len(closing) + 2
        if scaffold_len >= limit:
            return [block[:limit]]

        body_limit = limit - scaffold_len
        parts: list[str] = []
        current: list[str] = []
        current_len = 0

        def flush_current() -> None:
            nonlocal current, current_len
            if not current:
                return
            parts.append(f"{opening}\n" + "\n".join(current) + f"\n{closing}")
            current = []
            current_len = 0

        for line in body_lines:
            if len(line) > body_limit and not current:
                remaining_line = line
                while len(remaining_line) > body_limit:
                    piece = remaining_line[:body_limit]
                    parts.append(f"{opening}\n{piece}\n{closing}")
                    remaining_line = remaining_line[body_limit:]
                if remaining_line:
                    current = [remaining_line]
                    current_len = len(remaining_line)
                continue

            add_len = len(line) + (1 if current else 0)
            if current and current_len + add_len > body_limit:
                flush_current()
            current.append(line)
            current_len += len(line) + (1 if len(current) > 1 else 0)

        flush_current()
        return parts if parts else [f"{opening}\n{closing}"]

    @staticmethod
    def _split_output_chunks(text: str, limit: int = FINAL_OUTPUT_CHUNK_LIMIT) -> list[str]:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            return []
        if len(normalized) <= limit:
            return [normalized]

        segments: list[str] = []
        lines = normalized.splitlines()
        idx = 0
        while idx < len(lines):
            if MARKDOWN_FENCE_RE.match(lines[idx].strip()):
                start = idx
                idx += 1
                while idx < len(lines) and not MARKDOWN_FENCE_CLOSE_RE.match(lines[idx].strip()):
                    idx += 1
                if idx < len(lines):
                    idx += 1
                segment = "\n".join(lines[start:idx]).strip()
                if segment:
                    segments.append(segment)
                continue

            start = idx
            while idx < len(lines) and not MARKDOWN_FENCE_RE.match(lines[idx].strip()):
                idx += 1
            segment = "\n".join(lines[start:idx]).strip()
            if segment:
                segments.append(segment)

        expanded: list[str] = []
        for segment in segments:
            if len(segment) <= limit:
                expanded.append(segment)
                continue
            if segment.startswith("```"):
                expanded.extend(Bridge._split_fenced_block_chunks(segment, limit))
            else:
                expanded.extend(Bridge._split_plain_text_chunks(segment, limit))

        chunks: list[str] = []
        current = ""
        for segment in expanded:
            if not current:
                if len(segment) <= limit:
                    current = segment
                else:
                    chunks.extend(Bridge._split_plain_text_chunks(segment, limit))
                continue

            candidate = f"{current}\n\n{segment}"
            if len(candidate) <= limit:
                current = candidate
                continue

            chunks.append(current)
            if len(segment) <= limit:
                current = segment
            else:
                split_parts = Bridge._split_plain_text_chunks(segment, limit)
                if split_parts:
                    chunks.extend(split_parts[:-1])
                    current = split_parts[-1]
                else:
                    current = ""

        if current:
            chunks.append(current)
        return chunks

    def _prune_page_sessions(self) -> None:
        now = time.time()
        stale_keys = [
            key
            for key, session in self.page_sessions.items()
            if now - session.last_access > PAGE_SESSION_TTL_SECONDS
        ]
        if not stale_keys:
            return
        for key in stale_keys:
            self.page_sessions.pop(key, None)
        self._save_page_sessions()

    @staticmethod
    def _page_callback_data(message_id: int, index: int) -> str:
        return f"page:{message_id}:{index}"

    def _build_page_keyboard(self, message_id: int, index: int, total: int) -> Optional[InlineKeyboardMarkup]:
        if total <= 1:
            return None
        buttons: list[InlineKeyboardButton] = []
        if index > 0:
            buttons.append(InlineKeyboardButton("‹ Prev", callback_data=self._page_callback_data(message_id, index - 1)))
        if index < total - 1:
            buttons.append(InlineKeyboardButton("Next ›", callback_data=self._page_callback_data(message_id, index + 1)))
        if not buttons:
            return None
        return InlineKeyboardMarkup([buttons])

    def _render_paginated_html(self, content: str, index: int, total: int) -> str:
        page_html = self._render_preview_html(content)
        if total <= 1:
            return page_html
        indicator = f"<i>Page {index + 1}/{total}</i>"
        return f"{indicator}\n{page_html}" if page_html else indicator

    async def _send_final_output_messages(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        message_id: int,
        cleaned_output: str,
    ) -> None:
        preview_text = self._sanitize_output_for_preview(cleaned_output, "Done") if cleaned_output else ""
        if not preview_text:
            await self.safe_edit(
                context,
                chat_id,
                message_id,
                "<i>暂无输出</i>",
                disable_web_page_preview=False,
            )
            return

        chunks = self._split_output_chunks(preview_text, FINAL_OUTPUT_CHUNK_LIMIT)
        if not chunks:
            await self.safe_edit(
                context,
                chat_id,
                message_id,
                "<i>暂无输出</i>",
                disable_web_page_preview=False,
            )
            return

        self._prune_page_sessions()
        session_key = (chat_id, message_id)
        if len(chunks) > 1:
            now = time.time()
            self.page_sessions[session_key] = PageSession(
                chat_id=chat_id,
                message_id=message_id,
                pages=chunks,
                created_at=now,
                last_access=now,
                current_index=0,
            )
            self._save_page_sessions()
        else:
            if session_key in self.page_sessions:
                self.page_sessions.pop(session_key, None)
                self._save_page_sessions()
        first_html = self._render_paginated_html(chunks[0], 0, len(chunks))
        reply_markup = self._build_page_keyboard(message_id, 0, len(chunks))
        await self.safe_edit(
            context,
            chat_id,
            message_id,
            first_html,
            reply_markup=reply_markup,
            disable_web_page_preview=False,
        )

    def _output_file_name(self, chat_id: int, message_id: int) -> str:
        timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        return f"codex-output-{chat_id}-{message_id}-{timestamp}.txt"

    def _should_upload_output_file(self, cleaned_output: str) -> bool:
        if not self.settings.enable_output_file:
            return False
        stripped = cleaned_output.strip()
        if not stripped:
            return False
        return len(stripped) > OUTPUT_FILE_MIN_CHARS

    def _write_output_file(self, chat_id: int, message_id: int, cleaned_output: str) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.output_dir, 0o700)
        except OSError:
            pass

        file_name = self._output_file_name(chat_id, message_id)
        output_path = self.output_dir / file_name
        output_text = cleaned_output if cleaned_output else "(empty output)"
        output_path.write_text(output_text, encoding="utf-8")
        try:
            os.chmod(output_path, 0o600)
        except OSError:
            pass
        return output_path

    async def _upload_output_file(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        output_path: Path,
    ) -> None:
        with output_path.open("rb") as fh:
            await context.bot.send_document(
                chat_id=chat_id,
                document=InputFile(fh, filename=output_path.name),
                caption=f"完整输出文件: {output_path.name}",
            )

    def _is_duplicate_request(self, chat_id: int, message_id: int) -> bool:
        now = time.monotonic()
        stale_keys = [key for key, seen_at in self.recent_requests.items() if now - seen_at > REQUEST_DEDUP_SECONDS]
        for key in stale_keys:
            self.recent_requests.pop(key, None)
        dedup_key = (chat_id, message_id)
        if dedup_key in self.recent_requests:
            return True
        self.recent_requests[dedup_key] = now
        return False

    async def safe_edit(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        disable_web_page_preview: bool = True,
    ) -> None:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=disable_web_page_preview,
                reply_markup=reply_markup,
                read_timeout=30,
                write_timeout=30,
                connect_timeout=30,
                pool_timeout=5,
            )
        except BadRequest as err:
            if "Message is not modified" not in str(err):
                raise
        except TelegramError:
            # Telegram API network/transient errors should not fail the whole task.
            return

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None:
            return
        if not self._is_update_authorized(update):
            await self.send_html(update, "<b>Access denied</b>")
            return
        auth_status = "ON" if self._is_second_factor_enabled() else "OFF"
        await self.send_html(
            update,
            "<b>tg-codex</b>\n"
            "Send plain text directly to execute a task.\n\n"
            "Send an image (optional caption) to run an image prompt.\n\n"
            "<b>Commands</b>\n"
            "- <code>/cmd</code> show current command prefix\n"
            "- <code>/cmd &lt;new prefix&gt;</code> update command prefix\n"
            "- <code>/cmd reset</code> restore default command\n"
            "- <code>/id</code> show current chat/user id\n"
            "- <code>/auth &lt;passphrase&gt;</code> unlock execution\n"
            "- <code>/new</code> start a fresh Codex session\n"
            "- <code>/cwd</code> show or change working directory\n"
            "- <code>/skill</code> list installed Codex skills\n"
            "- <code>/skill &lt;name&gt;</code> show skill details\n"
            "- <code>/setting</code> show or update runtime settings\n"
            "- <code>/status</code> show current task status\n"
            "- <code>/cancel</code> stop current task\n"
            f"\nCommand override: <b>{'ON' if self.settings.allow_cmd_override else 'OFF'}</b> (admin user + admin chat)"
            f"\nSecond-factor auth: <b>{auth_status}</b>"
            "\n\nPlain text mode: <b>ON</b> (all non-<code>/xxx</code> text will run as prompt).",
        )

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None:
            return
        chat_id = update.effective_chat.id
        if not self._is_update_authorized(update):
            await self.send_html(update, "<b>Access denied</b>")
            return
        task = self.tasks.get(chat_id)
        mode = "enabled"
        output_file_mode = "enabled" if self.settings.enable_output_file else "disabled"
        resume_mode = "enabled" if self.settings.enable_session_resume else "disabled"
        session_id = self.chat_sessions.get(chat_id, "")
        session_text = self._code_inline(session_id) if session_id else "<code>(new)</code>"
        workdir_text = self._code_inline(str(self._effective_workdir(chat_id)))
        display_prefix = self._redacted_command_text(self.codex_prefix)
        if self._is_second_factor_enabled():
            auth_left = self._auth_seconds_left(update)
            auth_state = f"authenticated ({auth_left}s left)" if auth_left > 0 else "locked"
        else:
            auth_state = "disabled"
        if task and not task.done():
            await self.send_html(
                update,
                "<b>Task Status</b>\n"
                "State: <b>Running</b>\n"
                f"Command:\n{self._code_block(display_prefix)}\n"
                f"Workdir: {workdir_text}\n"
                f"Plain text mode: <b>{mode}</b>\n"
                f"Output file upload: <b>{output_file_mode}</b>\n"
                f"Session resume: <b>{resume_mode}</b>\n"
                f"Second-factor: <b>{auth_state}</b>\n"
                f"Session: {session_text}",
            )
        else:
            await self.send_html(
                update,
                "<b>Task Status</b>\n"
                "State: <b>Idle</b>\n"
                f"Command:\n{self._code_block(display_prefix)}\n"
                f"Workdir: {workdir_text}\n"
                f"Plain text mode: <b>{mode}</b>\n"
                f"Output file upload: <b>{output_file_mode}</b>\n"
                f"Session resume: <b>{resume_mode}</b>\n"
                f"Second-factor: <b>{auth_state}</b>\n"
                f"Session: {session_text}",
            )

    async def paginate(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.message is None:
            return
        if not self._is_update_authorized(update):
            await query.answer("Access denied", show_alert=False)
            return

        data = query.data or ""
        match = re.fullmatch(r"page:(\d+):(\d+)", data)
        if not match:
            await query.answer()
            return

        message_id = int(match.group(1))
        index = int(match.group(2))
        chat_id = query.message.chat.id
        actual_message_id = query.message.message_id
        if actual_message_id != message_id:
            message_id = actual_message_id

        self._prune_page_sessions()
        session = self.page_sessions.get((chat_id, message_id))
        if session is None:
            await query.answer("Expired", show_alert=False)
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=actual_message_id,
                    reply_markup=None,
                )
            except BadRequest:
                pass
            return

        if index < 0 or index >= len(session.pages):
            await query.answer()
            return

        session.current_index = index
        session.last_access = time.time()
        self._save_page_sessions()
        page_html = self._render_paginated_html(session.pages[index], index, len(session.pages))
        reply_markup = self._build_page_keyboard(message_id, index, len(session.pages))
        await self.safe_edit(
            context,
            chat_id,
            message_id,
            page_html,
            reply_markup=reply_markup,
            disable_web_page_preview=False,
        )
        await query.answer()

    async def chat_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None:
            return
        if not self._is_update_authorized(update):
            await self.send_html(update, "<b>Access denied</b>")
            return
        user_id = update.effective_user.id if update.effective_user else None
        user_line = self._code_inline(str(user_id)) if user_id is not None else "<code>(unknown)</code>"
        await self.send_html(
            update,
            "<b>IDs</b>\n"
            f"Chat: {self._code_inline(str(update.effective_chat.id))}\n"
            f"User: {user_line}",
        )

    async def auth(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None:
            return
        if not self._is_update_authorized(update):
            await self.send_html(update, "<b>Access denied</b>")
            return
        if not self._is_second_factor_enabled():
            await self.send_html(
                update,
                "<b>/auth disabled</b>\nSet <code>TG_AUTH_PASSPHRASE</code> to enable second-factor auth.",
            )
            return

        key = self._auth_key(update)
        if key is None:
            await self.send_html(update, "<b>Auth failed</b>\nCannot resolve user identity.")
            return

        raw = " ".join(context.args).strip()
        if not raw:
            seconds_left = self._auth_seconds_left(update)
            if seconds_left > 0:
                await self.send_html(
                    update,
                    "<b>Already authenticated</b>\n"
                    f"Remaining: <code>{seconds_left}s</code>.",
                )
            else:
                await self.send_html(
                    update,
                    "<b>Usage</b>\n<code>/auth &lt;passphrase&gt;</code>\n"
                    f"Session TTL: <code>{self.settings.auth_ttl_seconds}s</code>.",
                )
            return

        if raw.lower() in {"logout", "revoke"}:
            self.auth_sessions.pop(key, None)
            await self.send_html(update, "<b>Authentication cleared</b>")
            return

        if hmac.compare_digest(raw, self.settings.auth_passphrase):
            self.auth_sessions[key] = time.monotonic() + self.settings.auth_ttl_seconds
            await self.send_html(
                update,
                "<b>Authentication successful</b>\n"
                f"Valid for <code>{self.settings.auth_ttl_seconds}s</code>.",
            )
            return

        await self.send_html(update, "<b>Authentication failed</b>")

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None:
            return
        chat_id = update.effective_chat.id
        if not self._is_update_authorized(update):
            await self.send_html(update, "<b>Access denied</b>")
            return
        task = self.tasks.get(chat_id)
        if task and not task.done():
            task.cancel()
            await self.send_html(update, "<b>Cancellation requested</b>\nCurrent task is stopping.")
        else:
            await self.send_html(update, "<b>No running task</b>")

    async def new_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None:
            return
        chat_id = update.effective_chat.id
        if not self._is_update_authorized(update):
            await self.send_html(update, "<b>Access denied</b>")
            return
        if not await self._ensure_second_factor(update):
            return
        task = self.tasks.get(chat_id)
        if task and not task.done():
            await self.send_html(update, "<b>Task is running</b>\nUse <code>/cancel</code> first, then run <code>/new</code>.")
            return

        existed = self._clear_chat_session(chat_id)
        if existed:
            await self.send_html(
                update,
                "<b>Session reset</b>\nNext plain text message will start a fresh Codex session.",
            )
            return
        await self.send_html(
            update,
            "<b>Already fresh</b>\nNo previous session found. Next plain text message will start a fresh Codex session.",
        )

    async def cwd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None:
            return
        chat_id = update.effective_chat.id
        if not self._is_update_authorized(update):
            await self.send_html(update, "<b>Access denied</b>")
            return
        if not await self._ensure_second_factor(update):
            return

        raw = " ".join(context.args).strip()
        if not raw:
            current = self._effective_workdir(chat_id)
            await self.send_html(
                update,
                "<b>Working directory</b>\n"
                f"Current: {self._code_inline(str(current))}\n\n"
                "<b>Usage</b>\n"
                "<code>/cwd &lt;path&gt;</code>\n"
                "<code>/cwd reset</code>",
            )
            return

        if raw.lower() == "reset":
            existed = self._clear_chat_workdir(chat_id)
            current = self._effective_workdir(chat_id)
            if existed:
                await self.send_html(
                    update,
                    "<b>Working directory reset</b>\n"
                    f"Current: {self._code_inline(str(current))}",
                )
            else:
                await self.send_html(
                    update,
                    "<b>Already default</b>\n"
                    f"Current: {self._code_inline(str(current))}",
                )
            return

        try:
            target = self._resolve_target_workdir(chat_id, raw)
        except ValueError as err:
            await self.send_html(update, f"<b>Invalid directory</b>\n{self._code_inline(str(err))}")
            return

        self._set_chat_workdir(chat_id, target)
        await self.send_html(
            update,
            "<b>Working directory updated</b>\n"
            f"Current: {self._code_inline(str(target))}",
        )

    async def skill(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None:
            return
        if not self._is_update_authorized(update):
            await self.send_html(update, "<b>Access denied</b>")
            return

        raw_query = " ".join(context.args).strip()
        skills_root, installed = self._discover_installed_skills()

        if not installed:
            await self.send_html(
                update,
                "<b>Codex Skills</b>\n"
                "No installed skills found.\n"
                f"Path: {self._code_inline(str(skills_root))}",
            )
            return

        if not raw_query:
            custom_skills = [item for item in installed if not item.is_system]
            system_skills = [item for item in installed if item.is_system]
            lines = [
                "<b>Codex Skills</b>",
                f"Root: {self._code_inline(str(skills_root))}",
                (
                    f"Installed: <b>{len(installed)}</b> "
                    f"(custom: <b>{len(custom_skills)}</b>, system: <b>{len(system_skills)}</b>)"
                ),
                "",
            ]
            if custom_skills:
                lines.append("<b>Custom Skills</b>")
                lines.extend(self._format_skill_name_lines(custom_skills))
                lines.append("")
            if system_skills:
                lines.append("<b>System Skills</b>")
                lines.extend(self._format_skill_name_lines(system_skills, start_index=len(custom_skills) + 1))
                lines.append("")
            lines.append("Tip: use <code>/skill &lt;name&gt;</code> for details.")
            await self.send_html(update, "\n".join(lines).strip())
            return

        query = raw_query.lower()
        exact_matches = [item for item in installed if item.name.lower() == query]
        matches = exact_matches or [item for item in installed if query in item.name.lower()]
        if not matches:
            matches = [item for item in installed if query in item.description.lower()]

        if not matches:
            await self.send_html(
                update,
                "<b>Skill not found</b>\n"
                f"Query: {self._code_inline(raw_query)}\n"
                "Use <code>/skill</code> to view all installed skills.",
            )
            return

        if len(matches) == 1:
            skill = matches[0]
            description = html.escape(self._truncate_text(skill.description, limit=600))
            await self.send_html(
                update,
                "<b>Skill Details</b>\n"
                f"Name: {self._code_inline(skill.name)}\n"
                f"Category: <b>{'system' if skill.is_system else 'custom'}</b>\n"
                f"Path: {self._code_inline(str(skill.skill_md))}\n"
                f"Description: {description}",
            )
            return

        lines = [
            "<b>Skill Matches</b>",
            f"Query: {self._code_inline(raw_query)}",
            f"Matched: <b>{len(matches)}</b>",
            "",
        ]
        max_matches = 25
        shown_matches = matches[:max_matches]
        for idx, skill in enumerate(shown_matches, start=1):
            short_desc = html.escape(self._truncate_text(skill.description, limit=88))
            lines.append(f"{idx}. <code>{html.escape(skill.name)}</code> - {short_desc}")
        if len(matches) > max_matches:
            lines.append(f"... and <b>{len(matches) - max_matches}</b> more matches.")
        lines.append("")
        lines.append("Try a full name: <code>/skill &lt;exact-name&gt;</code>")
        await self.send_html(update, "\n".join(lines).strip())

    async def run(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_message is None:
            return
        await self._run_prompt(update, context, " ".join(context.args).strip())

    async def run_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_message is None:
            return
        prompt = (update.effective_message.text or "").strip()
        if not prompt:
            return
        await self._run_prompt(update, context, prompt)

    @staticmethod
    def _normalize_suffix(suffix: str) -> str:
        normalized = suffix.lower().strip()
        if re.fullmatch(r"\.[a-z0-9]{1,8}", normalized):
            return normalized
        return ".jpg"

    @staticmethod
    def _build_image_prompt(image_path: Path, caption: str) -> str:
        request = caption if caption else "Please analyze this image."
        return (
            "Use the local image file below as input context.\n"
            f"Image path: {image_path.resolve()}\n\n"
            f"User request:\n{request}"
        )

    async def run_image(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None or update.effective_message is None:
            return
        chat_id = update.effective_chat.id
        if not self._is_update_authorized(update):
            await self.send_html(update, "<b>Access denied</b>")
            return

        message = update.effective_message
        file_id: Optional[str] = None
        suffix = ".jpg"

        if message.photo:
            file_id = message.photo[-1].file_id
            file_size = message.photo[-1].file_size or 0
        elif message.document and (message.document.mime_type or "").startswith("image/"):
            file_id = message.document.file_id
            file_size = message.document.file_size or 0
            if message.document.file_name:
                suffix = self._normalize_suffix(Path(message.document.file_name).suffix)
            else:
                guessed = mimetypes.guess_extension(message.document.mime_type or "")
                if guessed:
                    suffix = self._normalize_suffix(guessed)
        else:
            file_size = 0

        if not file_id:
            return
        if file_size > self.settings.max_image_bytes:
            await self.send_html(
                update,
                "<b>Image rejected</b>\n"
                f"File too large: {self._code_inline(str(file_size))} bytes "
                f"(limit: {self._code_inline(str(self.settings.max_image_bytes))})",
            )
            return

        try:
            tg_file = await context.bot.get_file(file_id)
            if suffix == ".jpg" and tg_file.file_path:
                suffix = self._normalize_suffix(Path(tg_file.file_path).suffix)
            self.media_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.media_dir, 0o700)
            except OSError:
                pass
            image_path = self.media_dir / f"tg-{chat_id}-{message.message_id}-{uuid.uuid4().hex[:8]}{suffix}"
            await tg_file.download_to_drive(custom_path=str(image_path))
            try:
                os.chmod(image_path, 0o600)
            except OSError:
                pass
        except Exception as err:
            await self.send_html(update, f"<b>Image download failed</b>\nReason: {self._code_inline(str(err))}")
            return

        caption = (message.caption or "").strip()
        prompt = self._build_image_prompt(image_path, caption)
        await self._run_prompt(update, context, prompt, cleanup_paths=[image_path])

    async def cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None or update.effective_message is None:
            return
        if not self._is_update_authorized(update, require_admin=True):
            await self.send_html(update, "<b>Access denied</b>")
            return
        if not await self._ensure_second_factor(update):
            return

        low_prefix = "codex -a never --search exec -s workspace-write --skip-git-repo-check"
        readonly_prefix = "codex -a never --search exec -s read-only --skip-git-repo-check"
        high_prefix = "codex -a never --search exec -s danger-full-access --skip-git-repo-check"
        preset_aliases = {
            "low": ("LOW", low_prefix),
            "readonly": ("READONLY", readonly_prefix),
            "ro": ("READONLY", readonly_prefix),
            "high": ("HIGH", high_prefix),
        }

        raw = " ".join(context.args).strip()
        if not raw:
            display_prefix = self._redacted_command_text(self.codex_prefix)
            await self.send_html(
                update,
                "<b>Current command prefix</b>\n"
                f"{self._code_block(display_prefix)}\n"
                "<b>Permission profiles</b>\n"
                f"LOW (recommended): {self._code_inline(low_prefix)}\n"
                f"READONLY (audit/review): {self._code_inline(readonly_prefix)}\n"
                f"HIGH (danger-full-access): {self._code_inline(high_prefix)}\n\n"
                "<b>Usage</b>\n"
                "<code>/cmd &lt;command prefix&gt;</code>\n"
                "<code>/cmd low</code> / <code>/cmd readonly</code> / <code>/cmd high</code>\n"
                "<code>/cmd reset</code>\n"
                f"Override enabled: <b>{'yes' if self.settings.allow_cmd_override else 'no'}</b>",
            )
            return
        if not self.settings.allow_cmd_override:
            await self.send_html(update, "<b>Command override disabled</b>\nSet <code>TG_ALLOW_CMD_OVERRIDE=1</code> to enable.")
            return

        if raw.lower() == "reset":
            self.codex_prefix = self.default_codex_prefix
            display_prefix = self._redacted_command_text(self.codex_prefix)
            await self.send_html(
                update,
                "<b>Command prefix reset</b>\n"
                f"{self._code_block(display_prefix)}",
            )
            return

        preset = preset_aliases.get(raw.lower())
        if preset is not None:
            level, preset_prefix = preset
            self.codex_prefix = preset_prefix
            display_prefix = self._redacted_command_text(self.codex_prefix)
            await self.send_html(
                update,
                f"<b>Command prefix switched to {level}</b>\n"
                f"{self._code_block(display_prefix)}",
            )
            return

        try:
            _validate_codex_prefix(raw)
        except ValueError as err:
            await self.send_html(update, f"<b>Invalid command prefix</b>\n{self._code_inline(str(err))}")
            return
        self.codex_prefix = raw
        display_prefix = self._redacted_command_text(self.codex_prefix)
        await self.send_html(
            update,
            "<b>Command prefix updated</b>\n"
            f"{self._code_block(display_prefix)}",
        )

    async def setting(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None or update.effective_message is None:
            return
        if not self._is_update_authorized(update, require_admin=True):
            await self.send_html(update, "<b>Access denied</b>")
            return
        if not await self._ensure_second_factor(update):
            return

        raw = " ".join(context.args).strip()
        if not raw:
            output_mode = "enabled" if self.settings.enable_output_file else "disabled"
            resume_mode = "enabled" if self.settings.enable_session_resume else "disabled"
            await self.send_html(
                update,
                "<b>Runtime settings</b>\n"
                f"TG_ENABLE_OUTPUT_FILE: <b>{output_mode}</b>\n"
                f"TG_AUTH_TTL_SECONDS: <code>{self.settings.auth_ttl_seconds}s</code>\n"
                f"TG_ENABLE_SESSION_RESUME: <b>{resume_mode}</b>\n"
                f"Env file: {self._code_inline(str(self.env_path))}\n\n"
                "<b>Usage</b>\n"
                "<code>/setting output_file on|off</code>\n"
                "<code>/setting auth_ttl 7d</code>\n"
                "<code>/setting session_resume on|off</code>",
            )
            return

        if len(context.args) < 2:
            await self.send_html(
                update,
                "<b>Usage</b>\n"
                "<code>/setting output_file on|off</code>\n"
                "<code>/setting auth_ttl 7d</code>\n"
                "<code>/setting session_resume on|off</code>",
            )
            return

        key_raw = context.args[0].strip().lower()
        value_raw = " ".join(context.args[1:]).strip()
        key_aliases = {
            "output": "TG_ENABLE_OUTPUT_FILE",
            "output_file": "TG_ENABLE_OUTPUT_FILE",
            "codex_output_file": "TG_ENABLE_OUTPUT_FILE",
            "tg_enable_output_file": "TG_ENABLE_OUTPUT_FILE",
            "auth_ttl": "TG_AUTH_TTL_SECONDS",
            "auth_ttl_seconds": "TG_AUTH_TTL_SECONDS",
            "tg_auth_ttl_seconds": "TG_AUTH_TTL_SECONDS",
            "session_resume": "TG_ENABLE_SESSION_RESUME",
            "tg_enable_session_resume": "TG_ENABLE_SESSION_RESUME",
        }
        env_key = key_aliases.get(key_raw)
        if env_key is None:
            await self.send_html(
                update,
                "<b>Unknown setting key</b>\n"
                f"Received: {self._code_inline(key_raw)}\n"
                "Supported keys: <code>output_file</code>, <code>auth_ttl</code>, <code>session_resume</code>.",
            )
            return

        updates: Dict[str, str] = {}
        if env_key == "TG_AUTH_TTL_SECONDS":
            parsed = self._parse_auth_ttl_setting(value_raw)
            if parsed is None:
                await self.send_html(
                    update,
                    "<b>Invalid auth_ttl</b>\n"
                    "Use a positive duration like <code>3600</code>, <code>60s</code>, <code>30m</code>, <code>2h</code>, <code>7d</code>.",
                )
                return
            seconds, env_value = parsed
            self.settings.auth_ttl_seconds = seconds
            updates[env_key] = env_value
            applied_value = f"{seconds}s ({env_value})"
        else:
            toggle = self._parse_toggle_value(value_raw)
            if toggle is None:
                await self.send_html(
                    update,
                    "<b>Invalid toggle value</b>\n"
                    "Use one of: <code>on/off</code>, <code>1/0</code>, <code>true/false</code>.",
                )
                return
            updates[env_key] = "1" if toggle else "0"
            if env_key == "TG_ENABLE_OUTPUT_FILE":
                self.settings.enable_output_file = toggle
            elif env_key == "TG_ENABLE_SESSION_RESUME":
                self.settings.enable_session_resume = toggle
            applied_value = "enabled" if toggle else "disabled"

        try:
            self._upsert_env_settings(updates)
        except OSError as err:
            await self.send_html(update, f"<b>Setting update failed</b>\nReason: {self._code_inline(str(err))}")
            return

        await self.send_html(
            update,
            "<b>Setting updated</b>\n"
            f"{self._code_inline(env_key)} = <code>{html.escape(updates[env_key])}</code>\n"
            f"Applied: <b>{html.escape(applied_value)}</b>",
        )

    async def _run_prompt(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        prompt: str,
        cleanup_paths: Optional[list[Path]] = None,
    ) -> None:
        if update.effective_chat is None or update.effective_message is None:
            return
        chat_id = update.effective_chat.id
        if not self._is_update_authorized(update):
            await self.send_html(update, "<b>Access denied</b>")
            return
        if not await self._ensure_second_factor(update):
            return

        if not prompt:
            await self.send_html(update, "Usage: send plain text directly (non-<code>/xxx</code>).")
            return

        if self._is_duplicate_request(chat_id, update.effective_message.message_id):
            return

        if chat_id in self.tasks and not self.tasks[chat_id].done():
            await self.send_html(update, "<b>A task is already running</b>\nUse <code>/cancel</code> first.")
            return

        running_total = sum(1 for task in self.tasks.values() if not task.done())
        if running_total >= self.settings.max_concurrent_tasks:
            await self.send_html(
                update,
                "<b>System busy</b>\n"
                f"Too many running tasks (<code>{self.settings.max_concurrent_tasks}</code>). Try again later.",
            )
            return

        draft_enabled = (update.effective_chat.type == "private")
        thread_id = update.effective_message.message_thread_id
        draft_id = update.effective_message.message_id
        status_message_id: Optional[int] = None

        cmd_args, _session_id, workdir = self._resolve_codex_command(chat_id, prompt)
        if not draft_enabled:
            msg = await update.effective_message.reply_text(
                text="<i>Running...</i>",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            status_message_id = msg.message_id

        cleanup_targets = list(cleanup_paths or [])

        async def _worker() -> None:
            nonlocal status_message_id

            async def _ensure_status_message_id(initial_text: str = "<i>Running...</i>") -> int:
                nonlocal status_message_id
                if status_message_id is not None:
                    return status_message_id
                sent = await context.bot.send_message(
                    chat_id=chat_id,
                    text=initial_text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    message_thread_id=thread_id,
                    read_timeout=30,
                    write_timeout=30,
                    connect_timeout=30,
                    pool_timeout=5,
                )
                status_message_id = sent.message_id
                return status_message_id

            output = ""
            detected_session_id: Optional[str] = None
            last_progress_emit = 0.0
            last_stream_text = ""
            started = time.monotonic()
            output_truncated = False
            draft_available = draft_enabled
            draft_text = ""
            pending_progress_text: Optional[str] = None
            progress_update_task: Optional[asyncio.Task] = None

            async def _send_progress_text(stream_text: str) -> None:
                nonlocal draft_available, draft_text
                if draft_enabled and draft_available and stream_text != draft_text:
                    try:
                        await asyncio.wait_for(
                            self._send_message_draft_raw(
                                chat_id=chat_id,
                                draft_id=draft_id,
                                text=stream_text,
                                message_thread_id=thread_id,
                            ),
                            timeout=STREAM_PROGRESS_IO_TIMEOUT_SECONDS,
                        )
                        draft_text = stream_text
                    except Exception:
                        draft_available = False

                if not (draft_enabled and draft_available):
                    try:
                        message_id = await asyncio.wait_for(
                            _ensure_status_message_id(),
                            timeout=STREAM_PROGRESS_IO_TIMEOUT_SECONDS,
                        )
                        await asyncio.wait_for(
                            self.safe_edit(
                                context,
                                chat_id,
                                message_id,
                                stream_text,
                            ),
                            timeout=STREAM_PROGRESS_IO_TIMEOUT_SECONDS,
                        )
                    except Exception:
                        return

            async def _flush_progress_updates() -> None:
                nonlocal pending_progress_text, progress_update_task
                try:
                    while pending_progress_text is not None:
                        text = pending_progress_text
                        pending_progress_text = None
                        await _send_progress_text(text)
                finally:
                    progress_update_task = None

            def _queue_progress_update(stream_text: str) -> None:
                nonlocal pending_progress_text, progress_update_task
                pending_progress_text = stream_text
                if progress_update_task is None or progress_update_task.done():
                    progress_update_task = context.application.create_task(_flush_progress_updates())

            async def _stop_progress_updates() -> None:
                nonlocal pending_progress_text, progress_update_task
                pending_progress_text = None
                task = progress_update_task
                progress_update_task = None
                if task and not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
            try:
                async for chunk in run_codex_stream(cmd_args, self.settings.codex_timeout_seconds, cwd=workdir):
                    output += chunk
                    if len(output) > self.settings.max_buffered_output_chars:
                        output = output[-self.settings.max_buffered_output_chars :]
                        output_truncated = True
                    maybe_session_id = self._extract_session_id(chunk)
                    if maybe_session_id:
                        detected_session_id = maybe_session_id
                    now = time.monotonic()
                    throttle_seconds = EDIT_THROTTLE_SECONDS if chunk else IDLE_EDIT_THROTTLE_SECONDS
                    if now - last_progress_emit < throttle_seconds:
                        continue

                    stream_text = self._format_stream_text("Running", output, now - started)
                    if stream_text == last_stream_text:
                        continue

                    last_progress_emit = now
                    last_stream_text = stream_text
                    _queue_progress_update(stream_text)

                await _stop_progress_updates()
                cleaned_output = self._clean_output(output)
                if output_truncated:
                    cleaned_output = "[output truncated for safety]\n" + cleaned_output
                final_message_id = await _ensure_status_message_id("<i>Finalizing...</i>")
                await self._send_final_output_messages(
                    context=context,
                    chat_id=chat_id,
                    message_id=final_message_id,
                    cleaned_output=cleaned_output,
                )
                if self._should_upload_output_file(cleaned_output):
                    output_path = self._write_output_file(
                        chat_id=chat_id,
                        message_id=final_message_id,
                        cleaned_output=cleaned_output,
                    )
                    try:
                        await self._upload_output_file(
                            context=context,
                            chat_id=chat_id,
                            output_path=output_path,
                        )
                    except Exception as err:
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=(
                                "<b>文件上传失败</b>\n"
                                f"输出已保存到本地: {self._code_inline(str(output_path))}\n"
                                f"Reason: {self._code_inline(str(err))}"
                            ),
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True,
                        )
                final_session_id = self._extract_session_id(cleaned_output) or detected_session_id
                if final_session_id and self.settings.enable_session_resume:
                    self._set_chat_session(chat_id, final_session_id)
            except asyncio.CancelledError:
                await _stop_progress_updates()
                cancel_text = "<b>Task cancelled</b>\nExecution stopped by user."
                if status_message_id is not None:
                    await self.safe_edit(
                        context,
                        chat_id,
                        status_message_id,
                        cancel_text,
                    )
                else:
                    try:
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=cancel_text,
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True,
                            message_thread_id=thread_id,
                            read_timeout=30,
                            write_timeout=30,
                            connect_timeout=30,
                            pool_timeout=5,
                        )
                    except TelegramError:
                        pass
                raise
            except Exception as err:
                await _stop_progress_updates()
                error_text = f"<b>Task failed</b>\nReason: {self._code_inline(str(err))}"
                if status_message_id is not None:
                    await self.safe_edit(
                        context,
                        chat_id,
                        status_message_id,
                        error_text,
                    )
                else:
                    try:
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=error_text,
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True,
                            message_thread_id=thread_id,
                            read_timeout=30,
                            write_timeout=30,
                            connect_timeout=30,
                            pool_timeout=5,
                        )
                    except TelegramError:
                        pass
            finally:
                for path in cleanup_targets:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        continue

        task = context.application.create_task(_worker())
        self.tasks[chat_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(chat_id, None))
