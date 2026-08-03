"""Which backend stack actually RUNS on this machine — grounded, not aspirational.

The Requirements Blueprint (`app/agent/blueprint.py`) wants to answer "what
happens after I press the button" with a real, working backend. But Coder is
**offline** and its network gate is on by default (`settings.allow_network` is
False), so it usually cannot `pip install flask` or `npm install express`. A
blueprint that emits a Flask app on a machine with no Flask produces files that
don't run — which is *worse* than shipping no backend, because the report then
lies about it.

So the blueprint never guesses a stack: it asks this module which one is present
*here*, in preference order, and only ever proposes one that will actually start.
The default is the Python standard library — `http.server`/`wsgiref` + stdlib
`sqlite3` + `json` — which needs no install and runs fully offline everywhere,
turning "offline" from a limitation into the feature's grounding principle.

**`prefer` forces a stack, and a forced stack is never silently downgraded**
(`settings.web_stack`, default `"flask"` — Phase A of
`docs/always-fullstack-plan.md`). Probing is the right default for a generic
agent; it is the wrong one for a tool that promises "every website is built on
Flask/Jinja/SQLite", because the promise then quietly depends on what happens to
be importable. When a forced stack is absent this returns it anyway with
`runnable=False` and an `install_hint`, so the caller reports "install Flask"
rather than building a different kind of app under the same description.
Returning the stdlib stack there would be indistinguishable downstream from a
build that was always meant to be stdlib — the silent-truncation failure class
that `blueprint_max_files` and the coverage check already exist to prevent.

Pure and cheap: `find_spec`/`which` only, no imports of the frameworks
themselves, no network. Fully injectable for offline tests.
"""

from __future__ import annotations

import importlib.util
import shutil
from dataclasses import dataclass


@dataclass(frozen=True)
class Stack:
    """A backend runtime, either probed as present or forced by the caller."""

    language: str  # "python" | "node" | "none"
    backend: str  # "stdlib" | "flask" | "fastapi" | "express" | "none"
    runnable: bool = True  # proven present (find_spec / which succeeded)
    note: str = ""  # instruction block threaded into the generation prompt
    # User-facing, and deliberately NOT part of `note`: when a forced stack is
    # absent, the model must still be told to write that stack (it is what the
    # generated project's own requirements.txt declares), while the *user* is
    # told to install it. Folding this into `note` would do the opposite —
    # `prompts/blueprint.md` says "do NOT use a framework that isn't listed —
    # it is not installed", so the model would read the warning and quietly
    # write a stdlib app instead. That is the exact downgrade this reports.
    install_hint: str = ""

    def to_prompt_line(self) -> str:
        return self.note or f"{self.language} / {self.backend}"


# The always-available fallback: stdlib needs no install and runs offline.
STDLIB_STACK = Stack(
    language="python",
    backend="stdlib",
    runnable=True,
    note=(
        "Python standard library only — http.server (BaseHTTPRequestHandler) or "
        "wsgiref for the server, sqlite3 for storage, json for bodies. NO "
        "third-party imports (no Flask/FastAPI/Django): they are not installed "
        "and there is no network to install them. This runs offline as-is."
    ),
)

NO_STACK = Stack(language="none", backend="none", runnable=True, note="")


