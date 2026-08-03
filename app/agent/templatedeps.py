"""The dependency graph, extended past `app.py` into the templates (Phase W8).

`symbols.py` resolves imports for Python only, so the project graph stopped at
the file that renders the page. Everything downstream then had to guess which
template a change touches — and the guess it used, `Page.reads`, is inferred
from blueprint prose and is *routinely empty on the very listing page that
matters* (CLAUDE.md records the measurement; `functional_probe` step 3 was
rewritten for the same reason).

The edges a Jinja project really has are all readable off the files:

  * template → the layout it `{% extends %}`, and anything it `{% include %}`s
  * route → template, from `render_template(...)` (`routes_from_source` already
    reads these)
  * template → the endpoints its `url_for(...)` calls name, and separately the
    endpoints its **forms** post to
  * template → the static assets it references
  * template → the *entities* it displays, which is the one that replaces
    `Page.reads`

**Additive and best-effort, exactly like `reconcile_with_disk`.** A template
this parser cannot read yields **no edges, never a wrong edge** — every consumer
unions what it finds with what it already knew and nothing is ever removed on
the strength of an absence.

**The entity hint is the part that had to be careful.** The obvious version —
"does the template mention the word `products`?" — turns `base.html` into a
reader of every entity in the project, because its nav says `Products`, and an
amendment would then edit the site layout to "show price for each product".
So identifiers are taken only from **Jinja expressions**, with string literals
stripped first: `{% for p in products %}` and `{{ product.title }}` count,
`<a href="/products">Products</a>` does not, and `{{ url_for('products') }}`
contributes only `url_for` because `'products'` is a string. Layout templates
are excluded outright.

Pure and offline (design rule 2): `parse_template` takes text, `build_graph`
takes a directory. No LLM, no network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from app.agent.projectspec import routes_from_source

# Jinja's own vocabulary plus the globals Flask puts in every template context.
# An identifier from this set says nothing about what the page displays.
_JINJA_WORDS = frozenset(
    {
        "and",
        "as",
        "block",
        "by",
        "call",
        "do",
        "elif",
        "else",
        "endblock",
        "endcall",
        "endfilter",
        "endfor",
        "endif",
        "endmacro",
        "endset",
        "endwith",
        "extends",
        "false",
        "filter",
        "for",
        "from",
        "if",
        "import",
        "in",
        "include",
        "is",
        "loop",
        "macro",
        "none",
        "not",
        "or",
        "recursive",
        "set",
        "super",
        "true",
        "with",
        "without",
        "config",
        "csrf_token",
        "dict",
        "g",
        "get_flashed_messages",
        "namespace",
        "range",
        "request",
        "self",
        "session",
        "ui",
        "url_for",
        "varargs",
    }
)

# Jinja filters and tests are identifiers too, and none of them names an entity.
_JINJA_FILTERS = frozenset(
    {
        "abs",
        "attr",
        "batch",
        "capitalize",
        "default",
        "escape",
        "first",
        "float",
        "format",
        "groupby",
        "int",
        "join",
        "last",
        "length",
        "list",
        "lower",
        "map",
        "max",
        "min",
        "reject",
        "replace",
        "reverse",
        "round",
        "safe",
        "select",
        "slice",
        "sort",
        "string",
        "striptags",
        "sum",
        "title",
        "tojson",
        "trim",
        "truncate",
        "unique",
        "upper",
        "urlencode",
        "wordcount",
    }
)

_EXPRESSION_RE = re.compile(r"{{(?P<body>.*?)}}|{%(?P<tag>.*?)%}", re.DOTALL)
_STRING_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_EXTENDS_RE = re.compile(r"{%-?\s*extends\s+['\"](?P<name>[^'\"]+)['\"]")
_INCLUDE_RE = re.compile(
    r"{%-?\s*(?:include|import|from)\s+['\"](?P<name>[^'\"]+)['\"]"
)
_BLOCK_RE = re.compile(r"{%-?\s*block\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)")
_URL_FOR_RE = re.compile(r"url_for\(\s*['\"](?P<name>[^'\"]+)['\"]")
_STATIC_RE = re.compile(
    r"url_for\(\s*['\"]static['\"]\s*,\s*filename\s*=\s*['\"](?P<file>[^'\"]+)['\"]"
)
_FORM_RE = re.compile(r"<form\b[^>]*>", re.IGNORECASE)
# Backreferenced quote, exactly like `verify._FORM_ACTION_RE`: the value is
# `action="{{ url_for('add_product') }}"`, so a `[^'"]*` body stops dead at the
# apostrophe inside and the endpoint is never seen.
_ACTION_RE = re.compile(
    r"\baction\s*=\s*(?P<q>['\"])(?P<val>.*?)(?P=q)", re.IGNORECASE | re.DOTALL
)
_SEGMENT_RE = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")


def _name_key(name: str) -> str:
    """Collapse a name for matching, exactly as `references._name_key` does.

    Punctuation dropped, one trailing plural collapsed — so the entity
    `product`, the table `products` and the loop variable `products` all meet,
    while `product_list` stays its own thing.
    """
    key = re.sub(r"[^a-z0-9]+", "", (name or "").lower())
    if len(key) > 3 and key.endswith("s"):
        key = key[:-1]
    return key


def _keys_of(identifier: str) -> set[str]:
    """Every entity name an identifier could be naming.

    The whole word AND its segments, because the signal arrives in both shapes:
    a template writes `{% for product in products %}`, while the view behind it
    writes `models.get_all_products()`. Keying only the whole identifier finds
    the first and misses the second — which is the page that matters.
    """
    keys = {_name_key(identifier)}
    for part in _SEGMENT_RE.findall(identifier or ""):
        keys.add(_name_key(part))
    keys.discard("")
    return keys


@dataclass(frozen=True)
class TemplateInfo:
    """Every edge one template declares. Facts only — no judgements."""

    path: str = ""
    extends: str = ""
    includes: tuple[str, ...] = ()
    blocks: tuple[str, ...] = ()
    endpoints: tuple[str, ...] = ()  # every url_for target
    form_endpoints: tuple[str, ...] = ()  # only those a <form> posts to
    assets: tuple[str, ...] = ()  # static/… files it references
    identifiers: tuple[str, ...] = ()  # entity hints, filtered (see module doc)

    def mentions(self, *names: str) -> bool:
        """Does this template display something called any of ``names``?"""
        keys = {_name_key(n) for n in names if n}
        keys.discard("")
        return any(_keys_of(i) & keys for i in self.identifiers)


def parse_template(text: str, path: str = "") -> TemplateInfo:
    """Read one template's edges. Never raises; unreadable input yields nothing."""
    source = text or ""
    extends_match = _EXTENDS_RE.search(source)
    includes = tuple(
        dict.fromkeys(m.group("name") for m in _INCLUDE_RE.finditer(source))
    )
    blocks = tuple(dict.fromkeys(m.group("name") for m in _BLOCK_RE.finditer(source)))
    endpoints = tuple(
        dict.fromkeys(
            m.group("name") for m in _URL_FOR_RE.finditer(source) if m.group("name")
        )
    )
    assets = tuple(dict.fromkeys(m.group("file") for m in _STATIC_RE.finditer(source)))

    form_endpoints: list[str] = []
    for form in _FORM_RE.finditer(source):
        action = _ACTION_RE.search(form.group(0))
        if not action:
            continue
        target = _URL_FOR_RE.search(action.group("val"))
        if target and target.group("name") not in form_endpoints:
            form_endpoints.append(target.group("name"))

    identifiers: list[str] = []
    for match in _EXPRESSION_RE.finditer(source):
        body = match.group("body") or match.group("tag") or ""
        # Strings first: this is what keeps `url_for('products')` from claiming
        # the layout displays products.
        body = _STRING_RE.sub(" ", body)
        for ident in _IDENT_RE.findall(body):
            low = ident.lower()
            if low in _JINJA_WORDS or low in _JINJA_FILTERS or len(low) < 3:
                continue
            if ident not in identifiers:
                identifiers.append(ident)

    return TemplateInfo(
        path=(path or "").replace("\\", "/"),
        extends=extends_match.group("name") if extends_match else "",
        includes=includes,
        blocks=blocks,
        endpoints=endpoints,
        form_endpoints=tuple(form_endpoints),
        assets=assets,
        identifiers=tuple(identifiers),
    )


