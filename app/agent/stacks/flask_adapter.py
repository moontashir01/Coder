"""The Flask stack, behind the protocol — delegation only, no rewritten logic.

Phase N0 of `docs/node-stack-plan.md`. Every method here forwards to the
function that already did the job before the seam existed: `scaffold_flask`,
`crud.models_source`, `impact.migration_block`, `projectspec.routes_from_source`
and the rest. That is the whole point of the phase, and it is what makes the
exit criterion checkable — the existing suite passes **unmodified**. A method
in this file that grows a branch of its own has quietly become a rewrite, and
the guarantee stops being provable.

The one thing that genuinely moved rather than being wrapped is
`write_data_layer` / `migration_note`: `core._write_data_layer` and
`core._apply_migrations` were methods on `AgentCore` that touched nothing but
the filesystem and `crud`/`impact`, so they live here now, byte-for-byte, and
`core.py` calls them. Nothing about *when* they run changed.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.agent import scaffold as _scaffold
from app.agent.crud import (
    api_context,
    apply_table_block,
    models_source,
    plaintext_password_writes,
    seed_source,
)
from app.agent.impact import (
    DB_FILE,
    apply_migration_block,
    migration_block,
    reinstate_routes,
    restore_page_routes,
    route_blocks,
)
from app.agent.projectspec import ProjectSpec, routes_from_source
from app.agent.pyimports import add_missing_imports, searchable_sql
from app.agent.verify import (
    fix_endpoint_names,
    form_method_mismatches,
    unresolved_endpoints,
)

logger = logging.getLogger(__name__)


class FlaskAdapter:
    """Python / Flask / Jinja2 / sqlite3 — the default, and today's behaviour."""

    key = "flask"
    label = "Python · Flask · Jinja2 · SQLite"
    display_name = "Flask"
    scaffold_summary = (
        "app.py, db.py, models.py, templates/, static/, requirements.txt, Procfile"
    )
    start_hint = "python app.py"
    seed_hint = "python seed.py"
    language = "python"
    backends = ("flask",)
    entry_file = "app.py"
    template_dir = "templates"
    template_ext = ".html"
    # How this stack spells "the id goes here" in a route path.
    route_param = "<id>"
    layout_file = "base.html"
    static_dir = "static"
    theme_file = "static/css/theme.css"
    home_template = "templates/index.html"
    db_module = DB_FILE
    source_globs = ("*.py",)
    # How a derived page is told to relate to the layout, and what a home-page
    # edit must preserve. One sentence each: they ride inside a per-file
    # instruction that already competes for `llm_num_ctx`.
    page_note = "Extends base.html and puts its markup in {% block content %}."
    home_edit_note = (
        "Rewrite only the body of `{% block content %}` — keep the "
        "`{% extends %}` and `{% import %}` lines and the title block."
    )

    guarantees = (
        "deterministic data layer (db.py / models.py / seed.py from the schema)",
        "schema migrations from the spec, never from the model",
        "url_for endpoint validation and near-miss repair (W2)",
        "missing-import repair via stdlib ast (pyimports)",
        "Jinja block-scoped editing, so an edit cannot delete the layout (W3)",
        "template dependency graph for impact analysis (W8)",
    )
    gaps = ()

    # -- scaffold ---------------------------------------------------------

    def scaffold(self, root: Path, name: str | None = None) -> list[str]:
        return _scaffold.scaffold_flask(root, name)

    def scaffold_files(self) -> set[str]:
        return _scaffold.scaffold_files()

    def frozen_files(self) -> set[str]:
        return _scaffold.frozen_files()

    def is_frozen(self, filename: str) -> bool:
        return _scaffold.is_frozen(filename)

    def write_theme(self, root: Path, css: str) -> bool:
        return _scaffold.write_theme(root, css, self.theme_file)

    def theme_exists(self, root: Path) -> bool:
        return (Path(root) / self.theme_file).is_file()

    # -- data layer -------------------------------------------------------

    def write_data_layer(self, root: Path, spec: ProjectSpec) -> tuple[set[str], str]:
        """Write `db.py`'s tables, `models.py` and `seed.py` from the entities.

        Phase 4a/4d, moved here unchanged. These three files contain no
        decisions: the table IS the fields, the query IS the table, the demo row
        IS the field types. Leaving them to a 7B produced, on live builds, an
        `init_db()` with no `CREATE TABLE` and an `app.py` calling
        `models.get_all_posts` against a `models.py` defining only `add_post`.

        Returns ``(files it now owns, the API description for the prompt)``. The
        second half is not optional — taking the data layer away from the model
        is only safe if the model is TOLD what replaced it.
        """
        root = Path(root)
        owned: set[str] = set()

        db_path = root / self.db_module
        if db_path.is_file():
            try:
                source = db_path.read_text(encoding="utf-8", errors="replace")
                updated, changed = apply_table_block(source, spec)
                if changed and self.write_source_if_valid(db_path, updated):
                    owned.add(self.db_module)
            except Exception:
                logger.warning("could not write the schema into db.py", exc_info=True)

        for rel, render in (("models.py", models_source), ("seed.py", seed_source)):
            path = root / rel
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(render(spec), encoding="utf-8", newline="\n")
                owned.add(rel)
            except Exception:
                logger.warning("could not write %s", rel, exc_info=True)
        return owned, api_context(spec)

    def migration_note(self, root: Path, spec: ProjectSpec, since: int) -> str:
        """Put the new `ensure_column` calls into db.py. Moved unchanged.

        ``spec`` is already stamped with the delta, so `migrations(since=…)`
        names exactly the fields this turn added. Deterministic by design: the
        migration is exactly derivable from which revision each field arrived
        in, so generating it would add risk without adding information. A db.py
        we cannot recognise is left alone and REPORTED rather than half-edited.
        """
        db_path = Path(root) / self.db_module
        if not db_path.is_file():
            return ""
        block = migration_block(spec, since=since)
        if not block:
            return ""
        try:
            source = db_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""
        updated, changed = apply_migration_block(source, block)
        if not changed or not self.write_source_if_valid(db_path, updated):
            return (
                "may not meet: could not place the schema migration in db.py — "
                "add it by hand: " + "; ".join(spec.migrations(since=since))
            )
        calls = spec.migrations(since=since)
        return (
            f"Wrote {len(calls)} schema migration(s) into `db.py` from the project "
            "spec — existing rows are kept, not recreated."
        )

    # -- running it -------------------------------------------------------

    def readiness(self, root: Path) -> str:
        """Always "" — sqlite has no daemon and Flask needs no install step.

        `Stack.runnable` (is Flask importable) already answers the only question
        this stack has, and it is checked by the caller before this. Returning a
        reason here would gate a check that has always run.
        """
        return ""

    def autosetup(self, root: Path, log=None) -> list[str]:
        """Nothing to do — and that is the point of this stack.

        sqlite is a file, and the generated app's one dependency is installed in
        the venv Coder itself runs from (`run_command` uses `sys.executable` for
        exactly that reason). So a Flask project is runnable the moment it is
        written, and `/run` on this stack is unchanged by the auto-setup work.

        Returns the empty list, never a "nothing needed" line: a step that did
        not happen must not be printed as one that did.
        """
        return []

    def table_columns(self, root: Path) -> dict[str, set[str]] | None:
        """What the database REALLY has: `{table: {column, ...}}`, or None.

        Phase N6. "A `CREATE TABLE` in a file nobody executes proves nothing" is
        the rule the eval suite's schema checks are built on, and answering it
        means asking the database — which is a per-stack question, so it lives
        here rather than in `evals/`.

        None means *could not read*, which is not the same answer as "no tables"
        and must not be reported as one.
        """
        import sqlite3

        files = sorted(Path(root).glob("*.db"))
        if not files:
            return None
        found: dict[str, set[str]] = {}
        for path in files:
            try:
                conn = sqlite3.connect(path)
            except Exception:
                continue
            try:
                tables = [
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                ]
                for table in tables:
                    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                    if cols:
                        found.setdefault(table, set()).update(cols)
            except Exception:
                logger.debug("could not read %s", path.name, exc_info=True)
            finally:
                conn.close()
        return found

    def run_command(self, entry: str | Path) -> list[str]:
        """How to start the generated app.

        `sys.executable`, not `python`: the generated project's dependency is
        declared in ITS requirements.txt but installed in the venv Coder runs
        from, and a bare `python` on PATH is routinely a different interpreter.
        """
        import sys

        return [sys.executable, Path(entry).name]

    def seed_command(self) -> list[str] | None:
        """`seed.py`, run once after a build.

        A deliberate exception to "never execute generated code": seed.py and
        db.py's schema are written by `crud.py`, not by the model.
        """
        import sys

        return [sys.executable, "seed.py"]

    # -- reading what was written ----------------------------------------

    def routes_from_source(self, source: str) -> list[tuple[str, str, str, str]]:
        return routes_from_source(source)

    def check_links(self, text: str, routes) -> tuple[str, list, list]:
        """Phase W2, unchanged: a Jinja page names a route by its VIEW.

        Returns ``(text, fixes, problems)``. A near miss of a real view is a
        naming slip and is repointed; anything else is reported, because
        inventing the route would be generation.
        """
        known = {view for _m, _p, view, _t in routes}
        fixed, fixes = fix_endpoint_names(text, known)
        problems = [
            f"url_for('{n}') has no such route"
            for n in unresolved_endpoints(fixed, known)
        ]
        return fixed, fixes, problems + form_method_mismatches(fixed, routes)

    def source_is_valid(self, filename: str, source: str) -> bool:
        """Does this file still parse? Only `.py` can be answered in-process.

        The deterministic passes edit files by hand, outside
        `_verify_and_repair`, so nothing else would notice if one produced
        source that does not parse. Anything that is not Python is left to the
        ordinary check path rather than being failed on a guess.
        """
        if not str(filename).lower().endswith(".py"):
            return True
        try:
            compile(source, str(filename), "exec")
            return True
        except SyntaxError:
            return False

    def write_source_if_valid(self, path: Path, source: str) -> bool:
        """Write generated code only if it still parses. Returns success."""
        path = Path(path)
        if not self.source_is_valid(path.name, source):
            logger.warning("declined to write invalid source to %s", path.name)
            return False
        try:
            path.write_text(source, encoding="utf-8", newline="\n")
            return True
        except Exception:
            logger.warning("could not write %s", path.name, exc_info=True)
            return False

    def restore_entry_route(self, source: str) -> tuple[str, bool]:
        return _scaffold.restore_index_route(source)

    def restore_routes(self, source: str, missing) -> tuple[str, list[str]]:
        return restore_page_routes(source, missing)

    def restore_boot_block(self, source: str) -> tuple[str, bool]:
        return _scaffold.restore_run_block(source)

    def sql_literals(self, source: str) -> list[str]:
        """Python's answer, unchanged — string literals of a module that
        parses, the whole raw text of one that does not."""
        return searchable_sql(source)

    def render_locals(self, entry_source: str) -> dict[str, set[str]]:
        """Not needed here, and that is a property of Jinja rather than a gap.

        An undefined name in a Jinja template renders as empty; in EJS it is a
        ReferenceError and the page 500s. So the Node adapter has to check this
        and Flask genuinely does not.
        """
        return {}

    def repair_view_locals(
        self, text: str, provided: set[str]
    ) -> tuple[str, list[str], list[str]]:
        return text, [], []

    def repair_module_calls(self, source: str, root: Path) -> tuple[str, list[str]]:
        """Nothing here yet — `_check_cross_module_calls` REPORTS these on both
        stacks, and the Node repair is one scaffold invariant (`db.initDb`), not
        a general fixer. Inventing a Python equivalent without a measured
        failure behind it is how a repair pass becomes churn."""
        return source, []

    def repair_runtime_names(
        self, filename: str, source: str, root: Path
    ) -> tuple[str, list[str], list[str]]:
        """Delegation, unchanged: `add_missing_imports` + the password check.

        This is exactly what `core._repair_missing_imports` inlined before the
        Node half existed — moved behind the seam so the two stacks answer the
        same question rather than only one of them being asked it.
        """
        if Path(filename).suffix.lower() != ".py":
            return source, [], []

        sibling_sources: dict[str, str] = {}
        for name in ("db", "models", "seed"):
            sibling = root / f"{name}.py"
            if sibling.is_file() and sibling.name != Path(filename).name:
                try:
                    sibling_sources[name] = sibling.read_text(
                        encoding="utf-8", errors="replace"
                    )
                except OSError:
                    logger.debug("import repair: could not read %s.py", name)
        fixed, added, unresolved = add_missing_imports(
            source, frozenset(sibling_sources)
        )

        reports: list[str] = []
        if unresolved:
            reports.append(
                "uses undefined name(s) at runtime — " + ", ".join(unresolved[:6])
            )
        try:
            leaks = plaintext_password_writes(fixed)
        except Exception:
            logger.debug("password check failed for %s", filename, exc_info=True)
            leaks = []
        if leaks:
            reports.append(
                "stores a password without hashing it — "
                + "; ".join(leaks[:3])
                + " (use werkzeug.security.generate_password_hash)"
            )
        return fixed, added, reports

    def order_routes(self, source: str) -> tuple[str, list[str], list[str]]:
        """Nothing to do, and that is a property of Werkzeug, not an omission.

        Flask's URL map ranks rules by specificity, so `/items/<id>` written
        above `/items/new` still loses to it. Express matches in registration
        order and does not — see `NodeAdapter.order_routes`.
        """
        return source, [], []

    def route_blocks(self, source: str) -> dict[tuple[str, str], str]:
        return route_blocks(source)

    def reinstate_routes(
        self, source: str, blocks: dict[tuple[str, str], str]
    ) -> tuple[str, list[str]]:
        return reinstate_routes(source, blocks)

    def orphan_templates(self, root: Path) -> list[str]:
        return _scaffold.templates_without_inheritance(root)

    def convert_template(self, source: str) -> tuple[str, bool]:
        return _scaffold.convert_to_child_template(source)

    def build_template_graph(self, root: Path):
        """The project's Jinja edges (Phase W8), unchanged."""
        from app.agent.templatedeps import build_graph

        return build_graph(root, self.entry_file)

    def template_edit_region(self, filename: str, text: str):
        return _scaffold.template_edit_region(filename, text)

    # -- prompt blocks ----------------------------------------------------

    def schema_types(self) -> str:
        """The column types the SCHEMA call may use on this stack.

        It used to be a fixed "Storage is SQLite" paragraph in
        `prompts/schema.md`, which meant a Node build — whose database is
        PostgreSQL — was told to flatten `BOOLEAN` to `INTEGER` and every
        timestamp to `TEXT`. That is right here and wrong there, and the cost
        showed on the OpenBazaar PRD: an auction's `auction_end_time` became a
        string, so `WHERE auction_end_time > NOW()` could not be written.
        """
        return (
            "## Column types — use only these\n"
            "`INTEGER`, `TEXT`, `REAL`, `BLOB`, `NUMERIC`.\n\n"
            "Storage is SQLite, which has no boolean and no date type: a "
            "yes/no is `INTEGER`, a timestamp is `TEXT`. The primary key of "
            'every table is `{"name": "id", "type": "INTEGER", '
            '"pk": true}` — it autoincrements, so nothing supplies it.'
        )

    def blueprint_layout(self) -> str:
        """The filenames the PLANNING call must use (was hard-coded in the prompt).

        It lived in `prompts/blueprint.md` as "On the Flask stack, use this
        exact layout", which meant the Node stack was planned with no layout at
        all — the planner invented `app.py`/`templates/*.html` for an Express
        project and every later pass had to guess what it meant. The rules moved
        here unchanged; what changed is that each stack now has some.
        """
        return (
            "## File layout — use these names and no others\n"
            "| File | Holds |\n"
            "| --- | --- |\n"
            "| `app.py` | routes only — one `@app.route` per URL, no SQL |\n"
            "| `db.py` | `get_db()`, `init_db()`, `ensure_column()` |\n"
            "| `models.py` | one query helper per operation, `?` parameters only |\n"
            "| `seed.py` | a few demo rows per table |\n"
            "| `templates/base.html` | the nav and page shell — the ONLY place "
            "nav exists |\n"
            '| `templates/index.html` | the home page — `"action": "edit"`, it '
            "already exists |\n"
            "| `templates/<page>.html` | one per page, each "
            '`{% extends "base.html" %}` |\n'
            "| `static/css/style.css` | the one stylesheet |\n"
            "| `static/js/app.js` | optional enhancement only |\n\n"
            "Rules that follow from it:\n"
            "- A page template contains ONLY `{% extends %}` plus its blocks — "
            "never a full `<html>` document, never its own copy of the nav.\n"
            '- Prefer a real `<form method="post" action="/route">` posting to '
            "a Flask route over `fetch()`. It works with JavaScript disabled, "
            'which is what makes "the button does nothing" impossible rather '
            "than merely unlikely.\n"
            "- Routes call helpers in `models.py`; they never write SQL inline.\n"
            "- Do NOT plan `requirements.txt`, `Procfile` or `.gitignore` — they "
            "are already written for you.\n"
            "- **Plan the home page.** `templates/index.html` exists but holds "
            'placeholder text, so give it `"action": "edit"` and an instruction '
            "describing what this site's front door shows and which pages it "
            "links to. A build that leaves it alone ships a site whose first "
            "page says it was scaffolded."
        )

    def ui_context(self) -> str:
        return _scaffold.ui_context()

    def scaffold_context(self, written) -> str:
        return _scaffold.scaffold_context(written)


FLASK = FlaskAdapter()
