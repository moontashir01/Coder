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

Pure and cheap: `find_spec`/`which` only, no imports of the frameworks
themselves, no network. Fully injectable for offline tests.
"""

from __future__ import annotations

import importlib.util
import shutil
from dataclasses import dataclass


@dataclass(frozen=True)
class Stack:
    """A backend runtime chosen because it is installed on THIS machine."""

    language: str  # "python" | "node" | "none"
    backend: str  # "stdlib" | "flask" | "fastapi" | "express" | "none"
    runnable: bool = True  # proven present (find_spec / which succeeded)
    note: str = ""  # human line threaded into the generation prompt

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
    """Pick a backend stack that is proven to run on this machine.

    Preference order, each step gated on real presence:
      1. An explicit ``prefer`` the caller forced (flask/fastapi/node/stdlib),
         but only if it's actually present — otherwise fall through.
      2. A Python web framework already importable in the venv (Flask, then
         FastAPI) — richer than stdlib and no install needed.
      3. Node + a resolvable express (or ``allow_network`` so npm may install).
      4. The stdlib stack — the always-safe offline default.

    ``prefer="none"`` forces a frontend-only build (no backend proposed).
    The ``_has_module`` / ``_which`` seams are injected in tests so the choice is
    deterministic offline.
    """
    if prefer == "none":
        return NO_STACK
    if prefer == "stdlib":
        return STDLIB_STACK
    if prefer == "flask" and _has_module("flask"):
        return _flask()
    if prefer == "fastapi" and _has_module("fastapi"):
        return _fastapi()
    if prefer == "node" and _which("node"):
        return _node(allow_network, _has_module)

    # auto: richest present option wins, else stdlib.
    if _has_module("flask"):
        return _flask()
    if _has_module("fastapi"):
        return _fastapi()
    if _which("node") and (allow_network or _node_dep_present(_has_module)):
        return _node(allow_network, _has_module)
    return STDLIB_STACK


def _flask() -> Stack:
    return Stack(
        language="python",
        backend="flask",
        runnable=True,
        note=(
            "Flask is installed — server-rendered Flask + Jinja2 templates + "
            "stdlib sqlite3. One process serves pages, static files and uploads; "
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


def _fastapi() -> Stack:
    return Stack(
        language="python",
        backend="fastapi",
        runnable=True,
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


def _node(allow_network: bool, _has_module) -> Stack:
    install = (
        "Run `npm install express` (network is permitted)."
        if allow_network
        else "Use only Node's built-in `http` module — do NOT require('express') "
        "or any package that isn't vendored; there is no network to install it."
    )
    return Stack(
        language="node",
        backend="express" if allow_network else "stdlib",
        runnable=True,
        note="Node.js is available. " + install,
    )
