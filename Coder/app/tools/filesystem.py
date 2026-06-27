import re
from pathlib import Path
from typing import Any

ToolResult = dict[str, Any]


def _ok(result: str) -> ToolResult:
    return {"success": True, "result": result, "error": None}


def _err(error: str) -> ToolResult:
    return {"success": False, "result": "", "error": error}


def read_file(path: str) -> ToolResult:
    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace")
        return _ok(content)
    except FileNotFoundError:
        return _err(f"File not found: {path}")
    except Exception as e:
        return _err(str(e))


def write_file(path: str, content: str) -> ToolResult:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return _ok(f"Written {len(content)} bytes to {path}")
    except Exception as e:
        return _err(str(e))


def edit_file(path: str, old_str: str, new_str: str) -> ToolResult:
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
        p.write_text(updated, encoding="utf-8")
        return _ok(f"Edited {path}: replaced 1 occurrence")
    except FileNotFoundError:
        return _err(f"File not found: {path}")
    except Exception as e:
        return _err(str(e))


def create_file(path: str, content: str = "") -> ToolResult:
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
    try:
        p = Path(path)
        if not p.exists():
            return _err(f"File not found: {path}")
        p.unlink()
        return _ok(f"Deleted {path}")
    except Exception as e:
        return _err(str(e))


def list_directory(path: str, recursive: bool = False) -> ToolResult:
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
    try:
        root = Path(path)
        if not root.exists():
            return _err(f"Path not found: {path}")

        regex = re.compile(pattern)
        matches: list[str] = []

        targets = root.rglob("*") if root.is_dir() else [root]
        for file in targets:
            if not file.is_file():
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
