"""Deterministic project skeleton — copy the boilerplate, generate only the domain.

The highest-leverage idea in `docs/fullstack-web-plan.md`: today the LLM writes
every byte of every file, and most of those bytes are identical in every Flask
app ever written — `Flask(__name__)`, the sqlite `get_db()` helper with its
`row_factory`, the `if __name__ == "__main__"` block, `.gitignore`. Every one of
them is a chance for a 7B model to hallucinate, and it *does*: the Phase 0
baseline (`docs/phase0-baseline.md`) has a build whose `routes.py` used
`@app.route`, `sqlite3` and `DATABASE` without importing any of them. Pure
boilerplate, zero domain logic, dead on startup.

So stop generating it. This module copies a real, runnable Flask skeleton from
`settings.scaffolds_dir` before any generation runs. The effect is that the app
**starts before the model has written a line**, and the failures that remain are
in the domain layer, which is where repair actually works.

Pure and offline: no LLM call, no network, `shutil`/`Path` only — so it unit
tests fully (design rule 2, "pure modules, LLM calls in core.py").

Two conventions worth knowing before editing the template tree:

  * **Dotfiles are stored without their dot.** `gitignore` becomes `.gitignore`
    and `static/uploads/gitkeep` becomes `.gitkeep` at copy time (`_DOTFILES`).
    setuptools' `resources/**/*` package-data glob does not reliably capture
    hidden files, so a literal `.gitignore` in the tree can silently fail to
    ship in a wheel/pipx install — the file would simply be missing, with no
    error. Renaming at copy time keeps packaging honest.
  * **Placeholders are exact literals**, `{{PROJECT_NAME}}` and `{{SECRET_KEY}}`,
    substituted by plain string replacement — NOT by a template engine. That
    matters because the templates are Jinja2: `{{ url_for('static', ...) }}`
    must survive untouched into the generated project. Only the two exact
    strings are replaced.
"""

from __future__ import annotations

import logging
import re
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.agent.blueprint import Blueprint
from config.settings import settings

logger = logging.getLogger(__name__)

# Template filename -> name it is written as. See the module docstring.
_DOTFILES = {"gitignore": ".gitignore", "gitkeep": ".gitkeep"}

# Files copied byte-for-byte; everything else is read as UTF-8 text and has its
# placeholders substituted. Nothing binary ships today, but an image or font
# added to a scaffold later must not be run through str.replace().
_BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2"}

# Pages/templates that make a build a *web* build rather than a script.
_PAGE_SUFFIXES = (".html", ".htm")
# ...and the same question asked across every stack. `is_web_app` gates the
# scaffold copy, so a Node build whose plan is all `.ejs` views and no declared
# endpoint would otherwise get no skeleton at all — the one thing that has to
# happen before generation. Kept separate from `_PAGE_SUFFIXES` because that
# tuple also drives Jinja block editing, which is `.html` only.
_ANY_PAGE_SUFFIXES = _PAGE_SUFFIXES + (".ejs",)
_ANY_TEMPLATE_DIRS = ("templates/", "views/")

_NAME_CLEAN_RE = re.compile(r"[^A-Za-z0-9]+")


def scaffold_dir(key: str) -> Path:
    """Where one stack's template tree lives (`scaffolds/<key>/`).

    Resolved from the package, per the "Bundled resources & packaging" rule in
    CLAUDE.md — never from cwd. `key` is a stack key (`flask`, `node`) and is
    used as a plain path segment, so it must stay an identifier: the callers are
    `app/agent/stacks/`, which owns the (closed) set of keys.
    """
    return Path(settings.scaffolds_dir) / key


def flask_scaffold_dir() -> Path:
    """Where the Flask template tree lives."""
    return scaffold_dir("flask")


