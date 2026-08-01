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

# --- external assets (offline builds) --------------------------------------
# Coder is offline, but the sites it GENERATES were not: a Google Fonts <link>
# or a Tailwind/Bootstrap CDN <script> makes the page depend on a network that
# isn't there. Offline the browser blocks on a dead DNS lookup and then renders
# with default fonts — or, for a CDN stylesheet, completely unstyled. Both are
# invisible to every other check: the file parses, the reference passes (
# `references.py` deliberately ignores external URLs), and it ships.
#
# Matches absolute (`https://…`) and protocol-relative (`//cdn…`) URLs only, so
# a local `href="css/style.css"` is untouched. `<a href>` is NOT matched — a
# hyperlink to a real website is legitimate and must stay.
_EXTERNAL_URL = r"""["'](?:https?:)?//[^"']+["']"""
_EXTERNAL_LINK_RE = re.compile(
    r"<link\b[^>]*?\bhref\s*=\s*" + _EXTERNAL_URL + r"[^>]*>",
    re.IGNORECASE,
)
_EXTERNAL_SCRIPT_RE = re.compile(
    r"<script\b[^>]*?\bsrc\s*=\s*" + _EXTERNAL_URL + r"[^>]*>\s*(?:</script\s*>)?",
    re.IGNORECASE,
)
_EXTERNAL_CSS_IMPORT_RE = re.compile(
    r"@import\s+(?:url\(\s*)?['\"]?(?:https?:)?//[^'\")\s;]+['\"]?\s*\)?\s*;?",
    re.IGNORECASE,
)


# A <form> containing a file input MUST carry enctype="multipart/form-data".
# Without it the browser posts only the filename, so `request.files[...]` raises
# and the upload silently never happens — the plan calls this "the single most
# likely way the live demo embarrasses you" (Phase 4b). Deterministic to detect
# and to fix, so neither is left to the model.
_FORM_RE = re.compile(r"<form\b[^>]*>.*?</form\s*>", re.IGNORECASE | re.DOTALL)
_FORM_OPEN_RE = re.compile(r"<form\b[^>]*>", re.IGNORECASE)
_FILE_INPUT_RE = re.compile(r"<input\b[^>]*\btype\s*=\s*[\"']file[\"']", re.IGNORECASE)
_ENCTYPE_RE = re.compile(r"\benctype\s*=", re.IGNORECASE)
_METHOD_POST_RE = re.compile(r"\bmethod\s*=\s*[\"']post[\"']", re.IGNORECASE)


def forms_missing_enctype(text: str) -> list[str]:
    """Opening <form> tags that take a file upload but never declare enctype."""
    out: list[str] = []
    for form in _FORM_RE.finditer(text or ""):
        block = form.group(0)
        if not _FILE_INPUT_RE.search(block):
            continue
        open_tag = _FORM_OPEN_RE.match(block)
        if open_tag and not _ENCTYPE_RE.search(open_tag.group(0)):
            out.append(open_tag.group(0))
    return out


def fix_form_enctype(text: str) -> tuple[str, int]:
    """Add the missing `enctype` (and `method="post"`) to file-upload forms.

    Returns ``(new_text, how_many_fixed)``. Purely additive — no existing
    attribute is touched — so it cannot change a form that was already correct.
    """
    broken = forms_missing_enctype(text)
    if not broken:
        return text, 0
    out = text
    for open_tag in broken:
        addition = ' enctype="multipart/form-data"'
        if not _METHOD_POST_RE.search(open_tag):
            addition = ' method="post"' + addition
        fixed = open_tag[:-1].rstrip() + addition + ">"
        out = out.replace(open_tag, fixed, 1)
    return out, len(broken)


def find_external_assets(text: str, suffix: str) -> list[str]:
    """Render-blocking off-machine assets in a generated file.

    Returns the matched tags/at-rules (trimmed), or [] when clean.
    """
    out: list[str] = []
    if suffix in (".html", ".htm"):
        for pattern in (_EXTERNAL_LINK_RE, _EXTERNAL_SCRIPT_RE):
            out.extend(m.group(0).strip() for m in pattern.finditer(text or ""))
    elif suffix in (".css", ".scss", ".less"):
        out.extend(
            m.group(0).strip() for m in _EXTERNAL_CSS_IMPORT_RE.finditer(text or "")
        )
    return out


def strip_external_assets(text: str, suffix: str) -> tuple[str, list[str]]:
    """Remove those assets. Returns (new_text, what_was_removed).

    Deterministic — no LLM, same shape as the other content guards. Removing a
    dead <link>/<script> cannot break a page: the resource was never going to
    load. The styling intent survives because the build spec states the font as
    a system stack (see buildspec.to_context_block) rather than a CDN family.
    """
    removed = find_external_assets(text, suffix)
    if not removed:
        return text, []
    out = text
    if suffix in (".html", ".htm"):
        out = _EXTERNAL_LINK_RE.sub("", out)
        out = _EXTERNAL_SCRIPT_RE.sub("", out)
    elif suffix in (".css", ".scss", ".less"):
        out = _EXTERNAL_CSS_IMPORT_RE.sub("", out)
    # Collapse the blank lines the removal leaves behind.
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out, removed


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
            return (
                f"unexpected text before the document (looks like prose): {head[:80]!r}"
            )
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