def _has_module(name: str) -> bool:
    """True if an import of ``name`` would resolve, without importing it."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def detect_stack(
    allow_network: bool = False,
    *,
    prefer: str = "auto",
    _has_module=_has_module,
    _which=shutil.which,
) -> Stack:
    """Pick a backend stack: the one the caller forced, else one proven present.

    ``prefer`` names the stack to build on (flask/fastapi/node/stdlib/none) and
    is **honoured whether or not it is installed** — an absent one comes back
    with ``runnable=False`` and an ``install_hint`` instead of being swapped for
    something else. See the module docstring for why the swap is the bug.

    ``prefer="auto"`` restores pure probing, in preference order:
      1. A Python web framework already importable in the venv (Flask, then
         FastAPI) — richer than stdlib and no install needed.
      2. Node + a resolvable express (or ``allow_network`` so npm may install).
      3. The stdlib stack — the always-safe offline default.

    ``prefer="none"`` forces a frontend-only build (no backend proposed).
    An unrecognised ``prefer`` falls through to auto. The ``_has_module`` /
    ``_which`` seams are injected in tests so the choice is deterministic
    offline.
    """
    if prefer == "none":
        return NO_STACK
    if prefer == "stdlib":
        return STDLIB_STACK  # stdlib is always present, so always runnable
    if prefer == "flask":
        return _flask(runnable=_has_module("flask"))
    if prefer == "fastapi":
        return _fastapi(runnable=_has_module("fastapi"))
    if prefer == "node":
        # A FORCED node stack is the named "node" stack of docs/node-stack-plan.md:
        # Express + EJS + PostgreSQL, always. The probe-path `_node()` below
        # degrades to Node's built-in `http` when the network is off, which is
        # right when we are guessing and wrong when the user asked for this
        # stack by name — the scaffold on disk requires express, so a note
        # telling the model to avoid it would contradict the files it is
        # editing. `npm install` needing the network is reported in
        # `install_hint`, never folded into `note`.
        return _node_express(runnable=bool(_which("node")))

    # auto: richest present option wins, else stdlib.
    if _has_module("flask"):
        return _flask()
    if _has_module("fastapi"):
        return _fastapi()
    if _which("node") and (allow_network or _node_dep_present(_has_module)):
        return _node(allow_network)
    return STDLIB_STACK


def _flask(runnable: bool = True) -> Stack:
    return Stack(
        language="python",
        backend="flask",
        runnable=runnable,
        install_hint=(
            ""
            if runnable
            else (
                "Flask is not installed in this environment, so the generated "
                "app cannot start here yet — run `pip install flask` in the "
                "venv Coder runs from. The project's own requirements.txt "
                "already declares it; Coder launches generated apps with "
                "`sys.executable`, so it is the same interpreter."
            )
        ),
        note=(
            "The stack for this build is server-rendered Flask + Jinja2 "
            "templates + stdlib sqlite3. One process serves pages, static "
            "files and uploads; "
            "no separate frontend server, no build step, no React.\n"
            "Use EXACTLY this layout:\n"
            "  app.py           routes only (@app.route), no SQL\n"
            "  db.py            get_db(), init_db(), ensure_column() migrations\n"
            "  models.py        one query helper per operation, ? parameters only\n"
            "  seed.py          a few demo rows per table\n"
            "  templates/       Jinja2 pages; base.html holds the nav and shell\n"
            "  static/css|js|uploads\n"
            'Every page starts with {% extends "base.html" %} and puts its markup '
            "in {% block content %} — never write a full <html> document in a child "
            "template, and never copy the nav into one. Prefer a plain "
            '<form method="post" action="/route"> over fetch(): it works with no '
            "JavaScript at all, so the button cannot silently do nothing."
        ),
    )


def _fastapi(runnable: bool = True) -> Stack:
    return Stack(
        language="python",
        backend="fastapi",
        runnable=runnable,
        install_hint=(
            ""
            if runnable
            else (
                "FastAPI is not installed in this environment — run "
                "`pip install fastapi uvicorn` before running the generated app."
            )
        ),
        note=(
            "FastAPI is installed — use it for the server with an ASGI app and a "
            "uvicorn run line. Use stdlib sqlite3 for storage. Add a "
            "requirements.txt listing fastapi and uvicorn."
        ),
    )


def _node_dep_present(_has_module) -> bool:
    # We can't find_spec Node packages; treat node as usable only when the caller
    # allows install (checked separately) — so with the network off and no way to
    # confirm express is vendored, prefer the stdlib Python stack instead.
    return False


def _node_express(runnable: bool = True) -> Stack:
    """The named Node stack: Express + EJS + PostgreSQL (`web_stack="node"`).

    `install_hint` is non-empty even when Node IS present, and that is
    deliberate: unlike sqlite, this stack has a package install and a database
    daemon between "the files are correct" and "the app runs". Reporting them
    every time is the honest read; folding them into `note` would make
    `prompts/blueprint.md`'s "don't use what isn't installed" rule fire and
    quietly produce something else — the downgrade this module exists to
    prevent.
    """
    missing_node = (
        ""
        if runnable
        else "Node.js was not found on PATH — install it (nodejs.org) first. Then "
    )
    return Stack(
        language="node",
        backend="express",
        runnable=runnable,
        install_hint=(
            f"{missing_node}run `npm install` in the project (it needs the "
            "network once) and make sure PostgreSQL is running with the "
            "project's database created — the generated app exits on startup "
            "rather than serving pages it cannot back with data."
        ),
        note=(
            "The stack for this build is server-rendered Express + EJS views + "
            "PostgreSQL via the `pg` package. One process serves pages, static "
            "files and uploads; no separate frontend server, no build step, no "
            "React.\n"
            "Use EXACTLY this layout:\n"
            "  server.js        routes only (app.get/app.post), no SQL\n"
            "  db.js            getPool(), initDb(), ensureColumn() migrations\n"
            "  models.js        one query helper per operation, $1 parameters only\n"
            "  seed.js          a few demo rows per table\n"
            "  views/           EJS pages; layout.ejs holds the nav and shell\n"
            "  public/css|js|uploads\n"
            "Every view is a FRAGMENT wrapped by layout.ejs — never write a full "
            "<html> document in a view, and never copy the nav into one. Route "
            "handlers are `async` and `await` the model helpers. Values are "
            "bound as $1, $2, … parameters, never concatenated into the SQL, and "
            "an INSERT that needs the new id ends with RETURNING id. Prefer a "
            'plain <form method="post" action="/route"> over fetch(): it works '
            "with no JavaScript at all, so the button cannot silently do nothing."
        ),
    )


def _node(allow_network: bool, runnable: bool = True) -> Stack:
    install = (
        "Run `npm install express` (network is permitted)."
        if allow_network
        else "Use only Node's built-in `http` module — do NOT require('express') "
        "or any package that isn't vendored; there is no network to install it."
    )
    return Stack(
        language="node",
        backend="express" if allow_network else "stdlib",
        runnable=runnable,
        install_hint=(
            ""
            if runnable
            else "Node.js was not found on PATH — install it to run the generated app."
        ),
        note=("Node.js is available. " if runnable else "Target runtime is Node.js. ")
        + install,
    )
