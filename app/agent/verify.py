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
import tempfile
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

# A template expression inside a tag: `<% ... %>` (EJS) or `{% ... %}` / `{{ ... }}`
# (Jinja). These matter because **they can contain `>`** — `<form action="<%= u %>">`
# and `{% if a > b %}` both do — and every regex above scans an attribute list
# with `[^>]*`, which stops dead at that inner `>`. The match is then a truncated
# half-tag, and a pass that WRITES (`fix_form_enctype`, `strip_external_assets`)
# writes the truncation back: `<form action="<%= u % enctype="...">`. Masking is
# the whole fix, and it must preserve LENGTH so a span found in the masked text
# still addresses the same characters in the real one.
_TEMPLATE_TAG_RE = re.compile(r"<%.*?%>|\{%.*?%\}|\{\{.*?\}\}", re.DOTALL)


def mask_template_tags(text: str) -> str:
    """``text`` with template expressions blanked to spaces, same length.

    For regexes that assume plain HTML. Spaces are the safe filler: they are
    already legal between attributes and inside a quoted value, so nothing that
    was one token becomes two. A file with no template tags is returned
    unchanged, which is why every caller is a no-op on ordinary HTML.
    """

    def blank(match: re.Match) -> str:
        return " " * (match.end() - match.start())

    return _TEMPLATE_TAG_RE.sub(blank, text or "")


def forms_missing_enctype(text: str) -> list[str]:
    """Opening <form> tags that take a file upload but never declare enctype.

    Detection runs on the masked text and the result is sliced out of the REAL
    one, so what comes back is the true opening tag — template expressions
    intact — and `fix_form_enctype` can substitute it safely.
    """
    source = text or ""
    scan = mask_template_tags(source)
    out: list[str] = []
    for form in _FORM_RE.finditer(scan):
        block = form.group(0)
        if not _FILE_INPUT_RE.search(block):
            continue
        open_tag = _FORM_OPEN_RE.match(block)
        if open_tag and not _ENCTYPE_RE.search(open_tag.group(0)):
            start = form.start() + open_tag.start()
            out.append(source[start : form.start() + open_tag.end()])
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


# `.ejs` is a markup file for this purpose: a view can carry a CDN <link> exactly
# as a Jinja page can, and offline that is the same dead DNS lookup per request.
_MARKUP_EXTS = (".html", ".htm", ".ejs")
_STYLE_EXTS = (".css", ".scss", ".less")


def _external_asset_patterns(suffix: str) -> tuple[re.Pattern, ...]:
    if suffix in _MARKUP_EXTS:
        return (_EXTERNAL_LINK_RE, _EXTERNAL_SCRIPT_RE)
    if suffix in _STYLE_EXTS:
        return (_EXTERNAL_CSS_IMPORT_RE,)
    return ()


def _external_asset_spans(text: str, suffix: str) -> list[tuple[int, int]]:
    """Where the off-machine assets are, measured on the MASKED text.

    Masked for `mask_template_tags`' reason: an attribute after the URL can hold
    a template expression, and `[^>]*>` would then end the match at the `>`
    inside it — deleting half a tag and leaving the rest behind.
    """
    scan = mask_template_tags(text or "")
    spans = [
        (m.start(), m.end())
        for pattern in _external_asset_patterns(suffix)
        for m in pattern.finditer(scan)
    ]
    return sorted(spans)


def find_external_assets(text: str, suffix: str) -> list[str]:
    """Render-blocking off-machine assets in a generated file.

    Returns the matched tags/at-rules (trimmed), or [] when clean.
    """
    source = text or ""
    return [source[s:e].strip() for s, e in _external_asset_spans(source, suffix)]


