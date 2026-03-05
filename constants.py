import re

EDIT_THROTTLE_SECONDS = 1.0
IDLE_EDIT_THROTTLE_SECONDS = 6.0
STREAM_PROGRESS_IO_TIMEOUT_SECONDS = 3.0
STREAM_PREVIEW_LIMIT = 2200
STREAM_PREVIEW_LINE_LIMIT = 24
PREVIEW_LINE_CHAR_LIMIT = 180
TELEGRAM_MESSAGE_LIMIT = 3900
FINAL_OUTPUT_CHUNK_LIMIT = 1600
OUTPUT_FILE_MIN_CHARS = 1024
THINKING_DETAIL_MAX_LINES = 2
THINKING_DETAIL_MAX_CHARS = 140
PAGE_SESSION_TTL_SECONDS = 3600
REQUEST_DEDUP_SECONDS = 180
DEFAULT_MAX_BUFFERED_OUTPUT_CHARS = 200_000
DEFAULT_MAX_CONCURRENT_TASKS = 2
DEFAULT_CODEX_TIMEOUT_SECONDS = 21600
DEFAULT_AUTH_TTL_SECONDS = 604800
MIN_AUTH_PASSPHRASE_LENGTH = 12

ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
SESSION_ID_RE = re.compile(r"session id:\s*([0-9a-fA-F-]{36})", re.IGNORECASE)
MARKDOWN_FENCE_RE = re.compile(r"^\s*```")
MARKDOWN_FENCE_CLOSE_RE = re.compile(r"^\s*`{3,}\s*$")
MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")
MARKDOWN_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)$")
MARKDOWN_ORDERED_RE = re.compile(r"^\s*(\d+)\.\s+(.*)$")
MARKDOWN_RULE_RE = re.compile(r"^\s*([-*_])\1{2,}\s*$")
DIFF_HEADER_RE = re.compile(
    r"^(?:diff --git |index |--- |\+\+\+ |@@|new file mode |deleted file mode |similarity index |rename from |rename to |old mode |new mode )"
)
CODE_INDENT_RE = re.compile(r"^\s{4,}\S")
CODE_KEYWORD_RE = re.compile(
    r"^\s*(?:async\s+def|def|class|if|elif|else|for|while|try|except|finally|return|import|from|"
    r"function|const|let|var|public|private|protected|package|func|type|interface|switch|case|"
    r"SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b"
)
SHELL_PROMPT_RE = re.compile(r"^\s*(?:\$|#)\s+\S+")
SENSITIVE_OPTION_RE = re.compile(r"(?:token|secret|password|passwd|api[-_]?key|auth)", re.IGNORECASE)
LONG_SECRET_RE = re.compile(r"^[A-Za-z0-9_\-]{24,}$")
PREVIEW_DIVIDER_RE = re.compile(r"^-{3,}$")
PREVIEW_NOISE_PATTERNS = (
    re.compile(r"^OpenAI Codex v", re.IGNORECASE),
    re.compile(r"^(?:model|provider|approval|sandbox|workdir|reasoning effort):", re.IGNORECASE),
    re.compile(r"^session id:\s*[0-9a-fA-F-]{36}$", re.IGNORECASE),
    re.compile(r"^(?:user|assistant|codex|exec)$", re.IGNORECASE),
    re.compile(r"^mcp startup:", re.IGNORECASE),
    re.compile(r"^tokens used$", re.IGNORECASE),
    PREVIEW_DIVIDER_RE,
)

TRACE_SECTION_MARKERS = {"user", "assistant", "codex", "thinking", "exec"}
TRACE_SKIP_SECTION_MARKERS = {"user", "exec"}
PATCH_BEGIN_MARKER = "*** Begin Patch"
PATCH_END_MARKER = "*** End Patch"
PATCH_UPDATE_PREFIX = "*** Update File: "
PATCH_ADD_PREFIX = "*** Add File: "
PATCH_DELETE_PREFIX = "*** Delete File: "
PATCH_MOVE_PREFIX = "*** Move to: "
PATCH_END_OF_FILE_MARKER = "*** End of File"
THINKING_SPINNER_FRAMES = ("-", "\\", "|", "/")