def is_web_app(blueprint: Blueprint) -> bool:
    """Does this blueprint describe a web application (so a scaffold applies)?

    A backend stack that actually runs, PLUS evidence of a *web* surface: either
    a declared HTTP endpoint or at least one page/template in the file list. A
    blueprint with a backend but no pages is a script or a library, and dropping
    a Flask skeleton on it would be wrong.
    """
    stack = blueprint.stack
    if stack is None or stack.backend in ("none", "") or stack.language == "none":
        return False
    if blueprint.contract.endpoints:
        return True
    return any(_is_page(pf.filename) for pf in blueprint.files)


def _is_page(filename: str) -> bool:
    name = (filename or "").replace("\\", "/").lower()
    return name.endswith(_ANY_PAGE_SUFFIXES) or name.startswith(_ANY_TEMPLATE_DIRS)


def scaffold_tree_files(src_root: Path) -> set[str]:
    """Relative paths (posix) a scaffold tree writes, dots restored.

    Used to tell generation what already exists, so it imports `db`/`models`
    instead of reinventing them. Reads the template tree rather than hardcoding
    a list, so adding a file to a scaffold cannot desynchronise this.
    """
    src_root = Path(src_root)
    if not src_root.is_dir():
        return set()
    out: set[str] = set()
    for src in src_root.rglob("*"):
        if src.is_file():
            out.add(_destination_name(src.relative_to(src_root)))
    return out


def scaffold_files() -> set[str]:
    """Relative paths (posix) the Flask scaffold writes."""
    return scaffold_tree_files(flask_scaffold_dir())


# Scaffold files that generation must NOT touch: pure boilerplate with no domain
# content, where a rewrite is all risk and no benefit. Everything else the
# scaffold writes stays in the build plan and is *edited* on top of the working
# skeleton — `_file_op_flow` routes an existing file to `_surgical_edit`, so the
# model adds this project's routes to a running app.py rather than writing one
# from scratch. That distinction is the whole point: the domain layer is still
# generated, only the boilerplate stops being.
_FROZEN = frozenset(
    {
        "requirements.txt",
        "Procfile",
        ".gitignore",
        "static/uploads/.gitkeep",
        # Phase W1: the component library and the theme. Both are shipped whole
        # for the same reason db.py is — they contain no domain decisions — and
        # a model handed either one rewrites the classes every page depends on.
        # A restyle edits theme.css's variables, which `write_theme` owns.
        "templates/_macros.html",
        "static/css/theme.css",
    }
)


def frozen_files() -> set[str]:
    """Scaffold files a build plan should drop — see `_FROZEN`."""
    return set(_FROZEN)


def is_frozen(filename: str) -> bool:
    """Is this planned file one the scaffold finished for good?

    Normalizes separators and a leading `./` first, so `.\\Procfile` and
    `./Procfile` are recognised as `Procfile`. Case-sensitive on the basename
    is deliberate — `procfile` is not a Procfile to a Linux deploy host.
    """
    name = (filename or "").replace("\\", "/").strip()
    while name.startswith("./"):
        name = name[2:]
    return name.lstrip("/") in _FROZEN


def _destination_name(rel: Path) -> str:
    """Relative template path -> the path it is written as, posix-style."""
    parts = list(rel.parts)
    parts[-1] = _DOTFILES.get(parts[-1], parts[-1])
    return "/".join(parts)


def project_name(root: Path) -> str:
    """A human-readable project name derived from the directory name."""
    raw = _NAME_CLEAN_RE.sub(" ", Path(root).name).strip()
    return raw.title() if raw else "App"


def project_slug(root: Path, name: str | None = None) -> str:
    """An identifier-safe project name: `my-book-shop`.

    Used where a space is illegal rather than ugly — an npm `name` field and a
    PostgreSQL database name. Lowercase, `[a-z0-9-]` only, never empty and never
    leading with a digit (npm rejects neither, but `CREATE DATABASE 9shop` is a
    syntax error).
    """
    raw = _NAME_CLEAN_RE.sub("-", (name or Path(root).name) or "").strip("-").lower()
    slug = re.sub(r"-{2,}", "-", raw)
    if not slug or slug[0].isdigit():
        slug = f"app-{slug}".rstrip("-")
    return slug[:48]


