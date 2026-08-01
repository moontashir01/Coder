"""Declarative outcome checks for eval tasks.

Each factory returns a ``Check``: a callable ``(CheckContext) -> (bool, str)``.
The string is a human-readable detail shown when the check fails (and kept for
passing checks too, so a report can explain what was verified).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable, Tuple

if TYPE_CHECKING:
    from evals.harness import CheckContext

Check = Callable[["CheckContext"], Tuple[bool, str]]


def answer_contains(substring: str, case_insensitive: bool = True) -> Check:
    """The agent's textual answer includes ``substring``."""

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        hay = ctx.answer or ""
        needle = substring
        if case_insensitive:
            hay, needle = hay.lower(), needle.lower()
        ok = needle in hay
        return ok, f"answer {'contains' if ok else 'is missing'} {substring!r}"

    return check


def file_exists(relpath: str) -> Check:
    """A file was created at ``relpath`` under the task's working dir."""

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        ok = (ctx.workdir / relpath).is_file()
        return ok, f"file {relpath} {'exists' if ok else 'was not created'}"

    return check


def file_contains(relpath: str, substring: str) -> Check:
    """File ``relpath`` exists and its text includes ``substring``."""

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        p = ctx.workdir / relpath
        if not p.is_file():
            return (
                False,
                f"file {relpath} not found (expected to contain {substring!r})",
            )
        ok = substring in p.read_text(encoding="utf-8", errors="replace")
        return ok, f"{relpath} {'contains' if ok else 'is missing'} {substring!r}"

    return check


def file_excludes(relpath: str, substring: str) -> Check:
    """File ``relpath`` does NOT contain ``substring`` (a missing file passes).

    Use for "the inline <style> was moved out of index.html".
    """

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        p = ctx.workdir / relpath
        if not p.is_file():
            return True, f"file {relpath} absent → cannot contain {substring!r}"
        ok = substring not in p.read_text(encoding="utf-8", errors="replace")
        return ok, f"{relpath} {'excludes' if ok else 'still contains'} {substring!r}"

    return check


# ---------------------------------------------------------------------------
# Full-stack web checks (Phase 7) — these assert the app WORKS, not that files
# with plausible names appeared. `docs/fullstack-web-plan.md`.
# ---------------------------------------------------------------------------


def _spec(ctx: "CheckContext"):
    from app.agent.projectspec import ProjectSpec

    return ProjectSpec.load(ctx.workdir)


def spec_has_entity(name: str) -> Check:
    """The project REMEMBERS this entity — the thing turn 2 will amend."""

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        spec = _spec(ctx)
        if spec is None:
            return False, f"no project spec, so no {name} entity"
        found = spec.entity(name)
        names = ", ".join(e.name for e in spec.entities) or "none"
        return bool(found), (
            f"spec has entity {name}" if found else f"spec entities are: {names}"
        )

    return check


def spec_has_endpoint(method: str, path_fragment: str) -> Check:
    """A route matching ``method`` and containing ``path_fragment`` is recorded."""

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        spec = _spec(ctx)
        if spec is None:
            return False, "no project spec"
        hits = [
            e
            for e in spec.endpoints
            if e.method == method.upper() and path_fragment in e.path
        ]
        listed = ", ".join(f"{e.method} {e.path}" for e in spec.endpoints) or "none"
        return bool(hits), (
            f"spec has {method.upper()} …{path_fragment}"
            if hits
            else f"spec routes are: {listed}"
        )

    return check


def db_has_column(table: str, column: str) -> Check:
    """The real SQLite file has this column — the migration actually ran.

    Asks the database, not the source: a `CREATE TABLE` in a file nobody
    executes proves nothing, and this is the check that distinguishes "the
    schema changed" from "the schema was described".
    """

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        import sqlite3

        dbs = sorted(Path(ctx.workdir).glob("*.db"))
        if not dbs:
            return False, f"no .db file, so no {table}.{column}"
        for path in dbs:
            conn = sqlite3.connect(path)
            try:
                cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            except Exception:
                cols = set()
            finally:
                conn.close()
            if column in cols:
                return True, f"{table}.{column} exists in {path.name}"
            if cols:
                return False, f"{table} has: {', '.join(sorted(cols)) or 'no columns'}"
        return False, f"table {table} not found in {dbs[0].name}"

    return check