def strip_external_assets(text: str, suffix: str) -> tuple[str, list[str]]:
    """Remove those assets. Returns (new_text, what_was_removed).

    Deterministic — no LLM, same shape as the other content guards. Removing a
    dead <link>/<script> cannot break a page: the resource was never going to
    load. The styling intent survives because the build spec states the font as
    a system stack (see buildspec.to_context_block) rather than a CDN family.

    Cuts by SPAN rather than by `re.sub` on the raw text, so the region deleted
    is exactly the region that was matched on the masked text — the two can
    differ around a template expression, and the difference is a corrupted file.
    """
    source = text or ""
    spans = _external_asset_spans(source, suffix)
    if not spans:
        return text, []
    removed = [source[s:e].strip() for s, e in spans]
    out = source
    for start, end in reversed(spans):  # right to left: earlier spans stay valid
        out = out[:start] + out[end:]
    # Collapse the blank lines the removal leaves behind.
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out, removed


# --- Flask endpoints (Phase W2, docs/web-quality-plan.md) -------------------
# `{{ url_for('products') }}` against a view actually named `product_list` is a
# Jinja BuildError, which is a 500 on that page — and nothing in the codebase
# looked at it: `references.py` deliberately skips `url_for` (it cannot tell a
# route from a file path, and rewriting one corrupts the template), the syntax
# check only balances tags, and the functional probe sees the 500 without ever
# being able to say the endpoint NAME was the problem.
#
# Same split as everywhere else here: a near-miss is a naming slip and gets
# rewritten; anything else is reported, never invented. Synthesizing the missing
# route would be generation, and `_verify_blueprint_coverage` already owns that.

_URL_FOR_RE = re.compile(
    r"url_for\(\s*(?P<q>['\"])(?P<name>[A-Za-z_][A-Za-z0-9_.]*)(?P=q)"
)

# Flask registers this itself; it is never defined in app.py, so it must not be
# reported as missing.
_BUILTIN_ENDPOINTS = frozenset({"static"})

_FORM_ACTION_RE = re.compile(
    r"\baction\s*=\s*(?P<q>['\"])(?P<val>.*?)(?P=q)", re.IGNORECASE | re.DOTALL
)
_FORM_METHOD_RE = re.compile(
    r"\bmethod\s*=\s*['\"](?P<m>[A-Za-z]+)['\"]", re.IGNORECASE
)


def _endpoint_key(name: str) -> str:
    """Collapse an endpoint name for near-miss matching.

    Mirrors `references._name_key`: punctuation dropped and one trailing plural
    collapsed, so `product_list` and `productlist` and `products`/`product` all
    meet. Deliberately strict about nothing else — `add_product` and
    `edit_product` must NOT match, because sending a form to the wrong handler
    is worse than the 500 this pass replaces.
    """
    key = re.sub(r"[^a-z0-9]+", "", (name or "").lower())
    if len(key) > 3 and key.endswith("s"):
        key = key[:-1]
    return key


def endpoints_referenced(text: str) -> list[str]:
    """Every endpoint name a template passes to `url_for`, de-duplicated."""
    out: list[str] = []
    for match in _URL_FOR_RE.finditer(text or ""):
        name = match.group("name")
        if name not in out:
            out.append(name)
    return out


def unresolved_endpoints(text: str, known: set[str] | frozenset[str]) -> list[str]:
    """`url_for` targets that no view function defines.

    ``known`` is read off the file on disk, never off the ProjectSpec: the spec
    is additive by design (`reconcile_with_disk` never deletes), so it can name
    a route this turn's edit removed — and a check that trusts it would then
    stay silent on the one page that is now broken.
    """
    names = set(known or ()) | _BUILTIN_ENDPOINTS
    return [n for n in endpoints_referenced(text) if n not in names]