def database_url(slug: str) -> str:
    """The connection string a generated project ships with, for ``slug``.

    Built from `settings.postgres_server`, so the machine's credentials are
    remembered in ONE place instead of being retyped into every project — the
    scaffold used to hard-code `postgres://postgres:postgres@localhost:5432/`,
    which is nobody's actual server and made every first run fail on
    authentication before it could fail on anything real.

    Only the default. The generated `db.js` still reads `process.env
    .DATABASE_URL` first, so a deployment overrides it without an edit.
    """
    base = (settings.postgres_server or "").strip().rstrip("/")
    if not base:
        base = "postgres://postgres:postgres@localhost:5432"
    return f"{base}/{slug}"


def copy_scaffold(src_root: Path, root: Path, name: str | None = None) -> list[str]:
    """Copy a runnable project skeleton from ``src_root`` into ``root``.

    Returns the relative paths actually written, sorted. **Never overwrites an
    existing file** — so a later turn amending the project is a no-op here, and
    a user's edited `style.css` is never silently reverted. That property is
    what makes it safe to call this on every build turn.

    Stack-agnostic on purpose: the two conventions in the module docstring
    (dotfiles stored without their dot, exact-literal placeholders) are
    packaging and safety rules, not Flask ones, so the Node tree gets them for
    free rather than reimplementing them slightly differently.

    Best-effort per file: one unreadable template does not abort the scaffold.
    """
    root = Path(root)
    src_root = Path(src_root)
    if not src_root.is_dir():
        logger.warning("scaffold tree missing at %s", src_root)
        return []

    values = {
        "{{PROJECT_NAME}}": name or project_name(root),
        # A fresh key per project. Regenerating it on a re-scaffold would log
        # every existing session out, but re-scaffolding never rewrites app.py
        # (see above), so the key each project ships with is stable.
        "{{SECRET_KEY}}": secrets.token_hex(32),
        # An identifier-safe form of the name, for the things that cannot take
        # spaces: an npm package name and a PostgreSQL database name.
        "{{PROJECT_SLUG}}": project_slug(root, name),
        # This machine's PostgreSQL server, remembered once in settings instead
        # of being retyped into every generated project. `DATABASE_URL` in the
        # environment still wins at RUNTIME — the generated `db.js` reads it
        # first — so this is the default, not a lock-in.
        "{{DATABASE_URL}}": database_url(project_slug(root, name)),
    }

    written: list[str] = []
    for src in sorted(src_root.rglob("*")):
        if not src.is_file():
            continue
        rel = _destination_name(src.relative_to(src_root))
        dest = root / rel
        if dest.exists():
            continue  # never clobber
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.suffix.lower() in _BINARY_SUFFIXES:
                shutil.copyfile(src, dest)
            else:
                text = src.read_text(encoding="utf-8")
                for placeholder, value in values.items():
                    text = text.replace(placeholder, value)
                dest.write_text(text, encoding="utf-8", newline="\n")
            written.append(rel)
        except Exception:
            logger.warning("scaffold: could not write %s", rel, exc_info=True)
    return sorted(written)


def scaffold_flask(root: Path, name: str | None = None) -> list[str]:
    """Copy the runnable Flask skeleton into ``root``. See `copy_scaffold`."""
    return copy_scaffold(flask_scaffold_dir(), root, name)


def write_theme(root: Path, css: str, rel: str = "static/css/theme.css") -> bool:
    """Replace the scaffold's default theme with the one the request resolved to.

    The single scaffold file that IS overwritten, deliberately: it holds nothing
    but custom properties, it is written in the same breath as the scaffold copy
    that created it, and the copy would otherwise have just declined to clobber
    its own default. Nothing else rewrites it — an amendment turn never reaches
    this code — so a theme the user hand-edits on turn 2 survives turn 3.

    Best-effort: a theme that cannot be written costs the look, never the build.

    ``rel`` is the stack's theme path — `static/css/theme.css` on Flask,
    `public/css/theme.css` on Node. The FILE is byte-identical on both: it is
    pure custom properties, and forking it would make the two stacks stop
    looking like one product.
    """
    if not (css or "").strip():
        return False
    dest = Path(root) / Path(rel)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(css, encoding="utf-8", newline="\n")
        return True
    except Exception:
        logger.warning("scaffold: could not write theme.css", exc_info=True)
        return False


