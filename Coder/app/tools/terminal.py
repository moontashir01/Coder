import subprocess
import shlex
import sys
from pathlib import Path
from typing import Any

from config.settings import settings

ToolResult = dict[str, Any]
MAX_OUTPUT = 4000


def _ok(result: str) -> ToolResult:
    return {"success": True, "result": result, "error": None}


def _err(error: str) -> ToolResult:
    return {"success": False, "result": "", "error": error}


def _is_blocked(command: str) -> bool:
    cmd_lower = command.lower().strip()
    if not cmd_lower:
        return False

    tokens = cmd_lower.split()
    first_name = Path(tokens[0]).name if tokens else ""

    for blocked in settings.blocked_commands:
        b = blocked.lower().strip()
        if not b:
            continue
        # Multi-token / path-like patterns (e.g. "rm -rf /", "dd if=/dev/zero")
        # are distinctive enough to match anywhere in the command.
        if (" " in b) or ("/" in b) or ("=" in b):
            if b in cmd_lower:
                return True
        # Bare executable names (e.g. "format", "mkfs") must match the actual
        # command being invoked — not appear as a substring of an argument
        # (otherwise "'{}'.format(x)" would be blocked).
        elif first_name == b or first_name.startswith(b + "."):
            return True
    return False


def _truncate(text: str) -> str:
    if len(text) > MAX_OUTPUT:
        return text[:MAX_OUTPUT] + f"\n... [truncated — {len(text) - MAX_OUTPUT} more chars]"
    return text


def run_command(
    command: str,
    cwd: str | None = None,
    timeout: int | None = None,
) -> ToolResult:
    if _is_blocked(command):
        return _err(f"Command blocked by safety rules: {command!r}")

    timeout = timeout or settings.command_timeout_seconds
    cwd_path = Path(cwd).resolve() if cwd else None

    try:
        # On Windows use shell=True; on Unix split the command
        use_shell = sys.platform == "win32"
        args = command if use_shell else shlex.split(command)

        proc = subprocess.run(
            args,
            cwd=cwd_path,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=use_shell,
        )

        stdout = _truncate(proc.stdout or "")
        stderr = _truncate(proc.stderr or "")

        result_parts = []
        if stdout:
            result_parts.append(f"[stdout]\n{stdout}")
        if stderr:
            result_parts.append(f"[stderr]\n{stderr}")
        result_parts.append(f"[exit code] {proc.returncode}")

        combined = "\n".join(result_parts)

        if proc.returncode != 0:
            return {"success": False, "result": combined, "error": f"Exit code {proc.returncode}"}
        return _ok(combined)

    except subprocess.TimeoutExpired:
        return _err(f"Command timed out after {timeout}s: {command!r}")
    except FileNotFoundError as e:
        return _err(f"Command not found: {e}")
    except Exception as e:
        return _err(str(e))