def fix_endpoint_names(
    text: str, known: set[str] | frozenset[str]
) -> tuple[str, list[tuple[str, str]]]:
    """Repoint `url_for` calls that near-miss a real view. Returns (text, fixes).

    Rewrites ONLY when exactly one known endpoint collapses to the same key —
    the same "two candidates means guessing" rule `_resolve_target_from_spec`
    follows. Everything else is left for `unresolved_endpoints` to report.
    """
    missing = unresolved_endpoints(text, known)
    if not missing:
        return text, []
    by_key: dict[str, list[str]] = {}
    for name in known or ():
        by_key.setdefault(_endpoint_key(name), []).append(name)

    out = text
    fixes: list[tuple[str, str]] = []
    for name in missing:
        candidates = by_key.get(_endpoint_key(name)) or []
        if len(candidates) != 1:
            continue
        replacement = candidates[0]
        # Case-sensitive: `Products` and `products` are different Python names.
        pattern = re.compile(r"(url_for\(\s*)(['\"])" + re.escape(name) + r"\2")
        new, count = pattern.subn(
            lambda m: f"{m.group(1)}{m.group(2)}{replacement}{m.group(2)}", out
        )
        if count:
            out = new
            fixes.append((name, replacement))
    return out, fixes


# --- link validation for path-routed stacks (Phase N4) ----------------------
# Flask pages name a route by its VIEW (`url_for('products')`), so W2 validates
# names. An EJS view names it by its PATH (`href="/products"`), so the same
# class of defect — a link to a route nobody defined — is a different lookup.
# The near-miss repair rule transfers unchanged: exactly one candidate, or
# report and leave it alone.

_HREF_RE = re.compile(
    r"""\b(?P<attr>href|action)\s*=\s*(?P<q>["'])(?P<val>[^"']*)(?P=q)""", re.I
)
# A path with a file extension is an asset served by the static mount, not a
# route — `/css/style.css` must never be reported as a missing page.
_ASSET_PATH_RE = re.compile(r"\.[A-Za-z0-9]{1,6}$")
# `:id` in Express, `<int:id>` in Flask — a parameterised segment matches
# anything, so `/products/:id` serves `/products/5`.
_PARAM_SEG_RE = re.compile(r"^(?::[A-Za-z_]\w*|<[^>]+>)$")


def _route_matches(route: str, link: str) -> bool:
    """Does ``link`` hit ``route``, allowing for parameterised segments."""
    r = [s for s in (route or "").split("/") if s]
    l = [s for s in (link or "").split("/") if s]
    if len(r) != len(l):
        return False
    return all(_PARAM_SEG_RE.match(a) or a == b for a, b in zip(r, l))


def local_links(text: str) -> list[str]:
    """Site-internal `href`/`action` paths worth checking, de-duplicated.

    Everything that is not a route this project serves is dropped rather than
    guessed at — external URLs, protocol-relative `//cdn`, `#anchor`, `mailto:`,
    a relative path (which is the model's business, not a route's), and anything
    ending in a file extension (the static mount serves it). What survives is
    exactly the shape "a link to one of this app's own pages".
    """
    out: list[str] = []
    for match in _HREF_RE.finditer(text or ""):
        value = (match.group("val") or "").strip()
        if not value.startswith("/") or value.startswith("//"):
            continue  # external, relative, anchor, mailto:, or a template expr
        path = value.split("?", 1)[0].split("#", 1)[0]
        if not path or "<%" in path or "{{" in path:
            continue  # built at render time — its target cannot be known here
        if _ASSET_PATH_RE.search(path):
            continue  # a static file, not a route
        if path not in out:
            out.append(path)
    return out


def unresolved_links(text: str, routes: list[tuple[str, str, str, str]]) -> list[str]:
    """Internal links that no route on disk serves.

    ``routes`` is read off the server file, never off the ProjectSpec, for
    `unresolved_endpoints`' reason: the spec is additive and can name a route
    this turn's edit removed, so a check trusting it goes quiet on exactly the
    page that just broke.

    Returns [] when the file defines no routes at all — that means the parser
    could not read it, and reporting every link on the page as broken would be
    the false-failure flood, not a finding.
    """
    known = [p for _m, p, _v, _t in routes or ()]
    if not known:
        return []
    return [
        link
        for link in local_links(text)
        if not any(_route_matches(route, link) for route in known)
    ]