def ui_context() -> str:
    """The prompt block naming the components generation is expected to use.

    The rule `crud.api_context()` taught, applied to the presentation layer:
    taking work away from the model is only safe if you tell it what replaced
    the work. Shipping a component sheet and a macro library without this block
    does not stop the model writing its own `.product-card` and styling it
    inline — it just means the site now has two design systems.

    Kept in step with the real files by
    `tests/test_scaffold_ui.py::test_ui_context_names_every_macro`, because a
    context block that drifts from the templates is worse than none: it names
    macros that do not exist, and the page 500s on render.
    """
    return (
        "## Page components — ALREADY WRITTEN, use them\n"
        "`static/css/style.css` defines the site's components and "
        "`templates/_macros.html` defines the markup for them. Do NOT write new "
        "CSS, and do NOT hand-write a table, a form field or an empty state.\n"
        '- Start every page with `{% extends "base.html" %}` then '
        '`{% import "_macros.html" as ui %}`.\n'
        "- **Macros** (call as `{{ ui.name(...) }}`):\n"
        "  - `page_header(title, action_url='', action_label='', subtitle='')` — "
        'the page\'s `<h1>` plus an optional button, e.g. "Add product".\n'
        "  - `table(rows, columns, empty='Nothing here yet.')` — a list of rows; "
        "`columns` are real column names, in display order. Use this for EVERY "
        "listing page.\n"
        "  - `card(title, body='', href='', image='', meta='')` — one item in a "
        "`<div class=\"grid\">`. `image` takes a `url_for('static', ...)` path.\n"
        "  - `field(name, label='', type='text', value='', required=false, "
        "placeholder='', hint='')` — one labelled form control; "
        "`type='textarea'` renders a textarea, `type='file'` an upload.\n"
        "  - `badge(text, kind='')` — a small status label.\n"
        "  - `empty_state(message, action_url='', action_label='')` — shown "
        "instead of an empty list.\n"
        "  - `flash_messages()` — only if a page needs them somewhere other "
        "than the top; base.html already renders them.\n"
        "- **Classes** for anything the macros don't cover: `.grid` (auto-fitting "
        "card grid), `.stack` (vertical rhythm), `.cluster` (inline row), "
        "`.card`, `.table-wrap` + `.table`, `.field`, `.form-narrow`, `.button` "
        "/ `.button-secondary` / `.button-danger` / `.button-small`, `.alert-"
        "success` / `-error` / `-warning` / `-info`, `.empty`, `.badge`, "
        "`.breadcrumb`, `.pagination`, `.sidebar-layout`, `.page-header`, "
        "`.hero`, `.lede`, `.visually-hidden`.\n"
        "- A wide table MUST sit inside `.table-wrap` or the page scrolls "
        "sideways on a phone — `ui.table()` already does this for you.\n"
        "- Colours and fonts are the CSS variables in `static/css/theme.css` "
        "(`var(--color-accent)`, `var(--font-heading)`, …). Never write a hex "
        "code or a font family into a page or a new stylesheet: a value that "
        "isn't a variable is the one thing a restyle cannot reach."
    )


# --- protecting the scaffold's invariants after generation -----------------
# The scaffold guarantees a runnable app. Generation then EDITS it, and a 7B
# model's SEARCH/REPLACE happily replaces the block it was supposed to add to.
# Measured on two consecutive live `build me a blog` runs: both times the edit
# to app.py deleted the scaffold's `/` route outright, so the finished site
# 404'd on its own home page. These two checks are deterministic and put the
# guarantee back — no LLM, no judgement call.

