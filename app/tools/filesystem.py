import difflib
import re
import time
from itertools import count
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from config.settings import settings

ToolResult = dict[str, Any]


def _ok(result: str) -> ToolResult:
    return {"success": True, "result": result, "error": None}


def _err(error: str) -> ToolResult:
    return {"success": False, "result": "", "error": error}


# ---------------------------------------------------------------------------
# Path jail (Step 5 / S2): resolve every caller-supplied path and reject any
# that escapes settings.sandbox_root, unless allow_outside_root is set. The
# jail is inert when sandbox_root is None (tests / library use) so importing
# the tools imposes no policy; main.py + load_project set the root at runtime.
# ---------------------------------------------------------------------------


def _jail_check(path: str) -> str | None:
    """Return an error string if ``path`` escapes the sandbox root, else None."""
    if settings.allow_outside_root or settings.sandbox_root is None:
        return None
    root = Path(settings.sandbox_root).resolve()
    try:
        resolved = Path(path).resolve()
    except OSError as e:
        return f"Cannot resolve path {path}: {e}"
    if resolved == root or root in resolved.parents:
        return None
    return (
        f"Path escapes the project root: {path}\n"
        f"(root: {root}). Launch with --allow-outside-root to permit this."
    )


# ---------------------------------------------------------------------------
# Safe writes (Tier 3 #8): every mutating tool backs up the previous content
# into settings.backups_dir before touching the file; undo_write restores and
# consumes the most recent backup, so repeated undos walk back through history.
# The original absolute path is URL-quoted into the backup filename after the
# first "__" (quote() never emits "_", so "__" splits unambiguously).
# ---------------------------------------------------------------------------

_backup_seq = count()


def _backup_file(p: Path) -> None:
    """Snapshot p's current content. Raises on failure — callers must treat a
    failed backup as a failed mutation rather than proceed and lose data."""
    root = Path(settings.backups_dir)
    root.mkdir(parents=True, exist_ok=True)
    encoded = quote(str(p.resolve()), safe="")
    name = f"{time.time_ns():020d}-{next(_backup_seq) % 1_000_000:06d}__{encoded}"
    (root / name).write_bytes(p.read_bytes())
    _prune_backups(root)


def _prune_backups(root: Path) -> None:
    backups = sorted(root.iterdir(), key=lambda b: b.name)
    excess = len(backups) - settings.max_write_backups
    for old in backups[:excess] if excess > 0 else []:
        try:
            old.unlink()
        except OSError:
            pass  # pruning is best-effort; a stale backup is harmless


def _original_path(backup: Path) -> str | None:
    parts = backup.name.split("__", 1)
    return unquote(parts[1]) if len(parts) == 2 else None


def _attach_diff(res: ToolResult, old: str, new: str, path: str) -> ToolResult:
    """Add a unified diff of a mutating write to the tool result (Tier 3 #8).

    The diff rides on an extra "diff" key: the tool loop only feeds
    result["result"] back to the model, so this is display-only for the REPL.
    """
    diff = "\n".join(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )
    if diff:
        added = sum(
            1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")
        )
        removed = sum(
            1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")
        )
        res["diff"] = diff
        res["result"] += f" (+{added}/-{removed} lines)"
    return res


def undo_write(path: str | None = None) -> ToolResult:
    """Restore the most recent backup; with ``path``, the most recent backup
    of that file. The used backup is deleted (undo again → previous state)."""
    try:
        root = Path(settings.backups_dir)
        backups = (
            sorted((b for b in root.iterdir() if _original_path(b)), key=lambda b: b.name)
            if root.exists()
            else []
        )
        if path is not None:
            wanted = str(Path(path).resolve())
            backups = [b for b in backups if _original_path(b) == wanted]
        if not backups:
            target = f" for {path}" if path else ""
            return _err(f"No backup to undo{target}.")
        latest = backups[-1]
        original = Path(_original_path(latest))
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(latest.read_bytes())
        latest.unlink()
        return _ok(f"Restored {original} from backup.")
    except Exception as e:
        return _err(str(e))


# Directories never worth searching/reading through (mirrors the indexer skips).
_IGNORE_DIRS = {"__pycache__", "node_modules", ".git", ".venv"}


def _is_binary(p: Path) -> bool:
    """Heuristic: a NUL byte in the first 1 KiB means binary (mirrors git)."""
    try:
        with p.open("rb") as fh:
            return b"\x00" in fh.read(1024)
    except OSError:
        return False


