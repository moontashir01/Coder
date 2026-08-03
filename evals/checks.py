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


# ---------------------------------------------------------------------------
# Phase E (docs/always-fullstack-plan.md) — measure what phases A–D promised.
#
# The earlier checks name the table and the route they expect, which only works
# when the eval author already knows the schema. These derive both from the
# project's own spec, so one check covers any request shape — including the ones
# no noun list anticipates, which is the point of Phase B.
# ---------------------------------------------------------------------------


def is_full_stack_app() -> Check:
    """A Flask app with routes and templates — not a page of static HTML.

    Phases A and B in one assertion. Before them a request whose wording missed
    the gate's noun list, or a machine where Flask happened not to be importable,
    both produced a plausible-looking pile of HTML with no server and no
    database — and every file-level check still passed.
    """

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        workdir = Path(ctx.workdir)
        entry = workdir / "app.py"
        if not entry.is_file():
            html = sorted(p.name for p in workdir.rglob("*.html"))[:4]
            return False, (
                "no app.py — this build is static"
                + (f" ({', '.join(html)})" if html else " and empty")
            )
        try:
            source = entry.read_text(encoding="utf-8", errors="replace")
        except Exception as e:  # pragma: no cover - unreadable file
            return False, f"could not read app.py: {e}"

        missing = []
        if "flask" not in source.lower():
            missing.append("app.py does not import flask")
        if "@app.route" not in source:
            missing.append("app.py defines no route")
        if not (workdir / "templates").is_dir():
            missing.append("no templates/ directory")
        if missing:
            return False, "; ".join(missing)
        return True, "flask app with routes and templates"

    return check


def every_entity_has_a_table() -> Check:
    """Every table the project DECLARES exists in the database, with its columns.

    The dynamic form of `db_has_column`: it asks the spec what was promised and
    the database whether it was delivered, so it works for a request whose schema
    the eval author could not have known in advance.
    """

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        import sqlite3

        spec = _spec(ctx)
        if spec is None or not spec.entities:
            return False, "no project spec with entities"
        dbs = sorted(Path(ctx.workdir).glob("*.db"))
        if not dbs:
            return False, f"no .db file for {len(spec.entities)} declared table(s)"

        found: dict[str, set[str]] = {}
        for path in dbs:
            conn = sqlite3.connect(path)
            try:
                for entity in spec.entities:
                    try:
                        cols = {
                            r[1]
                            for r in conn.execute(f"PRAGMA table_info({entity.table})")
                        }
                    except Exception:
                        cols = set()
                    if cols:
                        found.setdefault(entity.table, set()).update(cols)
            finally:
                conn.close()

        problems = []
        for entity in spec.entities:
            cols = found.get(entity.table)
            if not cols:
                problems.append(f"{entity.table}: table missing")
                continue
            absent = [f.name for f in entity.fields if f.name not in cols]
            if absent:
                problems.append(f"{entity.table}: no column {', '.join(absent)}")
        if problems:
            return False, "; ".join(problems)
        return True, f"all {len(spec.entities)} table(s) exist: {', '.join(found)}"

    return check


# ---------------------------------------------------------------------------
# Does it LOOK right (Phase W10, docs/web-quality-plan.md)
#
# Everything above this line measures whether the app WORKS. None of it renders
# a page, so a table 900px wide inside a 390px viewport, a button wired to
# nothing and a console full of ReferenceErrors all scored a clean pass.
#
# These checks name no selector the task author had to guess: they read the
# project's own spec for its routes, drive the same `browser.py` the agent uses,
# and assert on measurements W5/W6 already define. A machine with no browser
# gets a FAILURE naming the install command, not a silent pass — a suite that
# scores 100% without having looked at anything is exactly what this phase
# exists to stop.
# ---------------------------------------------------------------------------