# --- EJS (Phase N4) ---------------------------------------------------------
# The same graph shape, a different parser. An EJS view has no `{% extends %}`
# — `express-ejs-layouts` wraps every render — so the layout edge is implicit
# and only `include()` is explicit. Everything else maps one to one: `<%= %>`
# and `<% %>` are the expressions that name entities, `href`/`action` name
# routes by PATH rather than by view name, and `/css/x` is a static asset.
_EJS_EXPRESSION_RE = re.compile(r"<%[-=]?(?!#)(?P<body>.*?)%>", re.DOTALL)
_EJS_INCLUDE_RE = re.compile(r"""\binclude\s*\(\s*(?P<q>["'])(?P<name>[^"']+)(?P=q)""")
_EJS_HREF_RE = re.compile(
    r"""\b(?:href|action)\s*=\s*(?P<q>["'])(?P<val>/[^"']*)(?P=q)""", re.IGNORECASE
)
_EJS_ASSET_RE = re.compile(
    r"""\b(?:href|src)\s*=\s*["'](?P<file>/(?:css|js|uploads|img|images|fonts)/[^"']+)["']""",
    re.IGNORECASE,
)
# JavaScript and EJS scaffolding that names no entity. `ui` is the component
# library, so every page mentions it; treating it as an entity hint would make
# every view a reader of everything.
_JS_WORDS = frozenset("""
    if else for while do return function const let var new typeof instanceof
    true false null undefined this async await try catch finally throw switch
    case break continue in of delete void yield class extends super import
    export default from require module exports length forEach map filter reduce
    join push slice split trim toUpperCase toLowerCase includes indexOf keys
    values entries Object Array String Number Boolean Math JSON Date Promise
    console log locals it item items row rows index idx key val value
    ui esc humanize body title messages projectName notFound render
    """.split())