_INDEX_ROUTE_RE = re.compile(r"""@app\.route\(\s*["']/["']\s*[),]""")
_APP_ROUTE_RE = re.compile(r"@app\.route\(")
_MAIN_GUARD_RE = re.compile(r"^if __name__ == .__main__.:", re.MULTILINE)

_INDEX_ROUTE_SNIPPET = '''

@app.route("/")
def index():
    """Home page."""
    return render_template("index.html")

'''


def restore_index_route(source: str) -> tuple[str, bool]:
    """Put the `/` route back into app.py when generation removed it.

    Returns ``(source, restored)``. Conservative — it declines rather than
    guesses when the file is not a recognisable Flask app, when `/` is still
    routed, or when `render_template` is not imported (synthesizing a route that
    raises NameError would be worse than the 404 it replaces).
    """
    text = source or ""
    if not _APP_ROUTE_RE.search(text):
        return source, False  # not a Flask route file — nothing to reason about
    if _INDEX_ROUTE_RE.search(text):
        return source, False  # still there
    if "render_template" not in text:
        return source, False  # can't synthesize safely
    if re.search(r"^def index\(", text, re.MULTILINE):
        return source, False  # a view named index already exists; don't collide

    guard = _MAIN_GUARD_RE.search(text)
    if guard:
        cut = guard.start()
        new = text[:cut].rstrip("\n") + "\n" + _INDEX_ROUTE_SNIPPET + text[cut:]
    else:
        new = text.rstrip("\n") + "\n" + _INDEX_ROUTE_SNIPPET
    return new, True


_RUN_BLOCK_SNIPPET = """

if __name__ == "__main__":
    # Restored by Coder: a rewrite had removed the run block, so `python app.py`
    # imported the module and exited without ever serving.
    app.run(debug=True, port=5000)
"""


def restore_run_block(source: str) -> tuple[str, bool]:
    """Put back the `if __name__ == "__main__":` block that makes app.py run.

    The Flask half of `NodeAdapter.restore_boot_block`, and it exists for the
    same measured reason: a repair pass that rewrites the entry file wholesale
    can end it at the last route, and nothing downstream notices. The file still
    compiles — a module of route definitions is valid Python — so `check_file`
    passes and the build reports "verified OK" on an app that cannot start.

    Conservative in the same way as `restore_index_route`: it declines unless
    this is recognisably a Flask entry file with an `app` to run.
    """
    text = source or ""
    if not _APP_ROUTE_RE.search(text):
        return source, False  # not a Flask route file
    if _MAIN_GUARD_RE.search(text):
        return source, False  # still there
    if not re.search(r"^app\s*=\s*Flask\(", text, re.MULTILINE):
        return source, False  # no app to run — restoring would NameError
    return text.rstrip("\n") + "\n" + _RUN_BLOCK_SNIPPET.lstrip("\n"), True


_BODY_RE = re.compile(
    r"<body\b[^>]*>(?P<inner>.*?)</body\s*>", re.IGNORECASE | re.DOTALL
)
_HEAD_RE = re.compile(r"<head\b[^>]*>.*?</head\s*>", re.IGNORECASE | re.DOTALL)
_DOC_TAGS_RE = re.compile(
    r"<!doctype[^>]*>|</?html\b[^>]*>|</?body\b[^>]*>", re.IGNORECASE
)
_TITLE_RE = re.compile(
    r"<title\b[^>]*>(?P<t>.*?)</title\s*>", re.IGNORECASE | re.DOTALL
)
# base.html already renders these. Left in a child they appear twice — two
# navbars on one page, which is worse than the bug this layout removes.
_CHROME_RE = re.compile(
    r"<(?P<tag>header|nav|footer)\b[^>]*>.*?</(?P=tag)\s*>", re.IGNORECASE | re.DOTALL
)
_APP_SCRIPT_RE = re.compile(
    r"<script\b[^>]*\bsrc\s*=\s*[\"'][^\"']*(?:app\.js|style\.css)[\"'][^>]*>\s*</script\s*>",
    re.IGNORECASE,
)