# One page's style fingerprint. Not a screenshot hash — a pixel diff fails when
# the seeded data changes and would be pure noise. What matters is whether the
# pages still share one design system: the same computed body typography and
# background, the vocabulary from `style.css`, and no page-local <style> block
# that has quietly opted out of the theme.
_STYLE_SCRIPT = """
() => {
  const body = getComputedStyle(document.body);
  const classes = new Set();
  for (const el of document.querySelectorAll('[class]')) {
    for (const c of String(el.className || '').trim().split(/\\s+/)) {
      if (c) classes.add(c);
    }
  }
  return {
    font_family: body.fontFamily,
    background: body.backgroundColor,
    color: body.color,
    local_styles: document.querySelectorAll('style').length,
    has_nav: !!document.querySelector('nav'),
    nav_links: Array.from(document.querySelectorAll('nav a')).map(
      (a) => (a.textContent || '').trim()
    ).slice(0, 12),
    classes: Array.from(classes).sort().slice(0, 60),
  };
}
"""

# Classes the shipped component sheet defines (Phase W1). A page built from the
# design system uses some of them; one that invented its own uses none.
_COMPONENT_CLASSES = {
    "card",
    "grid",
    "stack",
    "cluster",
    "table",
    "table-wrap",
    "field",
    "button",
    "badge",
    "empty",
    "alert",
    "page-header",
    "hero",
    "lede",
    "form-narrow",
    "breadcrumb",
    "pagination",
}


def _spec_routes(ctx: "CheckContext") -> list[str]:
    """Every GET page route the project remembers, always including `/`."""
    spec = _spec(ctx)
    routes: list[str] = ["/"]
    for page in getattr(spec, "pages", ()) or ():
        route = (page.route or "").strip()
        if route.startswith("/") and "<" not in route and route not in routes:
            routes.append(route)
    return routes


class _BrowserReport:
    """What one browser pass saw. Built once per task, shared by every check."""

    def __init__(self, audit=None, styles=None, error: str = "") -> None:
        self.audit = audit
        self.styles: dict[str, dict] = styles or {}
        self.error = error


def _browser_report(ctx: "CheckContext") -> _BrowserReport:
    """Start the app once, render every page once, and remember what was seen."""
    if isinstance(getattr(ctx, "browser", None), _BrowserReport):
        return ctx.browser  # type: ignore[return-value]

    from app.agent import browser as browser_layer
    from app.agent.apprunner import AppRunner
    from app.agent.browser import probe_pages
    from app.agent.pageaudit import audit_site, page_of, probe_urls

    report = _BrowserReport()
    if not browser_layer.available():
        report.error = browser_layer.install_hint()
    elif not (Path(ctx.workdir) / "app.py").is_file():
        report.error = "no app.py to run"
    else:
        runner = AppRunner()
        try:
            ok, message = runner.start(ctx.workdir)
            if not ok:
                report.error = f"app did not start: {message}"
            else:
                base = f"http://127.0.0.1:{runner._port}"
                routes = _spec_routes(ctx)
                report.audit = audit_site(base, routes)
                for probe in probe_pages(
                    probe_urls(base, routes),
                    widths=[1280],
                    scripts={"style": _STYLE_SCRIPT},
                ):
                    style = (probe.data or {}).get("style")
                    if probe.ok and isinstance(style, dict):
                        report.styles[page_of(probe.url)] = style
        except Exception as e:  # noqa: BLE001 — an eval never crashes the suite
            report.error = f"browser pass failed: {type(e).__name__}: {e}"
        finally:
            runner.stop()
    ctx.browser = report
    return report


def _audit_or_fail(ctx: "CheckContext"):
    """`(audit, failure_detail)` — the shared preamble of every W10 check."""
    report = _browser_report(ctx)
    if report.error:
        return None, report.error
    if report.audit is None or not report.audit.ran:
        return None, "the browser never opened a page"
    if not report.audit.observations:
        return None, "the browser opened, but no page loaded"
    return report.audit, ""


def no_horizontal_overflow() -> Check:
    """No page scrolls sideways — measured at every configured width.

    The single most common responsive bug and the one that embarrasses a demo
    on a phone. `browser_widths` includes 390px precisely so this can be asked.
    """

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        audit, failure = _audit_or_fail(ctx)
        if audit is None:
            return False, f"no_horizontal_overflow: {failure}"
        found = audit.of_kind("overflow")
        if found:
            return False, "; ".join(f.line() for f in found[:4])
        return True, (
            f"no sideways scroll on {len(audit.pages)} page(s) at "
            f"{', '.join(str(w) for w in audit.widths)}px"
        )

    return check