def app_serves(routes: list[str], label: str = "") -> Check:
    """Start the generated app and require every route to answer 2xx/3xx.

    The real question, asked the only way it can be answered honestly: by
    running the thing. Uses the same process handling as the smoke test, so the
    tree is always killed afterwards.
    """

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        from app.agent.apprunner import AppRunner
        from app.agent.smoke import _request

        entry = Path(ctx.workdir) / "app.py"
        if not entry.is_file():
            return False, "no app.py to run"

        runner = AppRunner()
        try:
            ok, message = runner.start(ctx.workdir)
            if not ok:
                return False, f"app did not start: {message}"
            results = []
            for route in routes:
                status, _ = _request(runner._port, "GET", route)
                results.append((route, status))
            bad = [f"{r} -> {s}" for r, s in results if not (s and 200 <= s < 400)]
            name = label or "routes"
            if bad:
                return False, f"{name}: {', '.join(bad)}"
            return True, f"{name}: all {len(routes)} served ({', '.join(routes)})"
        finally:
            runner.stop()

    return check


def earlier_pages_still_work(routes: list[str]) -> Check:
    """Pages built in an EARLIER turn must still serve after later turns.

    The headline number. This is the faculty's actual complaint made
    measurable: it is not "did turn 3 work" but "did turn 3 break turn 1".
    Measured live during Phase 3, an amendment deleted turn 1's `/products`
    route while reporting success — the file compiled, the new route worked, and
    nothing else could see it.
    """
    inner = app_serves(routes, label="earlier pages")
    return inner


def post_persists(path: str, fields: dict, appears_on: str) -> Check:
    """POST to ``path``, then require the value to show up on ``appears_on``.

    "It started" and "it works" are different claims. This asks the second one:
    a handler that answers 302 and never writes passes every other check in this
    file and fails this one.
    """
    marker = "EvalProbe5b2c"

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        from urllib.parse import urlencode

        from app.agent.apprunner import AppRunner
        from app.agent.smoke import _request, server_error

        if not (Path(ctx.workdir) / "app.py").is_file():
            return False, "no app.py to run"

        payload = {k: (v if v else f"{marker} {k}") for k, v in fields.items()}
        runner = AppRunner()
        try:
            ok, message = runner.start(ctx.workdir)
            if not ok:
                return False, f"app did not start: {message}"
            port = runner._port
            status, text = _request(
                port,
                "POST",
                path,
                body=urlencode(payload).encode(),
                content_type="application/x-www-form-urlencoded",
            )
            if status is None:
                return False, f"POST {path} got no response"
            if status >= 500:
                why = server_error(text)
                return False, f"POST {path} -> {status}" + (f" ({why})" if why else "")
            _, listing = _request(port, "GET", appears_on)
            if marker in (listing or ""):
                return True, f"POST {path} -> {status}; value visible on {appears_on}"
            return False, (
                f"POST {path} -> {status} but the value never appeared on {appears_on}"
            )
        finally:
            runner.stop()

    return check


def used_tool(tool_name: str) -> Check:
    """The tool trace contains a call to ``tool_name``."""

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        ok = any(t.get("tool") == tool_name for t in ctx.trace)
        return ok, f"tool {tool_name} {'was' if ok else 'was NOT'} called"

    return check


def min_files_written(n: int) -> Check:
    """At least ``n`` successful write_file/create_file calls happened."""

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        count = sum(
            1
            for t in ctx.trace
            if t.get("tool") in ("write_file", "create_file")
            and (t.get("result") or {}).get("success")
        )
        ok = count >= n
        return ok, f"{count} file(s) written (need >= {n})"

    return check


# ---------------------------------------------------------------------------
# Cross-file coherence checks (weaknesses.md #7) — the failures a
# file-exists + substring eval can't see: a build that's all layout and no
# behaviour, a form whose submit reaches no server, a backend the frontend
# never calls. These are what the Requirements Blueprint feature is measured on.
# ---------------------------------------------------------------------------

# Server-side languages, and client-side ones. `.js`/`.ts` are in BOTH — a
# single-file Node app is server and client at once — which is fine for a coarse
# "does it connect" signal (see route_wired).
_SERVER_EXTS = (".py", ".js", ".mjs", ".ts", ".go", ".rb", ".php", ".java")
_CLIENT_EXTS = (".html", ".htm", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte")
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".coder_backups", ".chroma_db"}

