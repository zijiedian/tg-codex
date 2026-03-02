import asyncio
import html
import hmac
import json
import mimetypes
import os
import re
import shlex
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
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
    LONG_SECRET_RE,
    MARKDOWN_BULLET_RE,
    MARKDOWN_FENCE_CLOSE_RE,
    MARKDOWN_FENCE_RE,
    MARKDOWN_HEADING_RE,
    MARKDOWN_ORDERED_RE,
    MARKDOWN_RULE_RE,
    MIN_AUTH_PASSPHRASE_LENGTH,
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
    STREAM_PREVIEW_LINE_LIMIT,
    TELEGRAM_MESSAGE_LIMIT,
    THINKING_DETAIL_MAX_CHARS,
    THINKING_DETAIL_MAX_LINES,
    THINKING_SPINNER_FRAMES,
    TRACE_SECTION_MARKERS,
    TRACE_SKIP_SECTION_MARKERS,
)
from settings import Settings


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
        self.media_dir = Path(__file__).with_name("incoming_media")
        self.output_dir = Path(__file__).with_name("outputs")
        self.sessions_path = Path(__file__).with_name("chat_sessions.json")
        self.chat_sessions: Dict[int, str] = self._load_chat_sessions()
        self.page_sessions: Dict[tuple[int, int], PageSession] = {}

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
            cmd = cmd[: exec_idx + 1] + ["resume", session_id] + cmd[exec_idx + 1 :]
            cmd.append(prompt)
            return cmd
        cmd.extend(["exec", "resume", session_id, prompt])
        return cmd

    def _resolve_codex_command(self, chat_id: int, prompt: str) -> tuple[list[str], str]:
        base = _validate_codex_prefix(self.codex_prefix)
        session_id = self.chat_sessions.get(chat_id)
        if session_id:
            resume_cmd = self._build_resume_command(base, session_id, prompt)
            if resume_cmd is not None:
                return resume_cmd, session_id
        return base + [prompt], ""

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
        if not normalized:
            return ""
        if "```" in normalized:
            return normalized

        lines = [line for line in normalized.splitlines() if line.strip()]
        if lines and self._looks_like_shell_command_line(lines[0]):
            return f"```bash\n{normalized}\n```"
        return f"```\n{normalized}\n```"

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
    def _append_elapsed_footer(body_html: str, elapsed_text: str) -> str:
        footer = f"<i>{elapsed_text}</i>"
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
            text = self._append_elapsed_footer(text, elapsed_text)
            if len(text) <= TELEGRAM_MESSAGE_LIMIT:
                return text
            compact_detail_html = self._format_thinking_detail_html(thinking_detail, compact=True)
            if compact_detail_html:
                compact = f"<i>{html.escape(frame)} thinking{dots}</i>\n{compact_detail_html}"
                return self._append_elapsed_footer(compact, elapsed_text)
            return self._append_elapsed_footer(f"<i>{html.escape(frame)} thinking{dots}</i>", elapsed_text)

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
        now = time.monotonic()
        stale_keys = [
            key
            for key, session in self.page_sessions.items()
            if now - session.last_access > PAGE_SESSION_TTL_SECONDS
        ]
        for key in stale_keys:
            self.page_sessions.pop(key, None)

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
            await self.safe_edit(context, chat_id, message_id, "<i>暂无输出</i>")
            return

        chunks = self._split_output_chunks(preview_text, FINAL_OUTPUT_CHUNK_LIMIT)
        if not chunks:
            await self.safe_edit(context, chat_id, message_id, "<i>暂无输出</i>")
            return

        self._prune_page_sessions()
        session_key = (chat_id, message_id)
        if len(chunks) > 1:
            self.page_sessions[session_key] = PageSession(
                chat_id=chat_id,
                message_id=message_id,
                pages=chunks,
                created_at=time.monotonic(),
                last_access=time.monotonic(),
                current_index=0,
            )
        else:
            self.page_sessions.pop(session_key, None)
        first_html = self._render_paginated_html(chunks[0], 0, len(chunks))
        reply_markup = self._build_page_keyboard(message_id, 0, len(chunks))
        await self.safe_edit(context, chat_id, message_id, first_html, reply_markup=reply_markup)

    def _output_file_name(self, chat_id: int, message_id: int) -> str:
        timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        return f"codex-output-{chat_id}-{message_id}-{timestamp}.txt"

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
    ) -> None:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )
        except BadRequest as err:
            if "Message is not modified" not in str(err):
                raise

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
            "Use <code>/run &lt;prompt&gt;</code> to execute a task.\n\n"
            "Send an image (optional caption) to run an image prompt.\n\n"
            "<b>Commands</b>\n"
            "- <code>/cmd</code> show current command prefix\n"
            "- <code>/cmd &lt;new prefix&gt;</code> update command prefix\n"
            "- <code>/cmd reset</code> restore default command\n"
            "- <code>/id</code> show current chat/user id\n"
            "- <code>/auth &lt;passphrase&gt;</code> unlock execution\n"
            "- <code>/status</code> show current task status\n"
            "- <code>/cancel</code> stop current task\n"
            f"\nCommand override: <b>{'ON' if self.settings.allow_cmd_override else 'OFF'}</b> (admin user + admin chat)"
            f"\nSecond-factor auth: <b>{auth_status}</b>"
            + (
                "\n\nPlain text mode: <b>ON</b> (send text directly to run prompt)."
                if self.settings.allow_plain_text
                else "\n\nPlain text mode: <b>OFF</b>."
            ),
        )

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None:
            return
        chat_id = update.effective_chat.id
        if not self._is_update_authorized(update):
            await self.send_html(update, "<b>Access denied</b>")
            return
        task = self.tasks.get(chat_id)
        mode = "enabled" if self.settings.allow_plain_text else "disabled"
        session_id = self.chat_sessions.get(chat_id, "")
        session_text = self._code_inline(session_id) if session_id else "<code>(new)</code>"
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
                f"Plain text mode: <b>{mode}</b>\n"
                f"Second-factor: <b>{auth_state}</b>\n"
                f"Session: {session_text}",
            )
        else:
            await self.send_html(
                update,
                "<b>Task Status</b>\n"
                "State: <b>Idle</b>\n"
                f"Command:\n{self._code_block(display_prefix)}\n"
                f"Plain text mode: <b>{mode}</b>\n"
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
        session.last_access = time.monotonic()
        page_html = self._render_paginated_html(session.pages[index], index, len(session.pages))
        reply_markup = self._build_page_keyboard(message_id, index, len(session.pages))
        await self.safe_edit(context, chat_id, message_id, page_html, reply_markup=reply_markup)
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

        raw = " ".join(context.args).strip()
        if not raw:
            display_prefix = self._redacted_command_text(self.codex_prefix)
            await self.send_html(
                update,
                "<b>Current command prefix</b>\n"
                f"{self._code_block(display_prefix)}\n"
                "<b>Usage</b>\n"
                "<code>/cmd &lt;command prefix&gt;</code>\n"
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
            await self.send_html(update, "Usage: <code>/run &lt;prompt&gt;</code>")
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

        cmd_args, session_id = self._resolve_codex_command(chat_id, prompt)
        session_text = session_id if session_id else "(new)"
        msg = await update.effective_message.reply_text(
            text=(
                "<b>Starting Codex task</b>\n"
                f"Session: {self._code_inline(session_text)}\n"
                f"Prompt: {self._code_inline(clip_for_inline(prompt, limit=400))}"
            ),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

        cleanup_targets = list(cleanup_paths or [])

        async def _worker() -> None:
            output = ""
            detected_session_id: Optional[str] = None
            last_edit = 0.0
            started = time.monotonic()
            output_truncated = False
            try:
                async for chunk in run_codex_stream(cmd_args, self.settings.codex_timeout_seconds):
                    output += chunk
                    if len(output) > self.settings.max_buffered_output_chars:
                        output = output[-self.settings.max_buffered_output_chars :]
                        output_truncated = True
                    maybe_session_id = self._extract_session_id(chunk)
                    if maybe_session_id:
                        detected_session_id = maybe_session_id
                    now = time.monotonic()
                    if now - last_edit >= EDIT_THROTTLE_SECONDS:
                        last_edit = now
                        await self.safe_edit(
                            context,
                            chat_id,
                            msg.message_id,
                            self._format_stream_text("Running", output, now - started),
                        )

                cleaned_output = self._clean_output(output)
                if output_truncated:
                    cleaned_output = "[output truncated for safety]\n" + cleaned_output
                await self._send_final_output_messages(
                    context=context,
                    chat_id=chat_id,
                    message_id=msg.message_id,
                    cleaned_output=cleaned_output,
                )
                output_path = self._write_output_file(
                    chat_id=chat_id,
                    message_id=msg.message_id,
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
                if final_session_id:
                    self._set_chat_session(chat_id, final_session_id)
            except asyncio.CancelledError:
                await self.safe_edit(
                    context,
                    chat_id,
                    msg.message_id,
                    "<b>Task cancelled</b>\nExecution stopped by user.",
                )
                raise
            except Exception as err:
                await self.safe_edit(
                    context,
                    chat_id,
                    msg.message_id,
                    f"<b>Task failed</b>\nReason: {self._code_inline(str(err))}",
                )
            finally:
                for path in cleanup_targets:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        continue

        task = context.application.create_task(_worker())
        self.tasks[chat_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(chat_id, None))