def document_inner(source: str) -> tuple[str, str]:
    """``(title, page content)`` from a full HTML document, chrome removed.

    The stack-agnostic half of `convert_to_child_template`: lift the `<body>`
    contents, carry the `<title>`, and drop the `<header>`/`<nav>`/`<footer>`
    and the app script that the layout — `base.html` on Flask, `layout.ejs` on
    Node — already renders. Leaving them in renders *two* navbars, which is
    worse than the drift the layout exists to prevent.

    Returns ``("", "")`` when there is no document to convert or nothing would
    survive, so every caller's "decline rather than guess" is one falsy check.
    """
    text = source or ""
    if "<html" not in text.lower():
        return "", ""

    title = ""
    match = _TITLE_RE.search(text)
    if match:
        title = " ".join(match.group("t").split())[:120]

    body = _BODY_RE.search(text)
    if body:
        inner = body.group("inner")
    else:
        inner = _HEAD_RE.sub("", text)
        inner = _DOC_TAGS_RE.sub("", inner)

    inner = _CHROME_RE.sub("", inner)
    inner = _APP_SCRIPT_RE.sub("", inner)
    inner = re.sub(r"\n{3,}", "\n\n", inner).strip()
    return (title, inner) if inner else ("", "")


def convert_to_child_template(source: str) -> tuple[str, bool]:
    """Rewrite a full `<html>` page as a child of `base.html`.

    The scaffold's whole structural claim is that `base.html` defines the nav
    once, so pages cannot drift apart. A generated page that ships its own
    `<html>`/`<head>`/`<nav>` opts out of that and brings the "every page has a
    different navbar" bug straight back — measured on roughly a third of
    generated templates even with the instruction stated in the prompt.

    Deterministic: lift the `<body>` contents into `{% block content %}`, carry
    the `<title>` into `{% block title %}`, and drop the chrome `base.html`
    already renders. Returns ``(source, converted)`` and declines — leaving the
    file untouched for the caller to report — whenever the result would be
    empty or there is no document to convert.
    """
    text = source or ""
    if "{% extends" in text:
        return source, False
    title, inner = document_inner(text)
    if not inner:
        # Nothing survived — the page was pure chrome, or we misread it. Better
        # to leave a wrong-shaped page than to replace it with an empty one.
        return source, False

    parts = ['{% extends "base.html" %}', ""]
    if title:
        parts += ["{% block title %}" + title + "{% endblock %}", ""]
    parts += ["{% block content %}", inner, "{% endblock %}", ""]
    return "\n".join(parts), True


# --- editing a child template one block at a time (Phase W3) ----------------
# `_surgical_edit` runs SEARCH/REPLACE across the whole file, and a 7B model's
# reply to "add a search box to this page" routinely replaces the block it was
# asked to add to — `{% extends %}` and all. That is measured behaviour, and it
# is the entire reason `_restore_scaffold_invariants` / `convert_to_child_template`
# exist. Repairing it afterwards is strictly worse than not causing it: the
# repair has to guess which chrome belonged to base.html.
#
# So hand the model only the body of the block it is editing. `{% extends %}`,
# `{% block title %}` and every other block are then untouchable by
# construction, not by instruction.

_EXTENDS_RE = re.compile(r"{%-?\s*extends\s")
_BLOCK_OPEN_RE = re.compile(r"{%-?\s*block\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*-?%}")
_BLOCK_END_RE = re.compile(r"{%-?\s*endblock(?:\s+[A-Za-z_][A-Za-z0-9_]*)?\s*-?%}")