# Markers that a JS/TS file is a real SERVER, not just a client script — so
# has_backend_server() doesn't count a `script.js` as a backend.
_SERVER_MARKERS = (
    "BaseHTTPRequestHandler",
    "http.server",
    "HTTPServer",
    "socketserver",
    "wsgiref",
    "Flask(",
    "@app.route",
    "FastAPI(",
    "app.listen",
    "createServer",
    "app.get(",
    "app.post(",
    "express(",
)


def _iter_files(workdir: Path, exts: tuple[str, ...] | None):
    for p in sorted(Path(workdir).rglob("*")):
        if not p.is_file():
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        if exts and p.suffix.lower() not in exts:
            continue
        yield p


def _first_containing(
    workdir: Path, substring: str, exts: tuple[str, ...] | None
) -> str | None:
    """Relative path of the first matching file that contains ``substring``."""
    for p in _iter_files(workdir, exts):
        try:
            if substring in p.read_text(encoding="utf-8", errors="ignore"):
                return str(p.relative_to(workdir))
        except Exception:
            continue
    return None


def any_file_matches(substrings, exts=None, label: str = "") -> Check:
    """Pass if ANY of ``substrings`` appears in ANY file (optionally ext-filtered).

    Robust to the filename/spelling the model happens to pick: use it for "some
    page has a <form>", "the frontend submits somehow" (fetch/action/onsubmit),
    etc. ``label`` names the concept in the failure detail.
    """
    subs = tuple(substrings)
    ext_t = tuple(e.lower() for e in exts) if exts else None

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        what = label or "marker"
        for sub in subs:
            where = _first_containing(ctx.workdir, sub, ext_t)
            if where is not None:
                return True, f"{what}: {sub!r} found in {where}"
        return False, f"{what}: none of {list(subs)} found in any file"

    return check


def has_backend_server() -> Check:
    """A real server file exists — a `.py` file, or a JS/TS file with a server
    marker. A static, frontend-only build (the old failure — "just the layout")
    fails this. This is the single most important blueprint check."""

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        for p in _iter_files(ctx.workdir, (".py", ".go", ".rb", ".php", ".java")):
            return True, f"backend server file: {p.relative_to(ctx.workdir)}"
        for p in _iter_files(ctx.workdir, (".js", ".mjs", ".ts")):
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            if any(m in text for m in _SERVER_MARKERS):
                return True, f"backend server file: {p.relative_to(ctx.workdir)}"
        return False, "no backend server file — build is frontend/static only"

    return check


def backend_defines_route(route: str) -> Check:
    """The ``route`` path string appears in a server-language file."""

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        where = _first_containing(ctx.workdir, route, _SERVER_EXTS)
        ok = where is not None
        return ok, (
            f"route {route} defined in {where}"
            if ok
            else f"no backend file defines route {route}"
        )

    return check


def frontend_calls_route(route: str) -> Check:
    """The ``route`` path string appears in a client-facing file."""

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        where = _first_containing(ctx.workdir, route, _CLIENT_EXTS)
        ok = where is not None
        return ok, (
            f"route {route} referenced by {where}"
            if ok
            else f"no frontend file references route {route}"
        )

    return check


def route_wired(route: str) -> Check:
    """The route the frontend calls is one the backend defines — the wiring that
    makes the button actually DO something (weaknesses.md #3/#7). Coarse: `.js`
    counts on both sides, so a single-file Node app can satisfy both halves."""

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        be = _first_containing(ctx.workdir, route, _SERVER_EXTS)
        fe = _first_containing(ctx.workdir, route, _CLIENT_EXTS)
        if be is not None and fe is not None:
            return True, f"route {route} wired: {fe} -> {be}"
        missing = []
        if be is None:
            missing.append("no backend defines it")
        if fe is None:
            missing.append("no frontend calls it")
        return False, f"route {route} not wired ({'; '.join(missing)})"

    return check


def backend_reads_fields(fields) -> Check:
    """Every named field appears in some backend file — the server reads what the
    form sends (a form with fields the server ignores is a dead button)."""
    wanted = tuple(fields)

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        missing = [
            f for f in wanted if _first_containing(ctx.workdir, f, _SERVER_EXTS) is None
        ]
        ok = not missing
        return ok, (
            "backend reads fields " + ", ".join(wanted)
            if ok
            else "backend missing field(s): " + ", ".join(missing)
        )

    return check