def fix_link_targets(
    text: str, routes: list[tuple[str, str, str, str]]
) -> tuple[str, list[tuple[str, str]]]:
    """Repoint a link that near-misses a real route. Returns (text, fixes).

    `references._name_key`'s rule, which W2 already relies on: punctuation
    dropped and one trailing plural collapsed, so `/product` -> `/products` is a
    naming slip while `/edit_product` -> `/add_product` is a different page and
    is left alone. Rewrites ONLY when exactly one route collapses to the same
    key — sending a link to the wrong page is worse than the 404 it replaces.
    """
    from app.agent.references import _name_key

    missing = unresolved_links(text, routes)
    if not missing:
        return text, []

    by_key: dict[str, set[str]] = {}
    for _m, path, _v, _t in routes or ():
        by_key.setdefault(_name_key(path), set()).add(path)

    out = text
    fixes: list[tuple[str, str]] = []
    for link in missing:
        candidates = sorted(by_key.get(_name_key(link)) or ())
        if len(candidates) != 1 or candidates[0] == link:
            continue
        replacement = candidates[0]
        pattern = re.compile(
            r"""((?:href|action)\s*=\s*)(["'])""" + re.escape(link) + r"""\2""",
            re.IGNORECASE,
        )
        new, count = pattern.subn(
            lambda m: f"{m.group(1)}{m.group(2)}{replacement}{m.group(2)}", out
        )
        if count:
            out = new
            fixes.append((link, replacement))
    return out, fixes


def form_method_mismatches_by_path(
    text: str, routes: list[tuple[str, str, str, str]]
) -> list[str]:
    """`form_method_mismatches` for a stack whose forms name a PATH.

    Same 405 that every other check passes — valid HTML, a page that renders, a
    route that exists — and the same rule about forms with no action: which
    route it posts to cannot be known from the view, and a false failure here
    sends the repair loop at working code.
    """
    accepted: dict[str, set[str]] = {}
    for method, path, _view, _tpl in routes or ():
        accepted.setdefault(path, set()).add((method or "GET").upper())
    if not accepted:
        return []

    out: list[str] = []
    for open_tag in _FORM_OPEN_RE.finditer(text or ""):
        tag = open_tag.group(0)
        action = _FORM_ACTION_RE.search(tag)
        if not action:
            continue
        target = (action.group("val") or "").split("?", 1)[0].strip()
        if not target.startswith("/") or "<%" in target:
            continue
        method_attr = _FORM_METHOD_RE.search(tag)
        method = (method_attr.group("m") if method_attr else "get").upper()
        # UNION across every matching route, not the first match. `/products/:id`
        # also matches `/products/new`, so taking the first hit reported a
        # genuine `POST /products/new` handler as a 405 — a false failure on
        # correct code, which is the thing this file exists not to do. The
        # server tries each route in turn, so the request 405s only when NONE of
        # them accepts the method.
        allowed: set[str] = set()
        for path, methods in accepted.items():
            if _route_matches(path, target):
                allowed |= methods
        if not allowed or method in allowed:
            continue
        out.append(
            f"may not meet: the form posting {method} to {target} will 405 — "
            f"that route only accepts {', '.join(sorted(allowed))}"
        )
    return out


