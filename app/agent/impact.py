"""Impact analysis — which EXISTING files a change breaks, computed by rule.

This is the heart of "update the past files so they still run with the new
ones", and the reason `SpecDelta` deliberately does not name the files to edit.
"What else does this break?" is precisely the question a 7B model answers worst:
it will confidently list `app.py` and stop, leaving `models.py` inserting a
column list that no longer matches the table and `seed.py` writing rows the
storefront cannot render.

So the model is asked only what *changed*; this module derives what that change
*touches*, from the spec, with no LLM call at all. Adding a field to `product`
means `db.py` (a migration), `models.py` (the INSERT/UPDATE column lists),
`seed.py` (the new column in the demo rows), every template whose `reads`
include that entity, and every form template that writes it.

Each rule yields an `Edit(filename, reason)`. The reason is threaded into that
file's edit instruction, so the model is told *precisely* what to change and
why — never handed the user's whole request again and asked to work it out. A
narrow instruction is also what keeps `_surgical_edit` surgical.

Pure and offline (design rule 2): the caller supplies the set of files that
exist, so nothing here touches the filesystem and it unit-tests completely.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.agent.projectspec import ProjectSpec, SpecDelta

# The canonical layout the scaffold guarantees (docs/fullstack-web-plan.md §1).
DB_FILE = "db.py"
MODELS_FILE = "models.py"
SEED_FILE = "seed.py"
APP_FILE = "app.py"
BASE_TEMPLATE = "templates/base.html"

# How many existing files one amendment will edit. An amendment that claims to
# touch twenty files is a runaway, not a change.
MAX_EDITS = 12


@dataclass(frozen=True)
class Edit:
    """One existing file that must change, and the reason it must."""

    filename: str
    reason: str


def _add(
    out: list[Edit], seen: dict[str, list[str]], filename: str, reason: str
) -> None:
    """Record one reason as one edit, skipping exact repeats.

    Reasons for the same file are deliberately NOT merged into a single edit.
    Measured on the live two-turn demo: `app.py` was handed three reasons at
    once — "read image off the request", "define POST /admin/products", "add the
    view function" — and the model did only the first, so the new route was
    silently never written and the coverage check had to report it. One surgical
    edit gets one thing done. Separate edits are safe because `_file_op_flow`
    re-reads the file each time, so they compose rather than collide.
    """
    reasons = seen.setdefault(filename, [])
    if reason in reasons:
        return
    reasons.append(reason)
    out.append(Edit(filename, reason))


def impacted_files(
    spec: ProjectSpec, delta: SpecDelta, existing: set[str]
) -> list[Edit]:
    """The existing files this delta breaks unless they are updated too.

    ``existing`` is the set of repo-relative paths actually on disk; a file that
    isn't there is a *new* file and belongs to the create path, not here.
    Returns at most `MAX_EDITS` edits, each with a merged reason.
    """
    out: list[Edit] = []
    seen: dict[str, list[str]] = {}
    have = {f.replace("\\", "/") for f in existing}

    def present(name: str) -> bool:
        return name in have

    # --- new field on an existing entity ---------------------------------
    for entity_name, fld in delta.add_fields:
        entity = spec.entity(entity_name)
        table = entity.table if entity else entity_name
        col = f"{table}.{fld.name}"

        if present(DB_FILE):
            _add(out, seen, DB_FILE, f"add the {col} column migration")
        if present(MODELS_FILE):
            _add(
                out,
                seen,
                MODELS_FILE,
                f"include {fld.name} in the SELECT/INSERT/UPDATE column lists for {table}",
            )
        if present(SEED_FILE):
            _add(out, seen, SEED_FILE, f"give the demo rows a {fld.name} value")

        for page in spec.pages:
            if entity and entity.name in page.reads and present(page.template):
                _add(
                    out,
                    seen,
                    page.template,
                    f"show {fld.name} for each {entity.name}",
                )
        for endpoint in spec.endpoints:
            if not entity or endpoint.entity != entity.name:
                continue
            if endpoint.method in ("POST", "PUT", "PATCH") and present(
                endpoint.template
            ):
                _add(
                    out,
                    seen,
                    endpoint.template,
                    f"add a form input named {fld.name}",
                )
            if present(APP_FILE):
                _add(
                    out,
                    seen,
                    APP_FILE,
                    f"read {fld.name} from the request in {endpoint.method} {endpoint.path}",
                )

    # --- new entity --------------------------------------------------------
    for entity in delta.add_entities:
        if present(DB_FILE):
            _add(out, seen, DB_FILE, f"create the {entity.table} table")
        if present(MODELS_FILE):
            _add(
                out,
                seen,
                MODELS_FILE,
                f"add list/get/create query helpers for {entity.table}",
            )
        if present(SEED_FILE):
            _add(out, seen, SEED_FILE, f"seed a few demo {entity.table} rows")

    # --- new endpoint ------------------------------------------------------
    for endpoint in delta.add_endpoints:
        if present(APP_FILE):
            _add(
                out,
                seen,
                APP_FILE,
                f"define the {endpoint.method} {endpoint.path} route",
            )
        if endpoint.template and present(endpoint.template):
            _add(
                out,
                seen,
                endpoint.template,
                f"point the form at {endpoint.method} {endpoint.path}",
            )

    # --- new page ----------------------------------------------------------
    for page in delta.add_pages:
        if present(APP_FILE):
            _add(
                out,
                seen,
                APP_FILE,
                f"add the view function that renders {page.template or page.route}",
            )
        # The nav lives in base.html and ONLY in base.html, so a new page always
        # touches it — this is the rule that keeps pages from drifting apart.
        if page.nav_label and present(BASE_TEMPLATE):
            _add(
                out,
                seen,
                BASE_TEMPLATE,
                f'add a nav link "{page.nav_label}" to {page.route or page.template}',
            )

    # Keep a file's edits adjacent so they run back to back against the version
    # each previous one just wrote, rather than interleaved with other files.
    out.sort(key=lambda e: list(seen).index(e.filename))
    return out[:MAX_EDITS]


def describe(edits: list[Edit]) -> str:
    """The user-facing "these files also changed, and why" block.

    Shown because it is the capability the whole plan is about: "I added images,
    and it went back and updated db.py, models.py, the storefront template and
    the seed script so everything still lines up." Grouped by file, since one
    file can legitimately change for several reasons.
    """
    if not edits:
        return ""
    grouped: dict[str, list[str]] = {}
    for edit in edits:
        grouped.setdefault(edit.filename, []).append(edit.reason)
    lines = "\n".join(
        f"- `{name}` — {'; '.join(reasons)}" for name, reasons in grouped.items()
    )
    return f"Updated {len(grouped)} existing file(s) so they still line up:\n{lines}"


# ---------------------------------------------------------------------------
# The deterministic half of db.py
# ---------------------------------------------------------------------------

_MIGRATION_START = "    # --- added columns ---"
_INIT_DB_RE = re.compile(r"^def init_db\(\).*?:\n", re.MULTILINE)
_COMMIT_RE = re.compile(r"^(\s*)conn\.commit\(\)", re.MULTILINE)


def vanished_routes(spec: ProjectSpec, app_source: str) -> list:
    """Routes the spec remembers that `app.py` no longer defines.

    The regression this catches is the one the whole plan is about: turn 2's
    surgical edit to `app.py` replaced turn 1's `/products` route with the new
    `/admin/products` one, so a page that worked before the amendment 404'd
    after it. Measured on the live two-turn demo. Nothing else can see this —
    the file compiles, the new route works, and the turn reports success.

    Only routes from an EARLIER revision count: a route added this turn that the
    model hasn't written yet is the coverage check's business, not a regression.
    """
    live = {(m, p) for m, p, _v, _t in _routes_from(app_source)}
    return [
        e
        for e in spec.endpoints
        if e.added_in < spec.revision and (e.method, e.path) not in live
    ]


def restore_page_routes(app_source: str, missing: list) -> tuple[str, list[str]]:
    """Re-add deleted GET routes that simply render a template.

    Deterministic and bounded: a page route's whole body is
    `return render_template(...)`, so it can be restored exactly. A POST
    handler's body cannot — that is domain logic, and inventing it would be
    generation, not repair. Those are reported instead.

    Returns ``(source, restored_paths)``.
    """
    text = app_source or ""
    if "render_template" not in text:
        return app_source, []

    restored: list[str] = []
    blocks: list[str] = []
    for endpoint in missing:
        if endpoint.method != "GET" or not endpoint.template:
            continue
        view = _view_name(endpoint.path)
        if f"def {view}(" in text or f'@app.route("{endpoint.path}"' in text:
            continue
        template = endpoint.template.split("templates/", 1)[-1]
        blocks.append(
            f'\n\n@app.route("{endpoint.path}")\n'
            f"def {view}():\n"
            f'    """Restored by Coder — this page existed before the last change."""\n'
            f'    return render_template("{template}")\n'
        )
        restored.append(endpoint.path)

    if not blocks:
        return app_source, []

    guard = _MAIN_GUARD_RE.search(text)
    body = "".join(blocks)
    if guard:
        at = guard.start()
        return text[:at].rstrip("\n") + "\n" + body + "\n\n" + text[at:], restored
    return text.rstrip("\n") + "\n" + body, restored


def _view_name(path: str) -> str:
    """`/admin/products` -> `admin_products`, safe as a Python identifier."""
    slug = re.sub(r"[^a-z0-9]+", "_", (path or "").lower()).strip("_")
    return f"page_{slug}" if slug else "page_root"


_MAIN_GUARD_RE = re.compile(r"^if __name__ == .__main__.:", re.MULTILINE)
_ROUTE_SCAN_RE = re.compile(
    r"@app\.route\(\s*[\"'](?P<path>/[^\"']*)[\"'](?P<rest>[^)]*)\)", re.MULTILINE
)
_METHODS_SCAN_RE = re.compile(r"methods\s*=\s*\[(?P<m>[^\]]*)\]")


def _routes_from(source: str):
    """`(method, path, "", "")` for every @app.route in a module.

    Deliberately simpler than `projectspec.routes_from_source`: this one only
    needs to know whether a route EXISTS, so it must not depend on the view body
    parsing cleanly.
    """
    out = []
    for match in _ROUTE_SCAN_RE.finditer(source or ""):
        methods_match = _METHODS_SCAN_RE.search(match.group("rest") or "")
        methods = (
            [
                m.strip().strip("\"'").upper()
                for m in methods_match.group("m").split(",")
            ]
            if methods_match
            else ["GET"]
        )
        for method in methods or ["GET"]:
            if method:
                out.append((method, match.group("path"), "", ""))
    return out


def migration_block(spec: ProjectSpec, since: int) -> str:
    """The `ensure_column` lines a schema change needs, ready to paste.

    Written from the spec rather than generated, per the plan: a migration is
    exactly derivable from `added_in`, and letting a 7B model write `ALTER TABLE`
    against live data is a risk with no upside.
    """
    calls = spec.migrations(since=since)
    if not calls:
        return ""
    body = "\n".join(f"        {c}" for c in calls)
    return (
        "        # --- added columns (written by Coder from the project spec) ---\n"
        f"{body}\n"
    )


def apply_migration_block(source: str, block: str) -> tuple[str, bool]:
    """Insert `block` into `init_db()`, just before its `conn.commit()`.

    Returns ``(source, changed)`` and declines rather than guessing when the
    shape isn't recognisable — a half-applied edit to the file that owns the
    schema is worse than none. Idempotent: a call already present is not added
    twice.
    """
    if not block:
        return source, False
    text = source or ""
    wanted = [line.strip() for line in block.splitlines() if "ensure_column" in line]
    missing = [line for line in wanted if line not in text]
    if not missing:
        return source, False  # already applied

    init_match = _INIT_DB_RE.search(text)
    if not init_match:
        return source, False

    commit = _COMMIT_RE.search(text, init_match.end())
    if not commit:
        return source, False

    indent = commit.group(1)
    inserted = "".join(f"{indent}{line}\n" for line in missing)
    at = commit.start()
    return text[:at] + inserted + text[at:], True
