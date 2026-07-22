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

# One whole <a>…</a>, its class attribute, and "strip every tag" — the pieces
# needed to compare two navs and to move the active marker between pages.
_A_TAG_RE = re.compile(r"<a\b[^>]*>.*?</a\s*>", re.IGNORECASE | re.DOTALL)
_CLASS_ATTR_RE = re.compile(
    r"""\bclass\s*=\s*(["'])(.*?)\1""", re.IGNORECASE | re.DOTALL
)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")


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


def replace_nav_block(html: str, new_nav: str) -> str:
    """Swap the page's navigation markup for ``new_nav`` (first block only)."""
    if _NAV_RE.search(html or ""):
        return _NAV_RE.sub(lambda _: new_nav, html, count=1)
    m = _HEADER_RE.search(html or "")
    if m and _HREF_RE.search(m.group(0)):
        return html[: m.start()] + new_nav + html[m.end() :]
    return html


def _normalize_target(href: str) -> str:
    """A nav link's comparable target: bare filename, lowercased, .html implied."""
    raw = (href or "").strip()
    if not raw:
        return ""
    if _EXTERNAL_RE.match(raw):
        return raw.lower()
    target = raw.split("#", 1)[0].split("?", 1)[0].strip().lstrip("/\\")
    if not target:
        return raw.lower()
    target = target.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if target and "." not in target:
        target += ".html"
    return target


def nav_links(nav_html: str) -> list[tuple[str, str]]:
    """``(href, link text)`` for each ``<a>`` in a navigation block, in order."""
    out: list[tuple[str, str]] = []
    for m in _A_TAG_RE.finditer(nav_html or ""):
        tag = m.group(0)
        href_m = _HREF_RE.search(tag)
        href = (href_m.group("val") or "").strip() if href_m else ""
        label = " ".join(_TAG_STRIP_RE.sub(" ", tag).split())
        out.append((href, label))
    return out


def nav_signature(nav_html: str) -> tuple[tuple[str, str], ...]:
    """The comparable identity of a nav: its ``(target, label)`` pairs.

    Normalized so that two pages whose navs differ ONLY in which item carries
    the active class — or in `./about.html` vs `about.html` — compare equal.
    Anything else (a renamed label, a reordered or missing item, a different
    target) makes the signatures differ, which is exactly the "every page has a
    different navbar" failure.
    """
    return tuple(
        (_normalize_target(href), label.lower()) for href, label in nav_links(nav_html)
    )


def _set_class(open_tag: str, value: str) -> str:
    """Set/replace the class attribute of an opening tag (dropping it if empty)."""
    m = _CLASS_ATTR_RE.search(open_tag)
    if m:
        quote = m.group(1)
        return (
            open_tag[: m.start()] + f"class={quote}{value}{quote}" + open_tag[m.end() :]
        )
    if not value:
        return open_tag
    body = open_tag[:-1].rstrip()
    if body.endswith("/"):
        body = body[:-1].rstrip()
        return f'{body} class="{value}" />'
    return f'{body} class="{value}">'


def set_active_link(nav_html: str, page: str) -> str:
    """Re-point the nav's ``active`` class at ``page``.

    Used when one page's nav is replaced by the canonical one: the markup is
    shared, only which item is highlighted is per-page.
    """
    page_base = _normalize_target(page)

    def _fix(m: re.Match) -> str:
        tag = m.group(0)
        open_m = re.match(r"<a\b[^>]*>", tag, re.IGNORECASE | re.DOTALL)
        if not open_m:
            return tag
        open_tag = open_m.group(0)
        classes = []
        cm = _CLASS_ATTR_RE.search(open_tag)
        if cm:
            classes = [c for c in cm.group(2).split() if c.lower() != "active"]
        href_m = _HREF_RE.search(open_tag)
        href = (href_m.group("val") or "") if href_m else ""
        if _normalize_target(href) == page_base and page_base:
            classes.append("active")
        return _set_class(open_tag, " ".join(classes)) + tag[len(open_tag) :]

    return _A_TAG_RE.sub(_fix, nav_html or "")


