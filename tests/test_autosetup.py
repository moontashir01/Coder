"""`/run` doing the setup instead of printing it (`StackAdapter.autosetup`).

`readiness` names why a generated app cannot start, and on the Node stack three
of the reasons it names are commands someone has to type in another terminal, in
the right order, before a single page opens. `autosetup` runs those three.

What is tested here is almost entirely what it must NOT do, because every one of
those is a way for it to be worse than the instructions it replaced:

  * it must not create a database because a PASSWORD was refused;
  * it must not create one because the probe could not find out;
  * it must not interpolate a name it did not generate into `CREATE DATABASE`;
  * it must not report a step as done when the step failed;
  * it must not touch a folder that is not a Node project of ours;
  * and it must not change the Flask stack at all.

Fully offline: no npm, no node, no PostgreSQL. Every subprocess-shaped helper is
substituted, and the two that are not (`_npm_install`, `_create_database`) are
reached only through them.
"""

from pathlib import Path

import pytest

from app.agent.stacks.flask_adapter import FLASK
from app.agent.stacks.node_adapter import NODE


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A directory that looks like a scaffolded Node project."""
    (tmp_path / "package.json").write_text('{"name": "shop"}', encoding="utf-8")
    (tmp_path / "seed.js").write_text("// seed", encoding="utf-8")
    return tmp_path


@pytest.fixture
def node_present(monkeypatch):
    """Pretend `node` is on PATH without requiring it to be."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")


def _spy(record: list, key: str, result):
    def fn(*args, **kwargs):
        record.append(key)
        return result

    return fn


# --- Flask is untouched ---------------------------------------------------


def test_flask_has_no_setup_step(tmp_path):
    """sqlite is a file and Flask lives in Coder's own venv — nothing to do.

    And it returns [], not a "nothing needed" line: a step that did not happen
    must never be printed as one that did.
    """
    assert FLASK.autosetup(tmp_path) == []


# --- the preconditions ----------------------------------------------------


def test_without_node_nothing_is_attempted(project, monkeypatch):
    """No Node means an installer is needed, which this may not run."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    calls: list = []
    monkeypatch.setattr(NODE, "_npm_install", _spy(calls, "npm", (True, "ok")))

    assert NODE.autosetup(project) == []
    assert calls == []


def test_a_folder_that_is_not_our_project_is_left_alone(
    tmp_path, node_present, monkeypatch
):
    """No `package.json` — an adopted repo with its own tooling is not ours."""
    calls: list = []
    monkeypatch.setattr(NODE, "_npm_install", _spy(calls, "npm", (True, "ok")))

    assert NODE.autosetup(tmp_path) == []
    assert calls == []


# --- npm install ----------------------------------------------------------


def test_dependencies_are_installed_when_they_are_missing(
    project, node_present, monkeypatch
):
    calls: list = []
    monkeypatch.setattr(
        NODE, "_npm_install", _spy(calls, "npm", (True, "installed the deps"))
    )
    monkeypatch.setattr(NODE, "_postgres_listening", lambda: False)

    assert NODE.autosetup(project) == ["installed the deps"]
    assert calls == ["npm"]


def test_an_existing_node_modules_is_not_reinstalled(
    project, node_present, monkeypatch
):
    (project / "node_modules").mkdir()
    calls: list = []
    monkeypatch.setattr(NODE, "_npm_install", _spy(calls, "npm", (True, "installed")))
    monkeypatch.setattr(NODE, "_postgres_listening", lambda: False)

    assert NODE.autosetup(project) == []
    assert calls == []


def test_a_failed_install_stops_and_says_so(project, node_present, monkeypatch):
    """The failure is REPORTED and the database step never runs on top of it."""
    calls: list = []
    monkeypatch.setattr(
        NODE, "_npm_install", lambda root: (False, "npm install failed: ENOTFOUND")
    )
    monkeypatch.setattr(NODE, "_postgres_listening", _spy(calls, "port", True))

    done = NODE.autosetup(project)

    assert done == ["npm install failed: ENOTFOUND"]
    assert calls == [], "nothing may be attempted on top of a failed install"


# --- creating the database ------------------------------------------------


def _ready(project, monkeypatch, payload):
    """Dependencies installed, PostgreSQL answering, probe returns `payload`."""
    (project / "node_modules").mkdir(exist_ok=True)
    monkeypatch.setattr(NODE, "_postgres_listening", lambda: True)
    monkeypatch.setattr(NODE, "_probe_database", lambda root: payload)


def test_a_missing_database_is_created_and_seeded(project, node_present, monkeypatch):
    _ready(project, monkeypatch, {"ok": False, "code": "3D000", "database": "shop"})
    created: list = []
    monkeypatch.setattr(
        NODE,
        "_create_database",
        lambda root, name: (
            created.append(name),
            (True, f"created the database {name}"),
        )[1],
    )
    monkeypatch.setattr(NODE, "_seed", lambda root, say: "created the tables")

    done = NODE.autosetup(project)

    assert created == ["shop"]
    assert done == ["created the database shop", "created the tables"]


def test_a_refused_password_never_creates_anything(project, node_present, monkeypatch):
    """28P01 is a password this cannot guess.

    Creating a database under some other working credential would hide that, and
    the user would be looking at an empty server wondering why their data is
    gone. Only `3D000` means "the thing does not exist yet".
    """
    _ready(project, monkeypatch, {"ok": False, "code": "28P01", "database": "shop"})
    calls: list = []
    monkeypatch.setattr(NODE, "_create_database", _spy(calls, "create", (True, "x")))

    assert NODE.autosetup(project) == []
    assert calls == []


def test_could_not_find_out_creates_nothing(project, node_present, monkeypatch):
    """None is "we could not tell" — the same rule N5's probe follows."""
    _ready(project, monkeypatch, None)
    calls: list = []
    monkeypatch.setattr(NODE, "_create_database", _spy(calls, "create", (True, "x")))

    assert NODE.autosetup(project) == []
    assert calls == []


