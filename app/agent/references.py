"""Cross-file reference checking — make "verified OK" mean "it resolves".

The single-file `verify.check_file` proves a file *parses*; it says nothing
about whether the files it points at *exist*. The local model routinely emits a
build whose `index.html` links a `script.js`/`styles.css` it never created
(weaknesses.md #2/#3) — the page loads broken and nothing notices.

These pure helpers extract the LOCAL references a file makes and report the ones
missing on disk, so the agent can create the absent files. Only same-project
references are considered:

  * HTML — `<script src>`, `<link href>`, `<img/source/iframe/audio/video src>`,
    and `<a href>` (only when it targets an .html page, not a route).
  * CSS  — `@import "…"` / `@import url(…)` and `url(…)` in declarations.
  * JS/TS — RELATIVE imports only (`./x`, `../x` via import/from/require), with
    the usual extension/`index` resolution so an extensionless import isn't a
    false alarm.

External URLs (`http:`, `//cdn`, `data:`, `mailto:`, `tel:`, `#anchor`),
root-absolute paths (`/static/…` — the web root is unknown), and bare npm-style
import specifiers are all ignored, so nothing off-disk is ever flagged.
"""

from __future__ import annotations

import re
from pathlib import Path

# <a href> / <link href>
_HREF_RE = re.compile(
    r"""<(?P<tag>a|link)\b[^>]*?\bhref\s*=\s*(?P<q>["'])(?P<val>.*?)(?P=q)""",
    re.IGNORECASE | re.DOTALL,
)
# <script src> / <img src> / <source src> / <iframe src> / media src
_SRC_RE = re.compile(
    r"""<(?P<tag>script|img|source|iframe|audio|video|track)\b[^>]*?"""
    r"""\bsrc\s*=\s*(?P<q>["'])(?P<val>.*?)(?P=q)""",
    re.IGNORECASE | re.DOTALL,
)
# @import "x.css"  /  @import url("x.css")  /  @import url(x.css)
_CSS_IMPORT_RE = re.compile(
    r"""@import\s+(?:url\(\s*)?(?P<q>["']?)(?P<val>[^"')\s]+)(?P=q)""",
    re.IGNORECASE,
)
# url(x)  /  url("x")  /  url('x')  — background images, fonts, etc.
_CSS_URL_RE = re.compile(
    r"""\burl\(\s*(?P<q>["']?)(?P<val>[^"')]+?)(?P=q)\s*\)""",
    re.IGNORECASE,
)
# import x from './y'  /  import './y'  /  require('./y')  /  import('./y')
# Only RELATIVE specifiers (./ or ../) — bare names are npm packages.
_JS_IMPORT_RE = re.compile(
    r"""(?:\bfrom\b|\brequire\s*\(|\bimport\s*\(|\bimport\b)\s*"""
    r"""(?P<q>["'])(?P<val>\.{1,2}/[^"']+)(?P=q)"""
)

# Anything with a URI scheme, protocol-relative, a pure anchor, or an inline
# data/mail/tel/js target is NOT a local file.
_EXTERNAL_RE = re.compile(
    r"^(?:[a-z][a-z0-9+.\-]*:|//|#|data:|mailto:|tel:|javascript:)",
    re.IGNORECASE,
)

# Text files we can meaningfully generate to satisfy a dead reference.
_CREATABLE_EXTS = {
    ".css",
    ".scss",
    ".less",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".html",
    ".htm",
    ".json",
    ".svg",
}
# Extensions tried when resolving an extensionless JS/TS relative import.
_JS_CANDIDATE_EXTS = (".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx")
# Referencing file types worth scanning for outbound references.
REF_SCANNED_EXTS = {
    ".html",
    ".htm",
    ".css",
    ".scss",
    ".less",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".jsx",
    ".tsx",
}


# The site-wide navigation block. <nav> is the semantic element; a <header>
# containing links is the common fallback the local model emits instead.
_NAV_RE = re.compile(r"<nav\b[^>]*>.*?</nav\s*>", re.IGNORECASE | re.DOTALL)
_HEADER_RE = re.compile(r"<header\b[^>]*>.*?</header\s*>", re.IGNORECASE | re.DOTALL)