def _name_key(name: str) -> str:
    """Collapse a filename to what a model was *probably* aiming at.

    `scripts.js` / `script.js` / `Script.js` / `main-script.js` are distinct
    files on disk but the same intent; punctuation and a trailing plural are the
    two ways the local model spells the same asset differently in two places.
    """
    stem = re.sub(r"[^a-z0-9]+", "", Path(name).stem.lower())
    if len(stem) > 3 and stem.endswith("s"):
        stem = stem[:-1]
    return stem


def find_similar_file(target: Path | str, project_root: Path | str) -> Path | None:
    """An existing file that a missing reference almost certainly MEANT.

    `_repair_dead_references` creates whatever a page points at, so a build
    whose HTML says `scripts.js` while the plan wrote `script.js` ends up with
    two scripts of overlapping purpose. Before creating, look for the file the
    reference was a near-miss for: same extension, same collapsed name, in the
    reference's own directory (then the project root). Deliberately strict — a
    genuinely different name like `main.css` vs `styles.css` is NOT matched.
    """
    target = Path(target)
    root = Path(project_root)
    ext = target.suffix.lower()
    key = _name_key(target.name)
    if not ext or not key:
        return None
    for directory in (target.parent, root):
        try:
            if not directory.is_dir():
                continue
            candidates = sorted(
                p
                for p in directory.iterdir()
                if p.is_file()
                and p.suffix.lower() == ext
                and _name_key(p.name) == key
                and p.resolve() != target.resolve()
            )
        except OSError:
            continue
        if candidates:
            return candidates[0]
    return None


def rewrite_reference(text: str, old_ref: str, new_ref: str) -> tuple[str, int]:
    """Point every occurrence of ``old_ref`` at ``new_ref``. Returns (text, n).

    Only attribute/import values are touched — a quoted value (optionally
    carrying a `?query`/`#fragment`, which is preserved) or a bare CSS
    ``url(...)`` — so the same string elsewhere in the file is left alone.
    """
    if not old_ref or old_ref == new_ref:
        return text, 0
    count = 0

    def _quoted(m: re.Match) -> str:
        nonlocal count
        count += 1
        suffix = m.group("val")[len(old_ref) :]
        return f'{m.group("q")}{new_ref}{suffix}{m.group("q")}'

    out = re.sub(
        r"""(?P<q>["'])(?P<val>""" + re.escape(old_ref) + r"""(?:[?#][^"']*)?)(?P=q)""",
        _quoted,
        text,
    )

    def _url(m: re.Match) -> str:
        nonlocal count
        count += 1
        return f"url({new_ref})"

    out = re.sub(
        r"\burl\(\s*" + re.escape(old_ref) + r"\s*\)", _url, out, flags=re.IGNORECASE
    )
    return out, count


def find_broken_page_links(
    file_path: Path | str, project_root: Path | str | None = None
) -> list[tuple[str, str]]:
    """`<a href>` targets that point at a real sibling page in a broken FORM.

    Distinct from find_dead_references, which reports links whose file is
    MISSING. These links have a file — the href just can't reach it from a
    static page opened over file://:

      * root-absolute — ``href="/about.html"`` (there is no web root)
      * extensionless — ``href="about"`` (no server to add .html)

    Returns ``(href_as_written, corrected_href)`` pairs, and only when the
    corrected target actually exists next to the file, so a genuine route in a
    server-rendered app is never rewritten.
    """
    p = Path(file_path)
    if p.suffix.lower() not in (".html", ".htm"):
        return []
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    base = p.parent
    root = Path(project_root).resolve() if project_root is not None else None
    fixes: list[tuple[str, str]] = []
    seen: set[str] = set()

    for m in _HREF_RE.finditer(text):
        if m.group("tag").lower() != "a":
            continue
        raw = (m.group("val") or "").strip()
        if not raw or raw in seen or _EXTERNAL_RE.match(raw):
            continue
        target = raw.split("#", 1)[0].split("?", 1)[0].strip()
        if not target:
            continue

        candidate = target.lstrip("/\\")  # root-absolute → same-dir
        if not Path(candidate).suffix:
            candidate = f"{candidate}.html"  # extensionless → add .html
        if candidate == target:
            continue  # already a well-formed relative link

        try:
            resolved = (base / candidate).resolve()
        except Exception:
            continue
        if root is not None:
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
        if not resolved.is_file():
            continue  # no such page → a real route, leave it alone

        seen.add(raw)
        fixes.append((raw, candidate + raw[len(target) :]))  # keep #frag/?query
    return fixes


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
