"""Names an EJS view uses that its route never passes in.

EJS compiles a template to `with (locals) { … }`, so a bare identifier the route
did not supply is a **ReferenceError at render time** — a 500 on a page the same
build wrote, with every static check green. Nothing here is reachable without
running the app, which is precisely why it went unseen until one was.

The measured case (OpenBazaar PRD, run 3, 2026-08-04): the prompt block lists
both `table(rows, columns, empty)` and `empty_state(message, …)` as `ui`
helpers, and the model conflated them —

    <%- ui.table(orders, ["id", "item_id", …], empty_state) %>

`empty_state` is a helper's NAME, not a local. Every listing page in the build —
five of them, the whole point of the site — answered 500. The route passes only
`{ orders }`.

Two jobs, and the split matters:

  * **Report** any free identifier, because that is always a render-time crash.
  * **Repair** only the one shape that is unambiguous: a bare undefined name in
    a trailing argument of a `ui.*()` call, which becomes `""`. Every `ui`
    helper defaults its optional arguments with `||`, so an empty string
    restores the default the model was reaching for. Anything else is left
    alone — rewriting an expression whose intent is unknown is generation.
"""

from __future__ import annotations

import re

# `<% … %>`, `<%= … %>`, `<%- … %>`. `<%#` is a comment and holds no code.
_CHUNK_RE = re.compile(r"<%(?![#%])[-=_]?(?P<code>.*?)[-_]?%>", re.DOTALL)
_STRING_RE = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`")
_IDENT_RE = re.compile(r"(?<![.\w$])(?P<name>[A-Za-z_$][\w$]*)")
# `const x =`, `let [a, b] =`, `function f(`, `for (const item of …)`
_DECL_RE = re.compile(r"\b(?:const|let|var|function)\s+(?P<name>[A-Za-z_$][\w$]*)")
# Callback parameters, which are bindings the template makes for itself. All
# four spellings, because missing ONE of them is a false alarm on a working
# page: `rows.forEach(row => …)` — a bare parameter with no parentheses — was
# missed at first, and the check's very first live report was five complaints
# about five views that rendered perfectly. A confident wrong complaint is
# worse than a missed one; that is the standard this module holds others to.
_PARAM_RES = (
    re.compile(r"\(\s*(?P<params>[\w$,\s]*?)\)\s*=>"),  # (a, b) =>
    re.compile(r"(?<![.\w$])(?P<params>[A-Za-z_$][\w$]*)\s*=>"),  # a =>
    re.compile(r"\bfunction\s*\w*\s*\(\s*(?P<params>[\w$,\s]*?)\)"),  # function (a)
    re.compile(r"\bcatch\s*\(\s*(?P<params>[\w$,\s]*?)\)"),  # catch (err)
)
_TYPEOF_RE_PLACEHOLDER = None
_TYPEOF_RE = re.compile(r"\btypeof\s+([A-Za-z_$][\w$]*)")

# Bindings every generated view really does have, from the scaffold's server.js
# (`app.locals`) and from EJS itself.
AMBIENT = frozenset({"ui", "projectName", "locals", "settings", "title", "body"})

_JS_GLOBALS = frozenset(
    {
        "Array",
        "Boolean",
        "Date",
        "Error",
        "Infinity",
        "JSON",
        "Math",
        "NaN",
        "Number",
        "Object",
        "Promise",
        "RegExp",
        "String",
        "console",
        "decodeURIComponent",
        "encodeURIComponent",
        "false",
        "isNaN",
        "null",
        "parseFloat",
        "parseInt",
        "process",
        "true",
        "undefined",
    }
)

_JS_KEYWORDS = frozenset(
    {
        "async",
        "await",
        "break",
        "case",
        "catch",
        "class",
        "const",
        "continue",
        "default",
        "delete",
        "do",
        "else",
        "finally",
        "for",
        "function",
        "if",
        "in",
        "instanceof",
        "let",
        "new",
        "of",
        "return",
        "switch",
        "this",
        "throw",
        "try",
        "typeof",
        "var",
        "void",
        "while",
        "yield",
    }
)

# `res.render("orders"` — the view name. What follows it is found by BALANCE,
# not by a regex: the object routinely holds a template literal
# (``res.render("bid_detail", { title: `Bid ${bid.id}`, bid })``) and `${…}`
# carries braces of its own, so `\{([^{}]*)\}` stopped early and reported the
# view's only real local as undefined. That was a false alarm on a page that
# rendered perfectly, which is the one thing this module must not produce.
_RENDER_HEAD_RE = re.compile(
    r"""res\s*\.\s*render\s*\(\s*["'`](?P<view>[\w./-]+)["'`]""",
)
_KEY_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*(?::|,|$)")


def _locals_object(text: str, at: int) -> str:
    """The `{ … }` after a `res.render("view"` that ends at ``at``, or "".

    Brace-matched over a copy whose string literals are blanked, so a brace
    inside a string or a `${}` cannot end the object early. Returns the blanked
    text, which is exactly what the key scan wants — every key is code.
    """
    rest = text[at:]
    comma = re.match(r"\s*,\s*", rest)
    if not comma:
        return ""
    start = at + comma.end()
    if start >= len(text) or text[start] != "{":
        return ""
    blanked = _STRING_RE.sub(lambda m: " " * len(m.group(0)), text[start:])
    depth = 0
    for index, char in enumerate(blanked):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return blanked[1:index]
    return ""


def render_locals(entry_source: str) -> dict[str, set[str]]:
    """`{"orders": {"orders"}}` — per view stem, the names its routes pass.

    Unioned across routes: two routes may render the same view with different
    locals, and a name either of them supplies is one the view may use.
    """
    text = entry_source or ""
    out: dict[str, set[str]] = {}
    for match in _RENDER_HEAD_RE.finditer(text):
        stem = match.group("view").rsplit("/", 1)[-1].rsplit(".", 1)[0]
        names: set[str] = set()
        for part in _locals_object(text, match.end()).split(","):
            key = _KEY_RE.match(part.strip())
            if key:
                names.add(key.group(1))
        out.setdefault(stem, set()).update(names)
    return out


def _code_chunks(text: str) -> list[tuple[int, int, str]]:
    """`(start, end, code)` for each EJS tag body, offsets into ``text``."""
    return [
        (m.start("code"), m.end("code"), m.group("code"))
        for m in _CHUNK_RE.finditer(text or "")
    ]


def _declared(chunks: list[tuple[int, int, str]]) -> set[str]:
    """Names the template binds itself — loop variables, consts, parameters."""
    names: set[str] = set()
    for _s, _e, code in chunks:
        blanked = _STRING_RE.sub(lambda m: " " * len(m.group(0)), code)
        names.update(m.group("name") for m in _DECL_RE.finditer(blanked))
        for pattern in _PARAM_RES:
            for match in pattern.finditer(blanked):
                names.update(
                    part.strip()
                    for part in match.group("params").split(",")
                    if part.strip()
                )
    return names


def _guarded(chunks: list[tuple[int, int, str]]) -> set[str]:
    """Names the view tests with `typeof` before using.

    `typeof x` is the one expression that does NOT throw on an undeclared name,
    and `typeof messages !== "undefined" ? messages : []` is exactly how the
    scaffold's own `layout.ejs` handles an optional local. A name guarded that
    way anywhere in the view is deliberate, not a defect — flagging it would
    make the check's first report a false alarm about Coder's own scaffold.
    """
    names: set[str] = set()
    for _s, _e, code in chunks:
        blanked = _STRING_RE.sub(lambda m: " " * len(m.group(0)), code)
        names.update(_TYPEOF_RE.findall(blanked))
    return names


def free_identifiers(text: str, provided: set[str]) -> list[str]:
    """Names the view evaluates that nothing supplies. Order of appearance."""
    chunks = _code_chunks(text)
    known = (
        set(provided)
        | AMBIENT
        | _JS_GLOBALS
        | _JS_KEYWORDS
        | _declared(chunks)
        | _guarded(chunks)
    )
    out: list[str] = []
    for _s, _e, code in chunks:
        blanked = _STRING_RE.sub(lambda m: " " * len(m.group(0)), code)
        for match in _IDENT_RE.finditer(blanked):
            name = match.group("name")
            # An object KEY (`{ total: n }`) is not a reference.
            after = blanked[match.end() :].lstrip()
            if after.startswith(":"):
                continue
            if name in known or name in out:
                continue
            out.append(name)
    return out


def repair_view_locals(
    text: str, provided: set[str]
) -> tuple[str, list[str], list[str]]:
    """Blank out undefined names used as `ui.*()` arguments; report the rest.

    Returns ``(text, fixes, problems)``.
    """
    source = text or ""
    unknown = free_identifiers(source, provided)
    if not unknown:
        return source, [], []

    fixes: list[str] = []
    out = source
    for name in unknown:
        # Only inside a `ui.helper(...)` argument list, and only as a whole
        # argument — `ui.table(rows, cols, empty_state)`, never a name buried in
        # a larger expression whose meaning this cannot reconstruct.
        pattern = re.compile(
            rf"(?P<head>\bui\s*\.\s*\w+\s*\([^()]*?,\s*)"
            rf"(?P<name>{re.escape(name)})(?P<tail>\s*[,)])"
        )
        replaced, count = pattern.subn(r'\g<head>""\g<tail>', out)
        if count:
            out = replaced
            fixes.append(name)

    problems = [
        f"`{name}` is used by this view but no route passes it — EJS raises "
        "ReferenceError, so the page 500s"
        for name in unknown
        if name not in fixes
    ]
    return out, fixes, problems

# ---------------------------------------------------------------------------
# The other half of the repair: give the ROUTE the name the view needs
# ---------------------------------------------------------------------------

# `items.forEach(...)`, `items.map(...)`, `items.length` — the name is a list,
# so the value that keeps the page rendering is an empty array, not "".
_LISTY = ("forEach", "map", "filter", "length", "slice", "join", "some", "every")


def default_for(name: str, view_text: str) -> str:
    """The JS literal to pass for ``name``, read off how the view USES it.

    A form page comparing `sale_type === "FIXED"` wants a string; a listing page
    calling `items.forEach` wants an array, and `""` there swaps a ReferenceError
    for a TypeError, which is not a repair.
    """
    for method in _LISTY:
        if re.search(rf"\b{re.escape(name)}\s*\.\s*{method}\b", view_text or ""):
            return "[]"
    return '""'


def add_render_locals(
    entry_source: str, stem: str, defaults: dict[str, str]
) -> tuple[str, list[str]]:
    """Pass ``defaults`` to every `res.render("<stem>"…)` that omits them.

    The view is not the thing to rewrite here. A free name in a template is a
    name the ROUTE was supposed to supply, and `repair_view_locals` can only
    blank the unambiguous shape (a bare `ui.*()` argument) — everything else it
    reports, and the page goes on answering 500. Measured on the OpenBazaar
    build: `new_item.ejs` branched on `sale_type` to decide which price fields
    were required, no route passed one, and the "create a listing" page — the
    single most important page in a marketplace — was a 500 from the moment it
    was written.

    Deliberately additive and deterministic: a name the route ALREADY passes is
    never touched, the value is a literal chosen from how the view uses the
    name, and nothing else in the file moves. It cannot make a working page
    wrong — at worst it renders a field the user must fill in.
    """
    text = entry_source or ""
    if not defaults:
        return text, []
    fixes: list[str] = []
    # Right to left: every edit changes the offsets of everything after it.
    for match in reversed(list(_RENDER_HEAD_RE.finditer(text))):
        if match.group("view").rsplit("/", 1)[-1].rsplit(".", 1)[0] != stem:
            continue
        rest = text[match.end() :]
        comma = re.match(r"\s*,\s*\{", rest)
        pairs = ", ".join(f"{n}: {v}" for n, v in sorted(defaults.items()))
        if comma:
            at = match.end() + comma.end()  # just past the `{`
            existing = _locals_object(text, match.end())
            keys = {
                key.group(1)
                for part in existing.split(",")
                if (key := _KEY_RE.match(part.strip()))
            }
            missing = {n: v for n, v in defaults.items() if n not in keys}
            if not missing:
                continue
            pairs = ", ".join(f"{n}: {v}" for n, v in sorted(missing.items()))
            text = text[:at] + " " + pairs + "," + text[at:]
            fixes += sorted(missing)
            continue
        closing = re.match(r"\s*\)", rest)
        if closing:
            at = match.end()
            text = text[:at] + ", { " + pairs + " }" + text[at:]
            fixes += sorted(defaults)
    return text, sorted(set(fixes))