def form_method_mismatches(
    text: str, routes: list[tuple[str, str, str, str]]
) -> list[str]:
    """Forms whose method the route they post to does not accept.

    `<form method="post" action="{{ url_for('add') }}">` against
    `@app.route("/add")` — which is GET-only unless `methods=` says otherwise —
    is a 405. Every check that exists passes it: the HTML is valid, the page
    renders, the route is defined, and the functional probe posts to routes from
    the spec rather than to the action the form actually names.

    Reported, never fixed: the repair is `methods=["GET", "POST"]` in app.py, a
    Python edit, and this pass runs per-HTML-file.
    """
    accepted: dict[str, set[str]] = {}
    for method, _path, view, _tpl in routes or ():
        accepted.setdefault(view, set()).add((method or "GET").upper())
    if not accepted:
        return []

    out: list[str] = []
    for open_tag in _FORM_OPEN_RE.finditer(text or ""):
        tag = open_tag.group(0)
        action = _FORM_ACTION_RE.search(tag)
        if not action:
            continue  # posts to its own URL — which route that is, we can't know
        target = _URL_FOR_RE.search(action.group("val"))
        if not target:
            continue
        view = target.group("name")
        methods = accepted.get(view)
        if not methods:
            continue  # unknown endpoint — `unresolved_endpoints` owns that
        method_attr = _FORM_METHOD_RE.search(tag)
        used = (method_attr.group("m") if method_attr else "GET").upper()
        if used not in methods:
            out.append(
                f"form uses method=\"{used.lower()}\" to url_for('{view}'), but "
                f"that route accepts {'/'.join(sorted(methods))} only"
            )
    return out


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
        ".ejs",
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
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return False, f"{type(e).__name__} reading {p.name}: {e}"
    ok, error = check_text(text, suffix, p.name)
    if not ok:
        return ok, error
    # The half that needs a real file and a real binary. Everything above is
    # tooling-free, which is why a candidate held only in memory can be scored
    # with exactly the same rules (Phase W9's best-of-N).
    if suffix == ".js":
        return _check_with_command(p, "node", ["--check"])
    if suffix == ".ts":
        return _check_with_command(p, "tsc", ["--noEmit"])
    if suffix == ".ejs":
        return _check_ejs_javascript(text, p.name)
    return True, ""


def check_text(text: str, suffix: str, name: str = "the file") -> tuple[bool, str]:
    """`check_file`'s tooling-free half, against a string.

    Same rules, no disk and no subprocess — which is what lets Phase W9 score
    several candidate generations against each other before any of them is
    written. For `.js`/`.ts` this is the content guard only: the syntax check
    needs `node`/`tsc`, which need a path, and a candidate has none yet.
    """
    suffix = (suffix or "").lower()
    if suffix == ".py":
        return _check_python_text(text, name)
    if suffix in (".js", ".ts"):
        return _check_js_text(text, suffix, name)
    if suffix in (".html", ".htm"):
        return _check_html_text(text, name)
    if suffix == ".ejs":
        return _check_ejs_text(text, name)
    if suffix in (".css", ".scss", ".less"):
        return _check_css_text(text, name)
    return True, ""


def _check_python_text(source: str, name: str) -> tuple[bool, str]:
    """Syntax-check Python via compile() — no subprocess, never executes code."""
    try:
        compile(source, name, "exec")
        return True, ""
    except SyntaxError as e:
        return False, f"SyntaxError in {name}, line {e.lineno}: {e.msg}"
    except Exception as e:
        return False, f"{type(e).__name__} in {name}: {e}"


def _check_js_text(text: str, suffix: str, name: str) -> tuple[bool, str]:
    """The tooling-free half of a JS/TS check: is this even the right language?

    An HTML document written into a .js/.ts file is caught even when node/tsc is
    absent, which is why `.js`/`.ts` are always verifiable.
    """
    if _starts_like_html(text):
        lang = "JavaScript" if suffix == ".js" else "TypeScript"
        return False, (
            f"{name}: content looks like HTML, not {lang} — the file "
            "starts with an HTML tag. Output only the code for this file."
        )
    return True, ""