def parse_ejs_template(text: str, path: str = "") -> TemplateInfo:
    """Read one EJS view's edges. Never raises; unreadable input yields nothing.

    `parse_template`'s rules, restated for EJS. The load-bearing one is
    unchanged: **identifiers come from EJS expressions with the strings stripped
    first.** Without that, `layout.ejs` — whose nav says "Products" and whose
    href is `/products` — becomes a reader of every entity, and an amendment
    rewrites the site layout to "show price for each product".
    """
    source = text or ""
    includes = tuple(
        dict.fromkeys(m.group("name") for m in _EJS_INCLUDE_RE.finditer(source))
    )
    # On a path-routed stack the "endpoint" IS the path, so this is what a link
    # check and `endpoints_used` have to work from.
    endpoints = tuple(
        dict.fromkeys(m.group("val") for m in _EJS_HREF_RE.finditer(source))
    )
    assets = tuple(
        dict.fromkeys(m.group("file") for m in _EJS_ASSET_RE.finditer(source))
    )

    form_endpoints: list[str] = []
    for form in _FORM_RE.finditer(source):
        action = _ACTION_RE.search(form.group(0))
        if action:
            target = (action.group("val") or "").strip()
            if target.startswith("/") and target not in form_endpoints:
                form_endpoints.append(target)

    identifiers: list[str] = []
    for match in _EJS_EXPRESSION_RE.finditer(source):
        body = _STRING_RE.sub(" ", match.group("body") or "")
        for ident in _IDENT_RE.findall(body):
            if ident in _JS_WORDS or len(ident) < 3:
                continue
            if ident not in identifiers:
                identifiers.append(ident)

    return TemplateInfo(
        path=(path or "").replace("\\", "/"),
        extends="",  # implicit: express-ejs-layouts wraps every render
        includes=includes,
        blocks=(),
        endpoints=endpoints,
        form_endpoints=tuple(form_endpoints),
        assets=assets,
        identifiers=tuple(identifiers),
    )


