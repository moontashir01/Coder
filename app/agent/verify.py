"""Post-write verification for generated files (roadmap Tier 1 #1).

Pure, offline checks: given a file the agent just wrote, answer "does it at
least parse?" so the agent can feed the error back to the model and repair
before shipping. One public function:

    check_file(path) -> (ok, error)

Unknown extensions and missing checker binaries (node/tsc) report ok=True —
"can't verify" must never be treated as "broken".
"""

from __future__ import annotations

import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

_CHECK_TIMEOUT_SECONDS = 30

# Void elements never take a closing tag — don't report them as unclosed.
_HTML_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


def is_verifiable(path: Path | str) -> bool:
    """Can check_file actually validate this file type on this machine?"""
    suffix = Path(path).suffix.lower()
    if suffix in (".py", ".html", ".htm"):
        return True
    if suffix == ".js":
        return shutil.which("node") is not None
    if suffix == ".ts":
        return shutil.which("tsc") is not None
    return False


def check_file(path: Path | str) -> tuple[bool, str]:
    """Cheap correctness check for a just-written file.

    Returns (ok, error). ok=True either means the check passed or that the
    file type is unverifiable here (unknown extension, checker not installed).
    """
    p = Path(path)
    if not p.is_file():
        return False, f"File not found: {p}"
    suffix = p.suffix.lower()
    if suffix == ".py":
        return _check_python(p)
    if suffix == ".js":
        return _check_with_command(p, "node", ["--check"])
    if suffix == ".ts":
        return _check_with_command(p, "tsc", ["--noEmit"])
    if suffix in (".html", ".htm"):
        return _check_html(p)
    return True, ""


def _check_python(path: Path) -> tuple[bool, str]:
    """Syntax-check Python via compile() — no subprocess, never executes code."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        compile(source, str(path), "exec")
        return True, ""
    except SyntaxError as e:
        return False, f"SyntaxError in {path.name}, line {e.lineno}: {e.msg}"
    except Exception as e:
        return False, f"{type(e).__name__} in {path.name}: {e}"


def _check_with_command(path: Path, binary: str, args: list[str]) -> tuple[bool, str]:
    """Run `<binary> <args> <file>`; missing binary counts as unverifiable-ok."""
    exe = shutil.which(binary)
    if exe is None:
        return True, ""
    try:
        proc = subprocess.run(
            [exe, *args, str(path)],
            capture_output=True,
            text=True,
            timeout=_CHECK_TIMEOUT_SECONDS,
        )
    except Exception:
        return True, ""  # checker itself broke → don't block the write
    if proc.returncode == 0:
        return True, ""
    detail = (proc.stderr or proc.stdout or "").strip()
    return False, detail[:1000] or f"{binary} check failed for {path.name}"


class _TagBalanceParser(HTMLParser):
    """Stack-based open/close tag matcher; records the first imbalance."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag not in _HTML_VOID_TAGS:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in _HTML_VOID_TAGS:
            return
        if tag in self.stack:
            # Pop up to the match; anything skipped over was left unclosed.
            while self.stack:
                open_tag = self.stack.pop()
                if open_tag == tag:
                    break
                self.errors.append(f"unclosed <{open_tag}>")
        else:
            self.errors.append(f"stray closing </{tag}>")


def _check_html(path: Path) -> tuple[bool, str]:
    """Structural tag-balance validation (no external tooling needed)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    parser = _TagBalanceParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as e:
        return False, f"HTML parse error in {path.name}: {e}"
    errors = list(parser.errors)
    errors.extend(f"unclosed <{tag}>" for tag in parser.stack)
    if errors:
        return False, f"{path.name}: " + "; ".join(errors[:10])
    return True, ""