def _check_css_text(text: str, name: str) -> tuple[bool, str]:
    """Structural sanity for stylesheets — no external tooling needed.

    Catches the two ways generation corrupts a stylesheet: HTML/JS dumped in
    (it opens with a tag) and plain prose with no CSS syntax at all. An empty
    or comment-only stylesheet is valid.
    """
    if _starts_like_html(text):
        return False, (
            f"{name}: content looks like HTML/markup, not CSS — a "
            "stylesheet must contain only CSS rules and selectors."
        )
    body = _CSS_COMMENT_RE.sub("", text).strip()
    if not body:
        return True, ""  # empty or comment-only is valid CSS
    has_rule_block = "{" in body and "}" in body
    if not has_rule_block and not _CSS_ATRULE_RE.search(body):
        return False, (
            f"{name}: no CSS rules or at-rules found — the content looks "
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


def strip_ejs(text: str) -> tuple[str, str]:
    """``(the HTML underneath, delimiter error)`` for an EJS view.

    An EJS tag opens with `<%` and closes with `%>`; `<%%` is the escape for a
    literal `<%`. Everything between is JavaScript, so it must come OUT before
    the markup can be balanced — `<% if (a) { %>` is not an element.

    The delimiter check is the point. An unterminated `<%` is the characteristic
    EJS syntax error and it takes the whole page down at render time with
    "Could not find matching close tag" — measured while building the Node
    scaffold, where `<%# … #%>` (there is no `#%>`) killed the home page. Nothing
    else in the pipeline can see it: the file is valid-ish HTML, `node --check`
    does not read it, and the browser never gets that far.
    """
    out: list[str] = []
    i, n = 0, len(text or "")
    while i < n:
        if text.startswith("<%%", i):  # the literal-`<%` escape
            out.append("<%")
            i += 3
            continue
        if text.startswith("<%", i):
            end = text.find("%>", i + 2)
            if end == -1:
                snippet = " ".join(text[i : i + 40].split())
                return "", (
                    f"unterminated EJS tag — `<%` with no matching `%>`: {snippet!r}"
                )
            # A scriptlet leaves nothing behind; an output tag leaves a value,
            # which for balance purposes is just text.
            out.append(" ")
            i = end + 2
            continue
        if text.startswith("%>", i):
            snippet = " ".join(text[max(0, i - 40) : i + 2].split())
            return "", f"stray `%>` with no matching `<%`: {snippet!r}"
        out.append(text[i])
        i += 1
    return "".join(out), ""


def ejs_script(text: str) -> str:
    """The JavaScript an EJS view compiles to, as one syntax-checkable program.

    `strip_ejs` throws this half away — it takes the JavaScript OUT so the
    markup underneath can be balanced. Nothing then looked at the JavaScript,
    and it is where EJS views actually break: measured on a live build, one view
    shipped `<%- users.forEach(user => { %>` (an OUTPUT tag around a statement
    that opens a brace, so EJS emits `__append(users.forEach(user => {)`) and
    another had a sentence of the model's own prose welded into a call's
    argument list. Both files were structurally perfect markup, both passed
    every check that existed, and both threw at render time — which on this
    project's own build meant the home page, `/users` and `/items` were 500s.

    The translation is EJS's own, kept deliberately literal:
      * `<% code %>`             → `code`            (scriptlet)
      * `<%= x %>` / `<%- x %>`  → `__append(x);`    (output)
      * `<%# … %>`               → nothing           (comment)
      * `<%% `                   → nothing           (the literal-`<%` escape)
    Markup between tags becomes nothing at all: it is a string in the real
    compilation and cannot affect whether the code parses.

    The result is wrapped in an `async function` because a view body may
    legitimately `await` and may `return` early, and both are syntax errors at
    the top level of a script. Undefined locals (`users`, `ui`) are irrelevant —
    `node --check` parses, it never runs.
    """
    parts: list[str] = ["async function __ejs(__append, locals) {\n"]
    i, n = 0, len(text or "")
    while i < n:
        if text.startswith("<%%", i):
            i += 3
            continue
        if not text.startswith("<%", i):
            i += 1
            continue
        end = text.find("%>", i + 2)
        if end == -1:
            break  # unterminated — strip_ejs owns that error, and reports it
        body = text[i + 2 : end]
        i = end + 2
        # `<%_` and `_%>`/`-%>` are whitespace-slurp markers, not code.
        if body.startswith("_"):
            body = body[1:]
        if body.endswith(("_", "-")):
            body = body[:-1]
        if not body.strip():
            continue
        marker, rest = body[0], body[1:]
        if marker == "#":
            continue  # a comment compiles to nothing
        if marker in ("=", "-"):
            parts.append(f"__append({rest}\n);\n")
            continue
        parts.append(body + "\n")
    parts.append("\n}\n")
    return "".join(parts)


def _check_ejs_text(text: str, name: str) -> tuple[bool, str]:
    """Structural validation for an EJS view: delimiters, then tag balance.

    The `.html` checks with the JavaScript taken out first. Deliberately does
    NOT require a full document: every view except `layout.ejs` is a fragment,
    and `_html_surrounding_prose` only fires when there is a document to be
    outside of, so both shapes pass for the right reason.

    The JavaScript itself is checked separately and needs `node` — see
    `ejs_script` and `check_file`. This half stays tooling-free so a W9
    candidate can still be scored before it is written.
    """
    html, error = strip_ejs(text)
    if error:
        return False, f"{name}: {error}"

    parser = _TagBalanceParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as e:
        return False, f"EJS parse error in {name}: {e}"

    errors = list(parser.errors)
    errors.extend(f"unclosed <{tag}>" for tag in reversed(parser.stack))
    if errors:
        return False, f"{name}: " + "; ".join(errors[:5])

    # A view with no EJS tag AND no markup at all is not a view. There is
    # deliberately no general "this looks like prose" guard here — a hero
    # paragraph is a legitimate fragment, unlike in a `.js` file where prose
    # means the wrong kind of content entirely — but a file containing neither a
    # tag nor an element renders literally nothing, so it can only be the model
    # answering in the file instead of writing one. Measured: "fix the files
    # inside users.ejs" produced a `users.ejs` whose entire content was "To
    # address the request … I need more information about the specific issues",
    # and it passed as a valid view.
    if text.strip() and "<%" not in text and not parser.saw_tag:
        head = " ".join(text.split())[:80]
        return False, (
            f"{name}: no markup and no EJS tags — this is prose, not a view: "
            f"{head!r}. Output only the contents of the file."
        )
    return True, ""


def _check_ejs_javascript(text: str, name: str) -> tuple[bool, str]:
    """Syntax-check the JavaScript inside an EJS view with `node --check`.

    The half of `check_file` that needs a binary, kept beside the `.js` and
    `.ts` cases and skipped the same way when `node` is missing. The script is
    written to a temp file rather than the view itself, so the error is about
    the code and the view on disk is never touched.
    """
    if shutil.which("node") is None:
        return True, ""
    script = ejs_script(text)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(script)
            tmp = Path(handle.name)
        ok, error = _check_with_command(tmp, "node", ["--check"])
    except Exception:
        return True, ""  # the checker itself broke → never block the write
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
    if ok:
        return True, ""
    # The temp path is noise, and the line numbers are the script's rather than
    # the view's — say which file is wrong and quote the parser.
    detail = " ".join(str(error).split())
    return False, (
        f"{name}: the JavaScript inside the EJS tags does not parse — {detail[:400]}"
    )


def _check_html_text(text: str, name: str) -> tuple[bool, str]:
    """Structural validation: no prose around the document, at least one tag,
    and balanced open/close tags — all without external tooling."""
    surrounding = _html_surrounding_prose(text)
    if surrounding:
        return False, f"{name}: {surrounding}"

    parser = _TagBalanceParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as e:
        return False, f"HTML parse error in {name}: {e}"

    visible = _HTML_COMMENT_RE.sub("", text).strip()
    if visible and not parser.saw_tag:
        return False, (
            f"{name}: no HTML tags found — the content looks like prose, not HTML."
        )

    errors = list(parser.errors)
    errors.extend(f"unclosed <{tag}>" for tag in parser.stack)
    if errors:
        return False, f"{name}: " + "; ".join(errors[:10])
    return True, ""