def no_console_errors() -> Check:
    """No uncaught JavaScript and no failed asset request, on any page."""

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        audit, failure = _audit_or_fail(ctx)
        if audit is None:
            return False, f"no_console_errors: {failure}"
        found = audit.of_kind("console", "network")
        if found:
            return False, "; ".join(f.line() for f in found[:4])
        return True, f"console clean on {len(audit.pages)} page(s)"

    return check


def every_control_does_something() -> Check:
    """Every button that was clicked changed something.

    W6's probe as an assertion, including its skips: a control that was not
    clicked (destructive, or a POST form the functional probe owns) is reported
    as skipped, never counted as a pass.
    """

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        audit, failure = _audit_or_fail(ctx)
        if audit is None:
            return False, f"every_control_does_something: {failure}"
        dead = [f for f in audit.of_kind("dead-control") if f.severity == "error"]
        if dead:
            return False, "; ".join(f.line() for f in dead[:4])
        detail = f"{audit.controls_clicked} control(s) clicked, all did something"
        if audit.controls_skipped:
            detail += f"; {len(audit.controls_skipped)} skipped"
        return True, detail

    return check


def contrast_ok() -> Check:
    """Every text node clears WCAG AA against its computed background."""

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        audit, failure = _audit_or_fail(ctx)
        if audit is None:
            return False, f"contrast_ok: {failure}"
        found = audit.of_kind("contrast")
        if found:
            return False, "; ".join(f.line() for f in found[:4])
        return True, f"contrast AA on {len(audit.pages)} page(s)"

    return check


def nav_on_every_page() -> Check:
    """Every page carries the SAME navigation — the base.html guarantee.

    `_repair_nav_consistency` fixes this for static builds and `base.html` makes
    it structural for Flask ones; this is the check that says whether either
    actually held.
    """

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        report = _browser_report(ctx)
        if report.error:
            return False, f"nav_on_every_page: {report.error}"
        if not report.styles:
            return False, "nav_on_every_page: no page could be inspected"
        missing = [page for page, s in report.styles.items() if not s.get("has_nav")]
        if missing:
            return False, "no <nav> on " + ", ".join(missing[:4])
        signatures = {
            page: tuple(s.get("nav_links") or ()) for page, s in report.styles.items()
        }
        distinct = set(signatures.values())
        if len(distinct) > 1:
            listed = "; ".join(
                f"{page}: {', '.join(links) or '(empty)'}"
                for page, links in list(signatures.items())[:3]
            )
            return False, f"the nav differs between pages — {listed}"
        return True, f"the same nav on all {len(signatures)} page(s)"

    return check


def style_stable_across_turns() -> Check:
    """Turn 3 did not restyle turn 1 — the visual sibling of
    `earlier_pages_still_work`, and the headline number for `web-quality-plan`.

    Compares the computed token values and the components in use, **not** a
    screenshot: a pixel diff fails whenever the seeded data changes, which would
    make it noise rather than a measurement. Three questions:

      * do all pages still compute the same body typography and background
        (i.e. is one theme still in force);
      * has any page opted out with its own `<style>` block;
      * is every page still built from the shipped component vocabulary.

    A page added on turn 3 that styled itself fails all three.
    """

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        report = _browser_report(ctx)
        if report.error:
            return False, f"style_stable_across_turns: {report.error}"
        if len(report.styles) < 2:
            return False, "style_stable_across_turns: fewer than two pages to compare"

        problems: list[str] = []
        fonts = {s.get("font_family") for s in report.styles.values()}
        backgrounds = {s.get("background") for s in report.styles.values()}
        if len(fonts) > 1:
            problems.append(f"{len(fonts)} different body fonts: {sorted(fonts)}")
        if len(backgrounds) > 1:
            problems.append(f"{len(backgrounds)} different page backgrounds")

        opted_out = [p for p, s in report.styles.items() if s.get("local_styles")]
        if opted_out:
            problems.append("page-local <style> on " + ", ".join(opted_out[:3]))

        no_components = [
            page
            for page, s in report.styles.items()
            if not (_COMPONENT_CLASSES & set(s.get("classes") or ()))
        ]
        if no_components:
            problems.append(
                "no shipped component class on " + ", ".join(no_components[:3])
            )

        if problems:
            return False, "; ".join(problems)
        return True, (
            f"all {len(report.styles)} page(s) share one theme and the shipped "
            "components"
        )

    return check