# The block an edit to a page almost always belongs in. Everything else on a
# child template is chrome that base.html owns.
_MAIN_BLOCK = "content"
# Never edited in isolation: a one-line block gives SEARCH/REPLACE nothing to
# match, and a request that really is about the title is exactly the case the
# whole-file fallback exists for.
_NEVER_ALONE = frozenset({"title", "nav", "head", "scripts", "styles"})


@dataclass(frozen=True)
class BlockRegion:
    """The body of one `{% block %}`, and where it sits in the file.

    ``start``/``end`` bound the body only — the `{% block %}` and
    `{% endblock %}` tags themselves are outside the span, so splicing can never
    lose them.
    """

    name: str
    start: int
    end: int
    body: str
    siblings: tuple[str, ...] = ()

    def splice(self, source: str, new_body: str) -> str:
        """``source`` with this region's body replaced. The only writer."""
        return source[: self.start] + new_body + source[self.end :]


def _is_template_path(filename: str) -> bool:
    name = (filename or "").replace("\\", "/").lower().lstrip("./")
    if not name.endswith(_PAGE_SUFFIXES):
        return False
    return name.startswith("templates/") or "/templates/" in name


def _top_level_blocks(text: str) -> list[tuple[str, int, int, bool]]:
    """Every outermost `{% block %}`: (name, body_start, body_end, has_nested).

    Returns [] when the tags do not balance — an unclosed block means we cannot
    say where anything ends, and guessing would splice an edit into the wrong
    half of the file.
    """
    events: list[tuple[int, int, int, str]] = []  # (pos, kind, end, name)
    for match in _BLOCK_OPEN_RE.finditer(text):
        events.append((match.start(), 1, match.end(), match.group("name")))
    for match in _BLOCK_END_RE.finditer(text):
        events.append((match.start(), -1, match.end(), ""))
    events.sort()

    out: list[tuple[str, int, int, bool]] = []
    depth = 0
    open_name = ""
    body_start = 0
    nested = False
    for pos, kind, end, name in events:
        if kind == 1:
            if depth == 0:
                open_name, body_start, nested = name, end, False
            else:
                nested = True
            depth += 1
        else:
            if depth == 0:
                return []  # a stray {% endblock %} — the file is not parseable
            depth -= 1
            if depth == 0:
                out.append((open_name, body_start, pos, nested))
    if depth != 0:
        return []  # unclosed block
    return out


def template_edit_region(filename: str, text: str) -> BlockRegion | None:
    """The single block an edit to this template should be confined to, or None.

    None means "use the existing whole-file path" — and it is returned for
    everything ambiguous, deliberately. The fallback is today's tested
    behaviour, so declining costs nothing; a wrong span would splice an edit
    into the middle of a tag.

    Declines when: the file is not a `.html` under `templates/`; it carries no
    `{% extends %}` (a full document is `convert_to_child_template`'s problem,
    not this one); the block tags do not balance; the chosen block contains a
    nested block; or the body is empty (there is nothing to SEARCH for).
    """
    if not _is_template_path(filename):
        return None
    source = text or ""
    if not _EXTENDS_RE.search(source):
        return None

    blocks = _top_level_blocks(source)
    if not blocks:
        return None
    names = tuple(name for name, _s, _e, _n in blocks)

    chosen = next((b for b in blocks if b[0].lower() == _MAIN_BLOCK), None)
    if chosen is None:
        usable = [b for b in blocks if b[0].lower() not in _NEVER_ALONE]
        if len(usable) != 1:
            return None  # two candidates means guessing which one the edit is in
        chosen = usable[0]

    name, start, end, has_nested = chosen
    if has_nested:
        return None
    body = source[start:end]
    if not body.strip():
        return None
    return BlockRegion(
        name=name,
        start=start,
        end=end,
        body=body,
        siblings=tuple(n for n in names if n != name),
    )


