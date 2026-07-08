import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from config.settings import settings

ToolResult = dict[str, Any]
MAX_OUTPUT = 4000

# Shell operators that chain commands. We split on these so the allowlist and
# network gate inspect EVERY binary in a compound command (e.g. `ok && curl x`),
# not just the first — this is the "gate shell metacharacters" half of Step 7:
# shell=True stays for Windows usability, but chained binaries can't smuggle a
# denied/network command past the checks.
_SEGMENT_SPLIT_RE = re.compile(r"\|\||&&|\||;|&|\n")

# Package managers reaching a remote index, and network-y git subcommands.
_INSTALL_NETWORK_RE = re.compile(
    r"\b(pip|pip3|pipx|npm|pnpm|yarn|poetry|conda|apt|apt-get|brew|choco|gem|cargo)\b"
    r".*\b(install|add|download|fetch)\b",
    re.IGNORECASE,
)
_GIT_NETWORK_RE = re.compile(
    r"\bgit\b.*\b(clone|pull|fetch|push|remote\s+(add|set-url))\b", re.IGNORECASE
)


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


def _segments(command: str) -> list[str]:
    return [s.strip() for s in _SEGMENT_SPLIT_RE.split(command) if s.strip()]


def _binary_name(segment: str) -> str:
    """Best-effort name of the binary a segment invokes (no path, no .exe)."""
    toks = segment.split()
    if not toks:
        return ""
    name = Path(toks[0]).name.lower()
    return name[:-4] if name.endswith(".exe") else name


def _allowlist_violation(command: str) -> str | None:
    """When settings.command_allowlist is non-empty, every chained binary must
    be on it (Step 7 / S1). Empty allowlist = disabled (denylist still applies)."""
    allow = {a.lower() for a in settings.command_allowlist}
    if not allow:
        return None
    for seg in _segments(command):
        name = _binary_name(seg)
        if name and name not in allow:
            return (
                f"Command {name!r} is not in the allowlist "
                f"({sorted(allow)}); blocked."
            )
    return None


def _network_violation(command: str) -> str | None:
    """Refuse commands that reach the network unless allow_network (Step 7 / S4)."""
    if settings.allow_network:
        return None
    net = {c.lower() for c in settings.network_commands}
    for seg in _segments(command):
        if _binary_name(seg) in net:
            return (
                f"Network command {_binary_name(seg)!r} is blocked; "
                "launch with --allow-network to permit it."
            )
    if _INSTALL_NETWORK_RE.search(command) or _GIT_NETWORK_RE.search(command):
        return (
            "Network-reaching command blocked; launch with --allow-network "
            "to permit it."
        )
    return None


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

    allow_err = _allowlist_violation(command)
    if allow_err:
        return _err(allow_err)

    net_err = _network_violation(command)
    if net_err:
        return _err(net_err)

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