def _entity_routes(spec, entity) -> tuple[str, str]:
    """`(list route, create route)` for an entity.

    Read from the spec when it recorded them, falling back to the convention
    `derive_pages_from_entities` synthesizes — which is what the routes will be
    whenever the model did not name its own.
    """
    gets = [
        e.path
        for e in spec.endpoints
        if e.method == "GET" and e.entity == entity.name and "new" not in e.path
    ]
    posts = [
        e.path for e in spec.endpoints if e.method == "POST" and e.entity == entity.name
    ]
    return (
        gets[0] if gets else f"/{entity.table}",
        posts[0] if posts else f"/{entity.table}/new",
    )


def entities_are_usable(check_persistence: bool = True) -> Check:
    """Every declared table is browsable AND writable, in the running app.

    Phase C3's postcondition, measured: "every entity gets a list page, a create
    form and the routes behind them" is a claim about the finished app, and only
    running it can settle it. A four-table build that shipped pages for two used
    to pass everything else in this file.

    Starts the app ONCE and loops the entities inside it — a start per entity
    turns a four-table check into four server launches.
    """
    marker = "EvalProbe7f1e"

    def check(ctx: "CheckContext") -> tuple[bool, str]:
        from urllib.parse import urlencode

        from app.agent.apprunner import AppRunner
        from app.agent.smoke import _encode_multipart, _png_1x1, _request, server_error

        spec = _spec(ctx)
        if spec is None or not spec.entities:
            return False, "no project spec with entities"
        if not (Path(ctx.workdir) / "app.py").is_file():
            return False, "no app.py to run"

        runner = AppRunner()
        problems: list[str] = []
        passed: list[str] = []
        try:
            ok, message = runner.start(ctx.workdir)
            if not ok:
                return False, f"app did not start: {message}"
            port = runner._port

            for entity in spec.entities:
                list_route, form_route = _entity_routes(spec, entity)
                status, _ = _request(port, "GET", list_route)
                if not (status and 200 <= status < 400):
                    problems.append(f"{entity.table}: GET {list_route} -> {status}")
                    continue
                if not check_persistence:
                    passed.append(entity.table)
                    continue

                fields: dict = {}
                files: dict = {}
                for f in entity.fields:
                    if f.pk:
                        continue
                    if f.is_upload():
                        files[f.name] = (f"{f.name}.png", _png_1x1())
                    elif f.type in ("INTEGER", "REAL", "NUMERIC"):
                        # A foreign key or a price. Text here would fail the
                        # insert for a reason that is not the build's fault.
                        fields[f.name] = "7"
                    else:
                        fields[f.name] = f"{marker} {f.name}"
                if not fields and not files:
                    passed.append(f"{entity.table} (nothing writable)")
                    continue

                if files:
                    body, content_type = _encode_multipart(fields, files)
                else:
                    body = urlencode(fields).encode()
                    content_type = "application/x-www-form-urlencoded"
                status, text = _request(
                    port, "POST", form_route, body=body, content_type=content_type
                )
                if status is None:
                    problems.append(f"{entity.table}: POST {form_route} no response")
                    continue
                if status >= 400:
                    why = server_error(text) if status >= 500 else ""
                    problems.append(
                        f"{entity.table}: POST {form_route} -> {status}"
                        + (f" ({why})" if why else "")
                    )
                    continue
                _, listing = _request(port, "GET", list_route)
                if marker in (listing or ""):
                    passed.append(entity.table)
                else:
                    problems.append(
                        f"{entity.table}: posted, but the value never appeared "
                        f"on {list_route}"
                    )
        finally:
            runner.stop()

        if problems:
            return False, "; ".join(problems)
        return True, f"usable: {', '.join(passed)}"

    return check