def extract_nav_block(html: str) -> str | None:
    """The page's navigation markup, or None if it has none.

    Threading this one block into later pages is far cheaper — and a far
    stronger signal — than pasting whole sibling files and hoping the nav
    survives truncation.
    """
    m = _NAV_RE.search(html or "")
    if m:
        return m.group(0)
    m = _HEADER_RE.search(html or "")
    # A <header> only counts as navigation when it actually links somewhere.
    if m and _HREF_RE.search(m.group(0)):
        return m.group(0)
    return None


def is_creatable(ref: str) -> bool:
    """True when a missing reference is a text file we can generate (vs. a
    binary asset like a .png/.woff we should only report)."""
    ext = Path(ref).suffix.lower()
    if not ext:
        return True  # extensionless → treated as a JS module (creatable)
    return ext in _CREATABLE_EXTS


def _clean_ref(val: str) -> str | None:
    """Normalize a raw attribute/import value to a local relative path, or None
    if it isn't a local reference (external, anchor, root-absolute, empty)."""
    v = (val or "").strip()
    if not v or _EXTERNAL_RE.match(v):
        return None
    if v.startswith("/") or v.startswith("\\"):
        return None  # root-absolute: web root is unknown, don't guess
    v = v.split("#", 1)[0].split("?", 1)[0].strip()
    return v or None


def extract_local_references(text: str, suffix: str) -> list[str]:
    """Local references made by ``text`` (a file of type ``suffix``), in order,
    de-duplicated. Empty for file types we don't scan."""
    suffix = suffix.lower()
    refs: list[str] = []

    if suffix in (".html", ".htm"):
        for m in _HREF_RE.finditer(text):
            v = _clean_ref(m.group("val"))
            # An <a href> is a page link only when it targets an actual .html
            # file; bare routes ("/about", "contact") are not files.
            if v and (
                m.group("tag").lower() != "a" or v.lower().endswith((".html", ".htm"))
            ):
                refs.append(v)
        for m in _SRC_RE.finditer(text):
            v = _clean_ref(m.group("val"))
            if v:
                refs.append(v)
    elif suffix in (".css", ".scss", ".less"):
        for regex in (_CSS_IMPORT_RE, _CSS_URL_RE):
            for m in regex.finditer(text):
                v = _clean_ref(m.group("val"))
                if v:
                    refs.append(v)
    elif suffix in (".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx"):
        for m in _JS_IMPORT_RE.finditer(text):
            v = _clean_ref(m.group("val"))
            if v:
                refs.append(v)

    seen: set[str] = set()
    out: list[str] = []
    for r in refs:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _resolves(resolved: Path, referencing_suffix: str) -> bool:
    """Does ``resolved`` exist, allowing JS/TS import extension/index fallbacks?"""
    if resolved.exists():
        return True
    if referencing_suffix.lower() in (".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx"):
        if not resolved.suffix:
            for ext in _JS_CANDIDATE_EXTS:
                if resolved.with_suffix(ext).exists():
                    return True
                if (resolved / f"index{ext}").exists():
                    return True
    return False


def find_dead_references(
    file_path: Path | str, project_root: Path | str | None = None
) -> list[tuple[str, Path]]:
    """Local references in ``file_path`` that don't exist on disk.

    Returns ``(reference_as_written, resolved_absolute_path)`` pairs. References
    are resolved relative to the file's own directory; any that resolve outside
    ``project_root`` (when given) are skipped so nothing outside the sandbox is
    ever touched.
    """
    p = Path(file_path)
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    base = p.parent
    root = Path(project_root).resolve() if project_root is not None else None
    dead: list[tuple[str, Path]] = []
    for ref in extract_local_references(text, p.suffix):
        try:
            resolved = (base / ref).resolve()
        except Exception:
            continue
        if root is not None:
            try:
                resolved.relative_to(root)
            except ValueError:
                continue  # escapes the project root → leave it alone
        if _resolves(resolved, p.suffix):
            continue
        dead.append((ref, resolved))
    return dead