def documents_without_layout(
    root: Path,
    subdir: str,
    ext: str,
    *,
    skip: tuple[str, ...] = (),
    skip_prefixes: tuple[str, ...] = (),
    marker: str = "",
) -> list[str]:
    """Page templates that are full `<html>` documents instead of using the layout.

    The layout file exists so the nav is defined once. A page that ships its own
    `<html>`/`<head>`/`<nav>` opts out of that entirely and brings back the
    "every page has a different navbar" bug the shell was meant to make
    impossible. Reported here, not rewritten — the caller decides.

    ``marker``, when given, is the text a correctly-shaped page carries
    (`{% extends` on Jinja). Node's layout engine wraps a view that carries no
    marker at all, so it passes ``""`` and every document is an orphan.
    """
    tpl_dir = Path(root) / subdir
    if not tpl_dir.is_dir():
        return []
    out: list[str] = []
    for path in sorted(tpl_dir.rglob(f"*{ext}")):
        if path.name in skip or path.name.startswith(skip_prefixes or ()):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if marker and marker in text:
            continue
        if "<html" in text.lower():
            out.append(f"{subdir}/{path.relative_to(tpl_dir).as_posix()}")
    return out


def templates_without_inheritance(root: Path) -> list[str]:
    """Jinja pages that are full documents instead of extending `base.html`."""
    return documents_without_layout(
        root, "templates", ".html", skip=("base.html",), marker="{% extends"
    )


def scaffold_context(written: list[str] | set[str]) -> str:
    """The prompt block telling generation what the scaffold already provides.

    Without this the model writes its own `get_db()` into app.py and its own
    nav into every page — reinventing exactly what was just copied in, and
    diverging from it. Compact by design: it rides in the same prompt as the
    plan manifest and the sibling context, inside `llm_num_ctx`.
    """
    if not written:
        return ""
    return (
        "## Project skeleton — ALREADY CREATED, do not rewrite it\n"
        "A working Flask app is already on disk and already runs. You are adding "
        "this project's own features to it, not building it from scratch:\n"
        "- **ADD to these files; never delete what is already in them.** In "
        "particular `app.py` already defines the `/` route (`def index()`) — keep "
        "that route and add your new ones alongside it. The template it renders, "
        "`templates/index.html`, is only a PLACEHOLDER: the home page itself "
        "still has to be written.\n"
        "- **Import every name you use.** `app.py` currently imports only "
        "`Flask, render_template`. If your routes use `request`, `redirect`, "
        "`url_for`, `flash`, `session`, `jsonify` or `abort`, add them to the "
        "`from flask import ...` line, and add `from models import ...` for the "
        "query helpers you call. A missing import still compiles and then fails "
        "with a 500 at runtime — the check cannot catch it for you.\n"
        "- `app.py` holds routes ONLY. Add `@app.route` functions to it. The "
        "`Flask(__name__)` app object, config and `db.init_db()` call already exist.\n"
        "- `db.py` owns the connection and the schema. Use `from db import get_db`; "
        "add `CREATE TABLE IF NOT EXISTS` statements inside `init_db()`, and use "
        "`ensure_column(conn, table, column, decl)` for a field added later. Never "
        "open sqlite3 anywhere else.\n"
        "- `models.py` owns every query, one function per operation, always with "
        "`?` parameters. Routes call these helpers; routes never write SQL.\n"
        "- `templates/base.html` owns the navigation and the page shell. EVERY "
        'page starts with `{% extends "base.html" %}` and puts its markup in '
        "`{% block content %}`. Never write a full `<html>` document in a child "
        "template, and never copy the nav into one — add links to the `{% block nav %}` "
        "in base.html instead.\n"
        "- `static/css/style.css` (components) and `static/css/theme.css` (colour "
        "and font variables) are the ONLY stylesheets, both linked by base.html. "
        "Never add a third one, and never write a hex code or a font family into "
        "a page — use `var(--color-accent)`, `var(--font-heading)` and the other "
        "variables. See the components section below.\n"
        "- `seed.py` holds demo rows so no page is ever empty.\n"
        "- `requirements.txt`, `Procfile` and `.gitignore` are done. Leave them alone."
    )
