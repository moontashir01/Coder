"""Runtime defects in generated JavaScript that `node --check` cannot see.

The JavaScript half of `pyimports.py`, and it exists for exactly the reason that
module does: a syntax check answers "does this parse", and the way a generated
app really dies is `ReferenceError: bcrypt is not defined` — a line that parses
perfectly and throws the moment it runs.

Measured on a live Node build (OpenBazaar): `server.js` shipped a `/api/login`
route calling `bcrypt.compareSync(...)` and `req.session.userId = ...` with
neither `bcrypt` required nor any session middleware installed, and stored the
raw form field into `password_hash` while the project's own generated
`passwords.js` sat unused. Every check in the pipeline passed it, because every
check that could have caught it was written against Python: `add_missing_imports`
parses with `ast`, `_repair_missing_imports` looks for `db.py`/`models.py`, and
`plaintext_password_writes` matches `request.form[...]` and recommends
`werkzeug.security`.

Two checks, mirroring the Python ones including which of them is allowed to
write:

* ``add_missing_requires`` — repairs, allowlist-only. A name is only bound when
  we can prove where it comes from: a Node BUILTIN module, or a sibling file in
  this project that really exports it. Everything else is REPORTED. That rule is
  sharper here than on the Python side: `require("bcrypt")` for a package that
  is not in `package.json` turns a 500 on one route into a crash at boot, so
  guessing is strictly worse than saying so.
* ``plaintext_password_writes`` — reports, never repairs, and is silent when the
  module hashes anywhere.

Parsing is tree-sitter, the same parser `symbols.py` and `chunker.py` already
pin. A file it cannot parse yields nothing rather than something wrong.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

try:  # pragma: no cover - exercised by the presence/absence of the dep
    from tree_sitter_languages import get_parser
except Exception:  # pragma: no cover
    get_parser = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Node builtins worth binding automatically. Deliberately short: these are the
# ones a generated Express app actually reaches for, they need no install, and
# `require("path")` can never fail at boot.
_NODE_BUILTINS: dict[str, str] = {
    "path": "path",
    "fs": "fs",
    "crypto": "crypto",
    "os": "os",
    "url": "url",
    "util": "util",
}

# Identifiers that are always in scope in a CommonJS module under Node. An
# undefined-name check without this list reports the entire language.
_GLOBALS = frozenset(
    {
        # CommonJS + Node
        "require",
        "module",
        "exports",
        "__dirname",
        "__filename",
        "process",
        "Buffer",
        "global",
        "globalThis",
        "console",
        "URL",
        "URLSearchParams",
        "TextEncoder",
        "TextDecoder",
        "AbortController",
        "AbortSignal",
        "setTimeout",
        "clearTimeout",
        "setInterval",
        "clearInterval",
        "setImmediate",
        "clearImmediate",
        "queueMicrotask",
        "structuredClone",
        "fetch",
        "Headers",
        "Request",
        "Response",
        "FormData",
        "Blob",
        "performance",
        "crypto",
        # The BROWSER. A generated page's scripts run in a document, and every
        # one of these was reported as an undefined name until now: a six-file
        # static build came back with "may not meet: uses undefined name(s) at
        # runtime — document, window, requestAnimationFrame" on four of its six
        # files, all of them correct code.
        #
        # The trade is deliberate and it is the one this codebase always makes:
        # a Node module that really does reference `document` is now unreported,
        # but that mistake fails loudly on the first request with a plain
        # ReferenceError, while the false alarm fired on EVERY browser file and
        # a false failure is worse than no check. The repair half is unaffected
        # either way — `add_missing_requires` binds only Node builtins and
        # sibling modules, and would never have bound `document` regardless.
        "window",
        "document",
        "navigator",
        "location",
        "history",
        "screen",
        "localStorage",
        "sessionStorage",
        "alert",
        "confirm",
        "prompt",
        "requestAnimationFrame",
        "cancelAnimationFrame",
        "requestIdleCallback",
        "cancelIdleCallback",
        "getComputedStyle",
        "matchMedia",
        "scrollTo",
        "scrollBy",
        "open",
        "close",
        "atob",
        "btoa",
        "XMLHttpRequest",
        "WebSocket",
        "EventSource",
        "Worker",
        "SharedWorker",
        "MessageChannel",
        "BroadcastChannel",
        "FileReader",
        "File",
        "DOMParser",
        "XMLSerializer",
        "Image",
        "Audio",
        "AudioContext",
        "webkitAudioContext",
        "OffscreenCanvas",
        "Path2D",
        "ImageData",
        "MutationObserver",
        "ResizeObserver",
        "IntersectionObserver",
        "CustomEvent",
        "Event",
        "KeyboardEvent",
        "MouseEvent",
        "PointerEvent",
        "TouchEvent",
        "WheelEvent",
        "Element",
        "HTMLElement",
        "Node",
        "NodeList",
        "Range",
        "Selection",
        "CSS",
        # Standard library objects
        "Object",
        "Array",
        "String",
        "Number",
        "Boolean",
        "Symbol",
        "BigInt",
        "Math",
        "JSON",
        "Date",
        "RegExp",
        "Error",
        "TypeError",
        "RangeError",
        "SyntaxError",
        "ReferenceError",
        "EvalError",
        "URIError",
        "AggregateError",
        "Promise",
        "Map",
        "Set",
        "WeakMap",
        "WeakSet",
        "Proxy",
        "Reflect",
        "Intl",
        "ArrayBuffer",
        "SharedArrayBuffer",
        "DataView",
        "Int8Array",
        "Uint8Array",
        "Uint8ClampedArray",
        "Int16Array",
        "Uint16Array",
        "Int32Array",
        "Uint32Array",
        "Float32Array",
        "Float64Array",
        "BigInt64Array",
        "BigUint64Array",
        "parseInt",
        "parseFloat",
        "isNaN",
        "isFinite",
        "encodeURI",
        "encodeURIComponent",
        "decodeURI",
        "decodeURIComponent",
        "escape",
        "unescape",
        "undefined",
        "NaN",
        "Infinity",
        "eval",
        "arguments",
        "this",
        "super",
    }
)

# Constructs that BIND, and the field holding the pattern they bind. `None`
# means the whole node is the pattern (a parameter list binds every name in it).
# Names are collected FLAT across the module — deliberately over-approximating
# scope, exactly as `pyimports.undefined_names` does, so the error direction is
# always "report nothing" rather than "report a name that is really in scope two
# blocks up".
_BINDERS: dict[str, str | None] = {
    "variable_declarator": "name",
    "function_declaration": "name",
    "generator_function_declaration": "name",
    "function_expression": "name",
    "class_declaration": "name",
    "formal_parameters": None,
    "catch_clause": "parameter",
    "for_in_statement": "left",
    "import_clause": None,
    "labeled_statement": "label",
}

# Node types that name a variable. `property_identifier` (`a.b`, `{b: 1}`,
# `class X { b() {} }`) is deliberately absent: it can never be undefined.
_NAME_NODES = frozenset({"identifier", "shorthand_property_identifier_pattern"})

_REQUIRE_RE = re.compile(r"""require\s*\(\s*["'][^"']+["']\s*\)""")
_USE_STRICT_RE = re.compile(r"""^\s*["']use strict["'];?\s*$""", re.MULTILINE)
_SHEBANG_RE = re.compile(r"^#!.*$", re.MULTILINE)

# Names that a generated Express app uses but that come from MIDDLEWARE rather
# than a binding — `req.session` exists only if `express-session` is mounted.
# Reported, never repaired: mounting a session store is a design decision with a
# secret and a backing store behind it, not an import.
_MIDDLEWARE_HINTS: tuple[tuple[re.Pattern, str, str], ...] = (
    (
        re.compile(r"\breq(?:uest)?\.session\b"),
        "req.session",
        "no session middleware is installed — add `express-session` and "
        "`app.use(session({...}))`, or store the login some other way",
    ),
    (
        re.compile(r"\breq(?:uest)?\.flash\s*\("),
        "req.flash",
        "no flash middleware is installed — add `connect-flash`",
    ),
    (
        re.compile(r"\breq(?:uest)?\.files?\b(?!\s*=)"),
        "req.file",
        "no upload middleware is installed — add `multer` and mount it on the " "route",
    ),
)


def _parse(source: str):
    """Tree-sitter root node for a JS source, or None when unavailable."""
    if get_parser is None:
        return None
    try:
        parser = get_parser("javascript")
        return parser.parse((source or "").encode("utf-8")).root_node
    except Exception:
        logger.debug("javascript parse failed", exc_info=True)
        return None


def _text(node, source_bytes: bytes) -> str:
    return source_bytes[node.start_byte : node.end_byte].decode(
        "utf-8", errors="replace"
    )


def _binding_patterns(root):
    """Yield the pattern subtree of every binding construct in the module.

    Identity (`node is other`) does NOT work on tree-sitter nodes — each
    attribute access builds a fresh wrapper — so a binding is found by walking
    the pattern subtree rather than by asking whether an identifier happens to
    be the `name` field of its parent. Getting that wrong is not a subtle
    mis-report: it made `const path = require("path")` read as an undefined
    `path`, i.e. every declaration in the file reported as a defect.
    """
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        field = _BINDERS.get(node.type)
        if node.type not in _BINDERS:
            # `(y) => y` has a `formal_parameters`; `y => y` hangs the single
            # parameter straight off the arrow function.
            if node.type == "arrow_function":
                single = node.child_by_field_name("parameter")
                if single is not None:
                    yield single
            continue
        if field is None:
            yield node
            continue
        pattern = node.child_by_field_name(field)
        if pattern is not None:
            yield pattern


def _names_in(node, data: bytes) -> set[str]:
    """Every variable name a pattern subtree binds.

    Over-approximates on purpose: in `{ a = fallback }` the default value is a
    read, not a binding, and it is counted as bound anyway. The cost is one
    missed report; the alternative error direction is a false one.
    """
    names: set[str] = set()
    stack = [node]
    while stack:
        current = stack.pop()
        stack.extend(current.children)
        if current.type in _NAME_NODES:
            names.add(_text(current, data))
    return names


def undefined_names(source: str) -> list[str]:
    """Identifiers the module reads but never binds anywhere in it.

    Flat scope on purpose (see `_BINDERS`). Returns sorted unique names with the
    language's own globals removed. An unparseable file yields ``[]``.
    """
    root = _parse(source)
    if root is None:
        return []
    data = (source or "").encode("utf-8")

    bound: set[str] = set()
    bound_spans: set[tuple[int, int]] = set()
    for pattern in _binding_patterns(root):
        bound |= _names_in(pattern, data)
        bound_spans.add((pattern.start_byte, pattern.end_byte))

    used: set[str] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if node.type != "identifier":
            continue  # `a.b` / `{b: 1}` are property_identifier — never a read
        parent = node.parent
        if parent is not None and parent.type == "member_expression":
            prop = parent.child_by_field_name("property")
            if prop is not None and (prop.start_byte, prop.end_byte) == (
                node.start_byte,
                node.end_byte,
            ):
                continue
        if any(
            start <= node.start_byte and node.end_byte <= end
            for start, end in bound_spans
        ):
            continue  # it is the declaration itself, not a use of it
        used.add(_text(node, data))

    return sorted(n for n in used if n not in bound and n not in _GLOBALS)


def _sibling_exports(root: Path, module: str) -> set[str]:
    """Names `<root>/<module>.js` puts on `module.exports`.

    Deliberately textual and deliberately shallow: it reads the object literal
    of a `module.exports = { … }` and the `module.exports.x =` form. Anything it
    cannot read yields an empty set, which makes the name unresolvable and
    therefore REPORTED — the safe direction.
    """
    path = root / f"{module}.js"
    try:
        if not path.is_file():
            return set()
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()

    names: set[str] = set()
    for match in re.finditer(r"module\.exports\s*=\s*\{(.*?)\}", text, re.DOTALL):
        for part in match.group(1).split(","):
            key = part.split(":", 1)[0].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", key):
                names.add(key)
    for match in re.finditer(r"(?:module\.)?exports\.([A-Za-z_$][\w$]*)\s*=", text):
        names.add(match.group(1))
    return names


def _require_for(
    name: str, root: Path | None, local_modules: tuple[str, ...]
) -> str | None:
    """The `const … = require(…)` line that binds `name`, or None to report it.

    Allowlist only. A Node builtin, or a sibling module in this project that
    really exports the name — nothing else. A package name is never guessed:
    requiring something absent from `node_modules` turns one broken route into
    an app that will not boot.
    """
    if name in _NODE_BUILTINS:
        return f'const {name} = require("{_NODE_BUILTINS[name]}");'
    if root is None:
        return None
    for module in local_modules:
        if name == module:
            return f'const {name} = require("./{module}");'
        if name in _sibling_exports(root, module):
            return f'const {{ {name} }} = require("./{module}");'
    return None


def add_missing_requires(
    source: str,
    root: Path | None = None,
    local_modules: tuple[str, ...] = ("db", "models", "ui", "passwords"),
) -> tuple[str, list[str], list[str]]:
    """``(new_source, added, still_missing)`` for a generated JS module.

    The JavaScript twin of `pyimports.add_missing_imports`, and it keeps that
    function's two guarantees: a name is only bound from the allowlist, and the
    result is re-checked before it is returned, so a file this pass cannot fix
    is returned byte-for-byte rather than half-edited.
    """
    text = source or ""
    missing = undefined_names(text)
    if not missing:
        return source, [], []

    lines: list[str] = []
    unresolved: list[str] = []
    for name in missing:
        stmt = _require_for(name, root, local_modules)
        if stmt is None:
            unresolved.append(name)
        elif stmt not in lines:
            lines.append(stmt)

    if not lines:
        return source, [], unresolved

    at = _insertion_point(text)
    body = text.splitlines()
    body[at:at] = lines
    new_text = "\n".join(body)
    if text.endswith("\n") and not new_text.endswith("\n"):
        new_text += "\n"

    # Never ship a file this pass broke, and never claim a fix that did not
    # take: if the names are still undefined afterwards, keep the original.
    if undefined_names(new_text) != unresolved:
        remaining = set(undefined_names(new_text))
        if remaining - set(unresolved):
            return source, [], unresolved
    return new_text, lines, unresolved


def _insertion_point(text: str) -> int:
    """Line index for new requires: after the shebang, `"use strict"` and any
    existing require block, before the first line of real code."""
    lines = text.splitlines()
    at = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "/*", "*", "*/")):
            continue
        if _SHEBANG_RE.match(stripped) or _USE_STRICT_RE.match(line):
            at = index + 1
            continue
        if _REQUIRE_RE.search(stripped):
            at = index + 1
            continue
        break
    return at


