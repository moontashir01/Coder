"""Post-write verification for generated files (roadmap Tier 1 #1).

Pure, offline checks: given a file the agent just wrote, answer "does it at
least parse, and is it the right KIND of content?" so the agent can feed the
error back to the model and repair before shipping. One public function:

    check_file(path) -> (ok, error)

Two families of check:
  * Syntax  — .py via compile(), .js via `node --check`, .ts via `tsc --noEmit`,
    .html/.htm via a tag-balance parser.
  * Content — tooling-free "is this the right language?" guards that catch the
    single most common local-model failure: the WRONG content written into a
    file (a whole HTML document dumped into script.js / styles.css, or plain
    prose left sitting in a code file). These need no external binary, which is
    why .js/.ts/.css are always verifiable now — the guard fires even when
    node/tsc are missing.

Unknown extensions still report ok=True — "can't verify" must never be treated
as "broken".
"""

from __future__ import annotations

import re
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

# The content of a file that opens with one of these is an HTML document, not
# JavaScript/CSS. A code/style file never legitimately starts with a tag, so
# this is a high-signal, low-false-positive "wrong language" detector.
_HTML_DOC_START_RE = re.compile(
    r"^\s*<(?:!doctype|!--|html\b|head\b|body\b|div\b|section\b|header\b|"
    r"footer\b|nav\b|main\b|span\b|p\b|ul\b|ol\b|table\b|form\b|meta\b|"
    r"link\b|script\b|style\b|h[1-6]\b)",
    re.IGNORECASE,
)

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
# An at-rule is CSS structure even without a `{ ... }` block (e.g. @import).
_CSS_ATRULE_RE = re.compile(
    r"@(?:import|charset|media|font-face|keyframes|supports|namespace|page|"
    r"use|tailwind|apply|layer)\b",
    re.IGNORECASE,
)


def _starts_like_html(text: str) -> bool:
    """True when content opens with an HTML tag/doctype — the tell-tale sign a
    non-HTML file (script.js, styles.css) was filled with HTML by mistake."""
    return bool(_HTML_DOC_START_RE.match(text or ""))


def is_verifiable(path: Path | str) -> bool:
    """Can check_file actually validate this file type on this machine?

    .js/.ts/.css are always verifiable now: even with no node/tsc installed we
    can still catch the common "wrong language / prose dumped into the file"
    failure with the tooling-free content guards below.
    """
    suffix = Path(path).suffix.lower()
    return suffix in (
        ".py",
        ".html",
        ".htm",
        ".js",
        ".ts",
        ".css",
        ".scss",
        ".less",
    )


def check_file(path: Path | str) -> tuple[bool, str]:
    """Cheap correctness check for a just-written file.

    Returns (ok, error). ok=True either means the check passed or that the
    file type is unverifiable here (unknown extension).
    """
    p = Path(path)
    if not p.is_file():
        return False, f"File not found: {p}"
    suffix = p.suffix.lower()
    if suffix == ".py":
        return _check_python(p)
    if suffix in (".js", ".ts"):
        return _check_js_ts(p, suffix)
    if suffix in (".html", ".htm"):
        return _check_html(p)
    if suffix in (".css", ".scss", ".less"):
        return _check_css(p)
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


def _check_js_ts(path: Path, suffix: str) -> tuple[bool, str]:
    """Verify a JS/TS file.

    First a tooling-free wrong-language guard (an HTML document written into a
    .js/.ts file is caught even when node/tsc is absent), then the real syntax
    check via node --check / tsc --noEmit when the binary is available.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    if _starts_like_html(text):
        lang = "JavaScript" if suffix == ".js" else "TypeScript"
        return False, (
            f"{path.name}: content looks like HTML, not {lang} — the file "
            "starts with an HTML tag. Output only the code for this file."
        )
    if suffix == ".js":
        return _check_with_command(path, "node", ["--check"])
    return _check_with_command(path, "tsc", ["--noEmit"])


def _check_css(path: Path) -> tuple[bool, str]:
    """Structural sanity for stylesheets — no external tooling needed.

    Catches the two ways generation corrupts a stylesheet: HTML/JS dumped in
    (it opens with a tag) and plain prose with no CSS syntax at all. An empty
    or comment-only stylesheet is valid.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    if _starts_like_html(text):
        return False, (
            f"{path.name}: content looks like HTML/markup, not CSS — a "
            "stylesheet must contain only CSS rules and selectors."
        )
    body = _CSS_COMMENT_RE.sub("", text).strip()
    if not body:
        return True, ""  # empty or comment-only is valid CSS
    has_rule_block = "{" in body and "}" in body
    if not has_rule_block and not _CSS_ATRULE_RE.search(body):
        return False, (
            f"{path.name}: no CSS rules or at-rules found — the content looks "
            "like prose, not a stylesheet. Output only CSS."
        )
    return True, ""


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
        self.saw_tag = False  # any real tag at all — distinguishes prose files

    def handle_starttag(self, tag: str, attrs) -> None:
        self.saw_tag = True
        if tag not in _HTML_VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.saw_tag = True  # self-closing <br/> etc. still counts as markup

    def handle_endtag(self, tag: str) -> None:
        self.saw_tag = True
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


def _html_surrounding_prose(text: str) -> str:
    """Detect prose leaking OUTSIDE the document — before <!doctype>/<html> or
    after </html> (weaknesses.md #9). Returns an error string, or '' if clean.
    Only fires for full documents (needs the doctype/<html> or </html> anchor),
    so HTML fragments/components are never falsely flagged."""
    lower = text.lower()

    close = lower.rfind("</html>")
    if close != -1:
        tail = _HTML_COMMENT_RE.sub("", text[close + len("</html>") :]).strip()
        if tail:
            return f"unexpected text after </html> (looks like prose): {tail[:80]!r}"

    anchor = -1
    for marker in ("<!doctype", "<html"):
        i = lower.find(marker)
        if i != -1:
            anchor = i if anchor == -1 else min(anchor, i)
    if anchor > 0:
        head = _HTML_COMMENT_RE.sub("", text[:anchor]).strip()
        if head:
            return f"unexpected text before the document (looks like prose): {head[:80]!r}"
    return ""


def _check_html(path: Path) -> tuple[bool, str]:
    """Structural validation: no prose around the document, at least one tag,
    and balanced open/close tags — all without external tooling."""
    text = path.read_text(encoding="utf-8", errors="replace")

    surrounding = _html_surrounding_prose(text)
    if surrounding:
        return False, f"{path.name}: {surrounding}"

    parser = _TagBalanceParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as e:
        return False, f"HTML parse error in {path.name}: {e}"

    visible = _HTML_COMMENT_RE.sub("", text).strip()
    if visible and not parser.saw_tag:
        return False, (
            f"{path.name}: no HTML tags found — the content looks like prose, "
            "not HTML."
        )

    errors = list(parser.errors)
    errors.extend(f"unclosed <{tag}>" for tag in parser.stack)
    if errors:
        return False, f"{path.name}: " + "; ".join(errors[:10])
    return True, ""
