"""The stack seam: one protocol, two implementations, no `if flask:` in core.py.

Phase N0 of `docs/node-stack-plan.md`. Before this module, "the stack" was not a
thing the code could name — it was `workdir / "app.py"` written out at four call
sites in `core.py`, `workdir.glob("*.py")` at a fifth, `[sys.executable,
"seed.py"]` at a sixth, and six modules that are Flask/Python to their core. A
second stack under that shape is a fork, not a feature.

So the promise this seam keeps is the testable one, not the comfortable one:

    **Flask code changes. Flask behaviour does not.**

Every existing test passes unmodified, because `FlaskAdapter` does not
reimplement anything — every method delegates to the function that already did
the job. A method here that contains logic instead of a delegation is a bug in
this phase.

Three rules the callers depend on:

  * **`get_adapter()` answers for every input, including nonsense.** An unknown
    key, `None`, and `""` all return the Flask adapter. Specs written before
    this module have no stack key and must keep working; a KeyError here would
    turn a missing field in an old `.coder/project.json` into a dead turn.
  * **The spec outranks the setting** (`resolve_key`). Opening a Node project
    with `web_stack` left at `flask` and letting the amendment path win would
    send `_amend_project` to write Python `ensure_column` calls into a `db.py`
    that does not exist — a silent, total failure on turn 2. `spec.language` /
    `spec.backend` already hold the answer and already persist to disk.
  * **An adapter states its gaps.** `guarantees` / `gaps` are read by `/stack`
    and printed verbatim. Flask has W2 endpoint validation, W3 block editing,
    migrations and `add_missing_imports`; Node does not, and a menu that hides
    that is how someone picks the weaker stack for a demo by accident.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.agent.projectspec import ProjectSpec
    from app.agent.scaffold import BlockRegion

# The answer to every unrecognised, empty or missing key. Flask stays this
# fallback deliberately, and it is NOT the same question as
# `settings.web_stack` — which now defaults to "node" (2026-08-04, by request).
# Two reasons to keep them apart:
#   * A spec written before the stack seam existed has no stack key at all, and
#     resolving those to Node would reinterpret every existing Flask project as
#     an Express one on its next turn — writing `ensure_column` calls into a
#     `db.js` that does not exist, which is the exact failure `resolve_key`'s
#     precedence exists to prevent.
#   * Flask has the deeper guarantees (see `gaps` on the Node adapter), so an
#     ACCIDENT — a typo, a `None` — should still land on the better-verified
#     path. A deliberate choice is what `settings.web_stack` expresses.
DEFAULT_KEY = "flask"


@runtime_checkable
class StackAdapter(Protocol):
    """Everything `core.py` needs to know that differs between two stacks.

    Wider than the sketch in `docs/node-stack-plan.md` in three places, each
    because the plan's single method mapped onto machinery that must stay in
    `core.py`:

      * `restore_invariants` is split into `restore_entry_route` /
        `orphan_templates` / `convert_template` / `source_is_valid`. The
        orchestration is async and writes through `executor.execute` (approval
        gate, backup, `/undo`); only the decisions are stack-specific, so only
        the decisions moved.
      * `write_migrations` is `migration_note`, which returns the note
        `_apply_migrations` used to build itself.
      * `template_edit_region` is here because Phase W3's block editing is
        Jinja-shaped. Node returns None, which is the existing "use the
        whole-file path" answer, not a new behaviour.
    """

    key: str  # "flask" | "node" — what a spec/setting names
    label: str  # one line for `/stack`
    display_name: str  # "Flask" | "Express" — the framework, for prose
    scaffold_summary: str  # the files the scaffold writes, for the answer line
    start_hint: str  # the command a human types to run the generated app
    seed_hint: str  # ditto for the seed script, for the failure line
    language: str  # "python" | "node" — matches runtime_probe.Stack.language
    backends: tuple[str, ...]  # Stack.backend values that mean this adapter
    entry_file: str  # "app.py" | "server.js"
    template_dir: str  # "templates" | "views"
    template_ext: str  # ".html" | ".ejs"
    route_param: str  # "<id>" | ":id" — how a route path spells its id segment
    layout_file: str  # "base.html" | "layout.ejs" — the shell every page uses
    static_dir: str  # "static" | "public"
    theme_file: str  # relative path of the file `write_theme` owns
    home_template: str  # the template the `/` route renders
    page_note: str  # one line telling a derived page how to meet the layout
    home_edit_note: str  # ...and what a home-page edit must not destroy
    db_module: str  # "db.py" | "db.js"
    source_globs: tuple[str, ...]  # what `_check_cross_module_calls` reads
    guarantees: tuple[str, ...]  # checks this stack really gets
    gaps: tuple[str, ...]  # checks it does NOT get — printed, never hidden

    # -- scaffold ---------------------------------------------------------
    def scaffold(self, root: Path, name: str | None = None) -> list[str]: ...
    def scaffold_files(self) -> set[str]: ...
    def frozen_files(self) -> set[str]: ...
    def is_frozen(self, filename: str) -> bool: ...
    def write_theme(self, root: Path, css: str) -> bool: ...
    def theme_exists(self, root: Path) -> bool: ...

    # -- data layer -------------------------------------------------------
    def write_data_layer(
        self, root: Path, spec: "ProjectSpec"
    ) -> tuple[set[str], str]: ...
    def migration_note(self, root: Path, spec: "ProjectSpec", since: int) -> str: ...

    # -- running it -------------------------------------------------------
    def readiness(self, root: Path) -> str: ...
    def autosetup(self, root: Path, log=None) -> list[str]: ...
    def table_columns(self, root: Path) -> dict[str, set[str]] | None: ...
    def run_command(self, entry: str | Path) -> list[str]: ...
    def seed_command(self) -> list[str] | None: ...
    def write_source_if_valid(self, path: Path, source: str) -> bool: ...

    # -- reading what was written ----------------------------------------
    def routes_from_source(self, source: str) -> list[tuple[str, str, str, str]]: ...
    def source_is_valid(self, filename: str, source: str) -> bool: ...
    def restore_entry_route(self, source: str) -> tuple[str, bool]: ...
    def restore_routes(self, source: str, missing) -> tuple[str, list[str]]: ...
    def restore_boot_block(self, source: str) -> tuple[str, bool]: ...
    def order_routes(self, source: str) -> tuple[str, list[str], list[str]]: ...
    def repair_module_calls(self, source: str, root: Path) -> tuple[str, list[str]]: ...
    def repair_runtime_names(
        self, filename: str, source: str, root: Path
    ) -> tuple[str, list[str], list[str]]: ...
    def sql_literals(self, source: str) -> list[str]: ...
    def render_locals(self, entry_source: str) -> dict[str, set[str]]: ...
    def repair_view_locals(
        self, text: str, provided: set[str]
    ) -> tuple[str, list[str], list[str]]: ...
    def route_blocks(self, source: str) -> dict[tuple[str, str], str]: ...
    def reinstate_routes(
        self, source: str, blocks: dict[tuple[str, str], str]
    ) -> tuple[str, list[str]]: ...
    def strip_layout_include(self, source: str) -> tuple[str, bool]: ...
    def repair_model_calls(
        self, source: str, root: Path
    ) -> tuple[str, list[str]]: ...
    def call_arity_mismatches(self, root: Path) -> list[str]: ...
    def normalize_render_names(
        self, source: str, root: Path
    ) -> tuple[str, list[str]]: ...
    def restore_data_layer_api(self, root: Path, spec: "ProjectSpec") -> list[str]: ...
    def wire_view_route(
        self, source: str, path: str, stem: str, locals_js: str = ""
    ) -> tuple[str, bool]: ...
    def orphan_templates(self, root: Path) -> list[str]: ...
    def convert_template(self, source: str) -> tuple[str, bool]: ...
    def route_edit_region(
        self, filename: str, text: str, user_message: str
    ) -> "BlockRegion | None": ...
    def template_edit_region(
        self, filename: str, text: str
    ) -> "BlockRegion | None": ...

    # -- prompt blocks ----------------------------------------------------
    def ui_context(self) -> str: ...
    def scaffold_context(self, written) -> str: ...
    def blueprint_layout(self) -> str: ...
    def schema_types(self) -> str: ...


@lru_cache(maxsize=1)
def _registry() -> dict[str, StackAdapter]:
    """The adapters, built lazily and once.

    Imported inside the function, not at module scope: `flask_adapter` pulls in
    `scaffold` -> `blueprint`, and `app.agent.core` imports this module, so a
    top-level import here would close a cycle. Lazy also keeps this module free
    of import-time side effects, which `test_no_import_side_effects.py` guards.
    """
    from app.agent.stacks.flask_adapter import FLASK
    from app.agent.stacks.node_adapter import NODE

    return {FLASK.key: FLASK, NODE.key: NODE}


def stack_keys() -> tuple[str, ...]:
    """Every stack key, default first."""
    keys = list(_registry())
    keys.sort(key=lambda k: (k != DEFAULT_KEY, k))
    return tuple(keys)


def get_adapter(key: str | None) -> StackAdapter:
    """The adapter for ``key``, falling back to Flask for anything unknown.

    Total by design — see the module docstring. `""`, `None`, `"auto"` and a
    typo all land on the default rather than raising.
    """
    registry = _registry()
    return registry.get(str(key or "").strip().lower(), registry[DEFAULT_KEY])


def key_for_stack(language: str | None, backend: str | None) -> str:
    """The adapter key a persisted `(language, backend)` pair means.

    Derived rather than stored, so a `.coder/project.json` written before this
    module resolves correctly instead of needing a migration. `language` is
    checked first: `runtime_probe._node()` reports `backend="stdlib"` when the
    network is off, which collides with the *Python* stdlib stack, and only the
    language tells them apart.
    """
    lang = str(language or "").strip().lower()
    back = str(backend or "").strip().lower()
    for adapter in _registry().values():
        if adapter.key == DEFAULT_KEY:
            continue
        if lang == adapter.language or back in adapter.backends:
            return adapter.key
    return DEFAULT_KEY


def resolve_key(spec=None, default: str = "") -> str:
    """Which ADAPTER this turn uses: **project memory beats session default**.

    Precedence: `spec.backend`/`spec.language` -> `settings.web_stack` -> flask.

    The load-bearing rule of Phase N1. Reading the setting first would send an
    amendment to a Node project down the Flask path — Python `ensure_column`
    calls written into a `db.py` that does not exist, on turn 2, silently. The
    spec is what travels with the folder, so the spec decides.

    Returns an adapter key and nothing else, so `web_stack="stdlib"` (or
    `"fastapi"`, or `"auto"`) resolves to Flask — which is correct: those are
    Python stacks, and the Flask adapter's scaffold gate already declines to
    copy anything unless the blueprint really chose Flask. Use `probe_prefer`
    when you need the string `runtime_probe.detect_stack` takes; conflating the
    two silently turns a stdlib build into a Flask one.
    """
    if spec is not None:
        language = getattr(spec, "language", "")
        backend = getattr(spec, "backend", "")
        if language or backend:
            return key_for_stack(language, backend)
    key = str(default or "").strip().lower()
    return key if key in _registry() else DEFAULT_KEY


# `Stack.backend` values `detect_stack(prefer=…)` does not accept under that
# name. "express" is what a Node build persists; "node" is what selects it.
_PROBE_ALIASES = {"express": "node"}
# What `detect_stack` understands. Anything else falls through to auto there,
# so passing a spec's backend blind would silently re-probe.
_PROBE_KEYS = frozenset({"flask", "fastapi", "node", "stdlib", "none", "auto"})


def probe_prefer(spec=None, default: str = "") -> str:
    """The `prefer=` string for `runtime_probe.detect_stack`, spec first.

    Same precedence rule as `resolve_key`, different vocabulary: this one must
    preserve `stdlib` / `fastapi` / `auto` / `none`, which name real stacks that
    have no adapter of their own. `resolve_key` collapses those to Flask
    because that is the right *adapter*; collapsing them here would rebuild a
    stdlib project's blueprint as a Flask one.
    """
    if spec is not None:
        language = str(getattr(spec, "language", "") or "").strip().lower()
        backend = str(getattr(spec, "backend", "") or "").strip().lower()
        if language == "node":
            return "node"
        candidate = _PROBE_ALIASES.get(backend, backend)
        if candidate in _PROBE_KEYS:
            return candidate
    return str(default or "").strip().lower() or DEFAULT_KEY


def describe_stacks() -> list[dict]:
    """Rows for the `/stack` menu: key, label, guarantees and gaps.

    `gaps` is not decoration. Flask has endpoint validation, Jinja block
    editing, deterministic migrations and import repair; Node has none of them
    yet. A menu that lists two stacks as if they were equals is how a demo gets
    built on the weaker one by accident.
    """
    return [
        {
            "key": a.key,
            "label": a.label,
            "language": a.language,
            "entry": a.entry_file,
            "guarantees": list(a.guarantees),
            "gaps": list(a.gaps),
        }
        for a in (get_adapter(k) for k in stack_keys())
    ]