# A raw request password on its way into storage, in Express terms. The Python
# twin matches `request.form[...]`; here the value arrives as `req.body.password`
# or is destructured out of it.
_HASH_CALL_RE = re.compile(
    r"(hashPassword|verifyPassword|bcrypt|scrypt|argon|pbkdf2|createHash|"
    r"timingSafeEqual)",
    re.IGNORECASE,
)
_ASSIGN_SECRET_JS_RE = re.compile(
    r"""(?P<col>\w*(?:password|passwd|secret|token)\w*)\s*[:=]\s*"""
    r"""req(?:uest)?\.(?:body|query|params)"""
    r"""(?:\.\w+|\[\s*["'][^"']+["']\s*\])""",
    re.IGNORECASE,
)
# `const { password_hash } = req.body` — the destructured form, which is what
# the generated Express routes actually write.
_DESTRUCTURE_SECRET_RE = re.compile(
    r"""(?:const|let|var)\s*\{(?P<names>[^}]*)\}\s*=\s*req(?:uest)?\.body""",
    re.IGNORECASE,
)
_SECRET_NAME_RE = re.compile(r"\w*(?:password|passwd|secret)\w*", re.IGNORECASE)


def plaintext_password_writes(
    source: str, undefined: set[str] | frozenset[str] | tuple[str, ...] = ()
) -> list[str]:
    """Lines that put a raw request password somewhere it will be stored.

    `crud.plaintext_password_writes` for JavaScript, with the same rule: silent
    when the module hashes ANYWHERE, so read-then-hash is correctly left alone.
    Reports, never repairs — the fix is to call the project's own
    `passwords.hashPassword`, and choosing where in a handler that goes is the
    build's job, not a regex's.

    ``undefined`` (from `undefined_names`) is what keeps that suppression
    honest on this stack. Measured: a generated `server.js` stored the raw form
    field AND mentioned `bcrypt.compareSync` in a different route — with
    `bcrypt` never required. Text-matching alone read that as "this module
    hashes" and went quiet about the one thing it exists to catch. A hash call
    on a name that is not bound is not a hash call.
    """
    text = source or ""
    hashed = [
        m for m in _HASH_CALL_RE.finditer(text) if m.group(0) not in set(undefined)
    ]
    if hashed:
        return []

    hits: list[str] = []
    for match in _ASSIGN_SECRET_JS_RE.finditer(text):
        hits.append(" ".join(match.group(0).split()))
    for match in _DESTRUCTURE_SECRET_RE.finditer(text):
        for raw in match.group("names").split(","):
            name = raw.split(":")[0].strip()
            if name and _SECRET_NAME_RE.fullmatch(name):
                hits.append(f"const {{ …, {name} }} = req.body")
    return hits


def middleware_gaps(source: str) -> list[str]:
    """Express features the code uses that nothing in this file installs.

    `req.session` is the one that matters: the generated login route assigns to
    it, `node --check` is happy, `undefined_names` cannot see it (it is a
    property of a parameter), and the route throws
    `TypeError: Cannot set properties of undefined` on the first successful
    login. Reported only — mounting a session store is a design decision.
    """
    text = source or ""
    gaps: list[str] = []
    for pattern, label, advice in _MIDDLEWARE_HINTS:
        if not pattern.search(text):
            continue
        # `app.use(session(...))` / `require("express-session")` in this file
        # means it IS installed.
        installed = re.search(
            rf"""(session|flash|multer)\s*\(|require\s*\(\s*["'][^"']*"""
            rf"""(session|flash|multer)[^"']*["']""",
            text,
            re.IGNORECASE,
        )
        if installed:
            continue
        gaps.append(f"{label}: {advice}")
    return gaps
