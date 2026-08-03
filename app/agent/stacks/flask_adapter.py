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
from app.agent.crud import api_context, apply_table_block, models_source, seed_source
from app.agent.impact import (
    DB_FILE,
    apply_migration_block,
    migration_block,
    restore_page_routes,
)
from app.agent.projectspec import ProjectSpec, routes_from_source
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

    def ui_context(self) -> str:
        return _scaffold.ui_context()

    def scaffold_context(self, written) -> str:
        return _scaffold.scaffold_context(written)


FLASK = FlaskAdapter()
