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

_NAME_CLEAN_RE = re.compile(r"[^A-Za-z0-9]+")


def flask_scaffold_dir() -> Path:
    """Where the Flask template tree lives. Resolved from the package, per the
    "Bundled resources & packaging" rule in CLAUDE.md — never from cwd."""
    return Path(settings.scaffolds_dir) / "flask"


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
    return name.endswith(_PAGE_SUFFIXES) or name.startswith("templates/")


def scaffold_files() -> set[str]:
    """Relative paths (posix) the Flask scaffold writes.

    Used to tell generation what already exists, so it imports `db`/`models`
    instead of reinventing them. Reads the template tree rather than hardcoding
    a list, so adding a file to the scaffold cannot desynchronise this.
    """
    root = flask_scaffold_dir()
    if not root.is_dir():
        return set()
    out: set[str] = set()
    for src in root.rglob("*"):
        if src.is_file():
            out.add(_destination_name(src.relative_to(root)))
    return out


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


def scaffold_flask(root: Path, name: str | None = None) -> list[str]:
    """Copy the runnable Flask skeleton into ``root``.

    Returns the relative paths actually written, sorted. **Never overwrites an
    existing file** — so a later turn amending the project is a no-op here, and
    a user's edited `style.css` is never silently reverted. That property is
    what makes it safe to call this on every build turn.

    Best-effort per file: one unreadable template does not abort the scaffold.
    """
    root = Path(root)
    src_root = flask_scaffold_dir()
    if not src_root.is_dir():
        logger.warning("flask scaffold missing at %s", src_root)
        return []

    values = {
        "{{PROJECT_NAME}}": name or project_name(root),
        # A fresh key per project. Regenerating it on a re-scaffold would log
        # every existing session out, but re-scaffolding never rewrites app.py
        # (see above), so the key each project ships with is stable.
        "{{SECRET_KEY}}": secrets.token_hex(32),
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
    if "<html" not in text.lower():
        return source, False

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
    if not inner:
        # Nothing survived — the page was pure chrome, or we misread it. Better
        # to leave a wrong-shaped page than to replace it with an empty one.
        return source, False

    parts = ['{% extends "base.html" %}', ""]
    if title:
        parts += ["{% block title %}" + title + "{% endblock %}", ""]
    parts += ["{% block content %}", inner, "{% endblock %}", ""]
    return "\n".join(parts), True


def templates_without_inheritance(root: Path) -> list[str]:
    """Page templates that are full `<html>` documents instead of extending base.

    `base.html` exists so the nav is defined once. A child template that ships
    its own `<html>`/`<head>`/`<nav>` opts out of that entirely and brings back
    the "every page has a different navbar" bug the shell was meant to make
    impossible. Reported, not rewritten — converting a document into a block is
    a content decision, and this pass is deterministic by design.
    """
    tpl_dir = Path(root) / "templates"
    if not tpl_dir.is_dir():
        return []
    out: list[str] = []
    for path in sorted(tpl_dir.rglob("*.html")):
        if path.name == "base.html":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if "{% extends" in text:
            continue
        if "<html" in text.lower():
            out.append(f"templates/{path.relative_to(tpl_dir).as_posix()}")
    return out


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
        "particular `app.py` already defines the `/` route (`def index()`), which "
        "is the home page — keep it and add your new routes alongside it.\n"
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
        "- `static/css/style.css` is the ONE stylesheet, linked by base.html. Fonts "
        "are the CSS variables `var(--font-heading)` / `var(--font-body)`.\n"
        "- `seed.py` holds demo rows so no page is ever empty.\n"
        "- `requirements.txt`, `Procfile` and `.gitignore` are done. Leave them alone."
    )