def is_layout(path: str, info: TemplateInfo) -> bool:
    """Is this the shell other pages extend, rather than a page of its own?

    `projectspec.is_layout_template`'s rule — by name, and by shape: it defines
    blocks without extending anything — applied to text already parsed, so it
    costs no disk read and does not depend on the caller's cwd.
    """
    name = (path or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name in ("base.html", "layout.html"):
        return True
    return bool(info.blocks) and not info.extends


_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(?P<name>\w+)\s*\(", re.MULTILINE)
_DECORATOR_RE = re.compile(r"^\s*@", re.MULTILINE)


def view_bodies(app_source: str) -> dict[str, str]:
    """`view name -> its source body`, for reading which entities it loads.

    A template that renders `{{ item.title }}` names no entity, but the view
    behind it calls `models.get_all_products()`. Crude by design — the body runs
    to the next top-level `def`, which is enough to see the calls.

    It stops at the next **decorator** rather than the next `def`, because
    `@app.route("/products")` sits between them: swept into the previous body it
    made `index` look like a page that displays products.
    """
    source = app_source or ""
    matches = list(_DEF_RE.finditer(source))
    out: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        decorator = _DECORATOR_RE.search(source, match.end(), end)
        out[match.group("name")] = source[
            match.end() : decorator.start() if decorator else end
        ]
    return out


@dataclass
class TemplateGraph:
    """The project's Jinja edges. Empty is a valid, meaningful answer."""

    templates: dict[str, TemplateInfo] = field(default_factory=dict)
    routes: tuple[tuple[str, str, str, str], ...] = ()  # method, path, view, template
    views: dict[str, str] = field(default_factory=dict)  # view name -> body source

    # -- structure ------------------------------------------------------

    def parents(self, template: str) -> list[str]:
        """What this template extends/includes, as project-relative paths."""
        info = self.templates.get(_norm(template))
        if info is None:
            return []
        names = ([info.extends] if info.extends else []) + list(info.includes)
        return [p for p in (self._resolve(n) for n in names) if p]

    def children(self, template: str) -> list[str]:
        """Every template that extends or includes this one."""
        target = _norm(template)
        out = []
        for path, info in self.templates.items():
            names = ([info.extends] if info.extends else []) + list(info.includes)
            if any(self._resolve(n) == target for n in names):
                out.append(path)
        return sorted(out)

    def _resolve(self, name: str) -> str:
        """`"base.html"` as written inside a template → its project path."""
        wanted = _norm(name)
        if wanted in self.templates:
            return wanted
        candidate = _norm(f"templates/{name}")
        if candidate in self.templates:
            return candidate
        tail = wanted.rsplit("/", 1)[-1]
        hits = [p for p in self.templates if p.rsplit("/", 1)[-1] == tail]
        return hits[0] if len(hits) == 1 else ""

    # -- routes ---------------------------------------------------------

    def template_for_route(self, route: str) -> str:
        for _method, path, _view, template in self.routes:
            if path == route and template:
                return self._resolve(template)
        return ""

    def route_for_template(self, template: str) -> str:
        target = _norm(template)
        for _method, path, _view, tpl in self.routes:
            if tpl and self._resolve(tpl) == target:
                return path
        return ""

    def view_for_template(self, template: str) -> str:
        target = _norm(template)
        for _method, _path, view, tpl in self.routes:
            if tpl and self._resolve(tpl) == target:
                return view
        return ""

    # -- what a change touches ------------------------------------------

    def templates_reading(self, *names: str) -> list[str]:
        """Templates that display an entity called any of ``names``.

        Two signals, unioned: the template's own Jinja identifiers, and the body
        of the view that renders it (a page whose loop variable is `item` still
        belongs to `product` if its view calls `get_all_products`). Layout
        templates are never included — `base.html` linking to /products is the
        false edge this whole module is careful about.
        """
        keys = {_name_key(n) for n in names if n}
        keys.discard("")
        if not keys:
            return []
        out: list[str] = []
        for path, info in sorted(self.templates.items()):
            if is_layout(path, info):
                continue
            if info.mentions(*names):
                out.append(path)
                continue
            body = self.views.get(self.view_for_template(path), "")
            # Strings stripped first, for the same reason they are in a
            # template: `render_template("products.html")` and the route path
            # `"/products"` are not evidence that this page displays a product —
            # a call to `get_all_products()` is.
            body = _STRING_RE.sub(" ", body)
            if body and any(_keys_of(word) & keys for word in _IDENT_RE.findall(body)):
                out.append(path)
        return out

    def forms_writing(self, *endpoints: str) -> list[str]:
        """Templates whose `<form>` posts to one of these endpoints."""
        wanted = {e for e in endpoints if e}
        if not wanted:
            return []
        return sorted(
            path
            for path, info in self.templates.items()
            if wanted.intersection(info.form_endpoints)
        )

    def endpoints_used(self, template: str) -> list[str]:
        info = self.templates.get(_norm(template))
        return list(info.endpoints) if info else []

    def assets_used(self, template: str) -> list[str]:
        info = self.templates.get(_norm(template))
        return list(info.assets) if info else []


def _norm(path: str) -> str:
    return (path or "").replace("\\", "/").lstrip("./")


def build_graph(
    root: str | Path,
    app_file: str = "app.py",
    *,
    template_dir: str = "templates",
    template_ext: str = ".html",
    parser=None,
    routes_reader=None,
) -> TemplateGraph:
    """Read a project's template edges off disk. Never raises.

    Cheap enough to call per turn — a handful of small text files — and
    deliberately not cached: a cache would go stale exactly on the turn that
    just wrote a template, which is the turn that needs it.

    Every keyword argument defaults to the Flask layout, so the existing callers
    and every existing test are unchanged. Phase N4 passes the Node adapter's
    values (`views/`, `.ejs`, `parse_ejs_template`, the Express route reader) to
    get the same graph off a Node project — the graph SHAPE is identical, only
    the parser differs.
    """
    base = Path(root)
    graph = TemplateGraph()
    parse = parser or parse_template
    read_routes = routes_reader or routes_from_source

    tpl_dir = base / template_dir
    if tpl_dir.is_dir():
        for path in sorted(tpl_dir.rglob(f"*{template_ext}")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue  # unreadable: no edges, never a wrong edge
            rel = path.relative_to(base).as_posix()
            graph.templates[rel] = parse(text, rel)

    try:
        source = (base / app_file).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return graph
    try:
        graph.routes = tuple(read_routes(source))
        # View BODIES are Python-shaped (`def name():` up to the next
        # decorator). There is no equivalent for an anonymous Express arrow
        # function yet, so a Node graph simply has none — an absent edge, never
        # a wrong one.
        graph.views = view_bodies(source) if parser is None else {}
    except Exception:
        pass
    return graph
