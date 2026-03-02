import asyncio
import shlex
import time
from pathlib import PurePath
from typing import AsyncIterator


def _validate_codex_prefix(prefix: str) -> list[str]:
    parts = shlex.split(prefix)
    if not parts:
        raise ValueError("command prefix cannot be empty")
    first = PurePath(parts[0]).name.lower()
    if "codex" not in first:
        raise ValueError("command prefix must start with a codex executable")
    if "exec" not in parts:
        raise ValueError("command prefix must include exec")
    if "--dangerously-skip-permissions" in parts:
        raise ValueError("command prefix cannot use --dangerously-skip-permissions")

    approval_mode = ""
    if "-a" in parts:
        idx = parts.index("-a")
        approval_mode = parts[idx + 1] if idx + 1 < len(parts) else ""
    if "--ask-for-approval" in parts:
        idx = parts.index("--ask-for-approval")
        approval_mode = parts[idx + 1] if idx + 1 < len(parts) else ""
    if approval_mode and approval_mode != "never":
        raise ValueError("command prefix must keep approval mode as never")
    return parts


async def run_codex_stream(cmd: list[str], timeout_seconds: int) -> AsyncIterator[str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as err:
        raise RuntimeError(f"codex command not found: {cmd[0]}") from err
    if proc.stdout is None:
        proc.kill()
        await proc.wait()
        raise RuntimeError("codex subprocess stdout is unavailable")

    # Timeout is enforced on both total runtime and stdout reads.
    start = time.monotonic()
    try:
        while True:
            elapsed = time.monotonic() - start
            if elapsed >= timeout_seconds:
                proc.kill()
                await proc.wait()
                raise TimeoutError(f"codex command timed out after {timeout_seconds}s")
            remaining = timeout_seconds - elapsed
            read_timeout = min(1.0, remaining)
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=read_timeout)
            except asyncio.TimeoutError:
                # Heartbeat: keep UI updates (spinner/timer) flowing while waiting for next chunk.
                yield ""
                continue
            if not line:
                break
            yield line.decode("utf-8", errors="replace")
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise

    code = await proc.wait()
    if code != 0:
        raise RuntimeError(f"codex exited with code {code}")