def read_file(path: str) -> ToolResult:
    jail = _jail_check(path)
    if jail:
        return _err(jail)
    try:
        p = Path(path)
        cap = settings.max_read_file_bytes
        with p.open("rb") as fh:
            data = fh.read(cap + 1)
        truncated = len(data) > cap
        content = data[:cap].decode("utf-8", errors="replace")
        if truncated:
            total = p.stat().st_size
            content += f"\n... [truncated — file is {total} bytes, showing first {cap}]"
        return _ok(content)
    except FileNotFoundError:
        return _err(f"File not found: {path}")
    except Exception as e:
        return _err(str(e))


def write_file(path: str, content: str) -> ToolResult:
    jail = _jail_check(path)
    if jail:
        return _err(jail)
    try:
        p = Path(path)
        old_content: str | None = None
        if p.is_file():
            old_content = p.read_text(encoding="utf-8", errors="replace")
            _backup_file(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        res = _ok(f"Written {len(content)} bytes to {path}")
        if old_content is not None:
            res = _attach_diff(res, old_content, content, path)
        return res
    except Exception as e:
        return _err(str(e))


def edit_file(path: str, old_str: str, new_str: str) -> ToolResult:
    jail = _jail_check(path)
    if jail:
        return _err(jail)
    try:
        p = Path(path)
        original = p.read_text(encoding="utf-8", errors="replace")
        if old_str not in original:
            return _err(f"String not found in {path}: {old_str[:80]!r}")
        count = original.count(old_str)
        if count > 1:
            return _err(
                f"Ambiguous edit: {count} occurrences of the target string in {path}. "
                "Provide more context to make it unique."
            )
        updated = original.replace(old_str, new_str, 1)
        _backup_file(p)  # only after validation — a rejected edit leaves no backup
        p.write_text(updated, encoding="utf-8")
        return _attach_diff(
            _ok(f"Edited {path}: replaced 1 occurrence"), original, updated, path
        )
    except FileNotFoundError:
        return _err(f"File not found: {path}")
    except Exception as e:
        return _err(str(e))


def create_file(path: str, content: str = "") -> ToolResult:
    jail = _jail_check(path)
    if jail:
        return _err(jail)
    try:
        p = Path(path)
        if p.exists():
            return _err(f"File already exists: {path}. Use write_file to overwrite.")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return _ok(f"Created {path}")
    except Exception as e:
        return _err(str(e))


def delete_file(path: str, confirm: bool = False) -> ToolResult:
    if not confirm:
        return _err(
            f"delete_file requires confirm=True to prevent accidental deletion of {path}"
        )
    jail = _jail_check(path)
    if jail:
        return _err(jail)
    try:
        p = Path(path)
        if not p.exists():
            return _err(f"File not found: {path}")
        if p.is_file():
            _backup_file(p)
        p.unlink()
        return _ok(f"Deleted {path}")
    except Exception as e:
        return _err(str(e))


def list_directory(path: str, recursive: bool = False) -> ToolResult:
    jail = _jail_check(path)
    if jail:
        return _err(jail)
    try:
        p = Path(path)
        if not p.exists():
            return _err(f"Path not found: {path}")
        if not p.is_dir():
            return _err(f"Not a directory: {path}")

        entries = sorted(p.rglob("*") if recursive else p.iterdir())
        lines: list[str] = []
        for entry in entries:
            rel = entry.relative_to(p)
            prefix = "DIR  " if entry.is_dir() else "FILE "
            lines.append(f"{prefix}{rel}")

        return _ok("\n".join(lines) if lines else "(empty directory)")
    except Exception as e:
        return _err(str(e))


def search_files(path: str, pattern: str) -> ToolResult:
    jail = _jail_check(path)
    if jail:
        return _err(jail)
    try:
        root = Path(path)
        if not root.exists():
            return _err(f"Path not found: {path}")

        regex = re.compile(pattern)
        matches: list[str] = []

        is_dir = root.is_dir()
        targets = root.rglob("*") if is_dir else [root]
        for file in targets:
            if not file.is_file():
                continue
            # Skip vendored/hidden dirs and binary files (C4). When searching a
            # single file directly, honor the caller and don't second-guess it.
            if is_dir:
                rel_parts = file.relative_to(root).parts
                if any(
                    part in _IGNORE_DIRS or part.startswith(".")
                    for part in rel_parts[:-1]
                ):
                    continue
                if _is_binary(file):
                    continue
            try:
                for i, line in enumerate(
                    file.read_text(encoding="utf-8", errors="replace").splitlines(), 1
                ):
                    if regex.search(line):
                        matches.append(f"{file}:{i}: {line.rstrip()}")
            except Exception:
                continue

        if not matches:
            return _ok(f"No matches for pattern {pattern!r} in {path}")
        return _ok("\n".join(matches))
    except re.error as e:
        return _err(f"Invalid regex pattern: {e}")
    except Exception as e:
        return _err(str(e))