def test_a_reachable_database_is_left_alone(project, node_present, monkeypatch):
    _ready(project, monkeypatch, {"ok": True, "database": "shop"})
    calls: list = []
    monkeypatch.setattr(NODE, "_create_database", _spy(calls, "create", (True, "x")))

    assert NODE.autosetup(project) == []
    assert calls == []


def test_a_name_that_is_not_an_identifier_is_refused(
    project, node_present, monkeypatch
):
    """`CREATE DATABASE` takes no bound parameter, so the name is interpolated.

    It therefore has to be an identifier this project generated. Anything else is
    refused rather than quoted and hoped for — `projectspec._ident`'s rule.
    """
    _ready(
        project,
        monkeypatch,
        {"ok": False, "code": "3D000", "database": 'shop"; DROP DATABASE x; --'},
    )
    calls: list = []
    monkeypatch.setattr(NODE, "_create_database", _spy(calls, "create", (True, "x")))

    assert NODE.autosetup(project) == []
    assert calls == []


def test_the_guard_is_also_inside_create_database(project):
    """Belt and braces: the caller checks, and so does the thing it calls."""
    ok, note = NODE._create_database(project, "shop; DROP DATABASE x")
    assert ok is False
    assert "refusing" in note


def test_a_failed_creation_is_not_seeded(project, node_present, monkeypatch):
    _ready(project, monkeypatch, {"ok": False, "code": "3D000", "database": "shop"})
    monkeypatch.setattr(
        NODE, "_create_database", lambda root, name: (False, "could not create it")
    )
    calls: list = []
    monkeypatch.setattr(NODE, "_seed", _spy(calls, "seed", "seeded"))

    assert NODE.autosetup(project) == ["could not create it"]
    assert calls == []


def test_a_stopped_postgres_is_left_to_the_readiness_check(
    project, node_present, monkeypatch
):
    """Starting a service needs an administrator, so it stays reported."""
    (project / "node_modules").mkdir()
    monkeypatch.setattr(NODE, "_postgres_listening", lambda: False)
    calls: list = []
    monkeypatch.setattr(NODE, "_probe_database", _spy(calls, "probe", None))

    assert NODE.autosetup(project) == []
    assert calls == []


# --- the log hook ---------------------------------------------------------


def test_progress_is_reported_while_it_happens(project, node_present, monkeypatch):
    """An `npm install` on a cold cache is ~30s of silence otherwise."""
    lines: list[str] = []
    monkeypatch.setattr(NODE, "_npm_install", lambda root: (True, "installed"))
    monkeypatch.setattr(NODE, "_postgres_listening", lambda: False)

    NODE.autosetup(project, log=lines.append)

    assert any("npm install" in line for line in lines)


def test_a_raising_log_hook_does_not_break_setup(project, node_present, monkeypatch):
    def boom(_line):
        raise RuntimeError("no console")

    monkeypatch.setattr(NODE, "_npm_install", lambda root: (True, "installed"))
    monkeypatch.setattr(NODE, "_postgres_listening", lambda: False)

    assert NODE.autosetup(project, log=boom) == ["installed"]


# --- the seed is best-effort ---------------------------------------------


def test_a_project_with_no_seed_script_still_reports_the_database(
    project, node_present, monkeypatch
):
    (project / "seed.js").unlink()
    _ready(project, monkeypatch, {"ok": False, "code": "3D000", "database": "shop"})
    monkeypatch.setattr(
        NODE,
        "_create_database",
        lambda root, name: (True, f"created the database {name}"),
    )

    assert NODE.autosetup(project) == ["created the database shop"]
