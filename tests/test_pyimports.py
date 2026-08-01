"""Missing-import repair (app/agent/pyimports.py) — the NameError compile() can't see.

Fully offline. The headline test replays the exact `app.py` a live
`build me a blog` produced, which passed `verified OK` and then 500'd.
"""

from types import SimpleNamespace

import pytest

from app.agent.core import AgentCore
from app.agent.pyimports import (
    add_missing_imports,
    duplicate_definitions,
    missing_tables,
    undefined_names,
    unresolved_local_calls,
    uses_flask,
)
from config.settings import settings


class ScriptedLLM:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    def invoke(self, messages):
        out = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        return SimpleNamespace(content=out)


# Verbatim from docs/phase1-notes.md — build 3's app.py, trimmed to the routes.
LIVE_FAILURE = '''"""Blog."""

import os
from pathlib import Path

import db
from flask import Flask, render_template

app = Flask(__name__)
db.init_db()


@app.route("/posts")
def list_posts():
    """List all blog posts."""
    db_conn = get_db()
    posts = models.get_all_posts(db_conn)
    return render_template("posts.html", posts=posts)


@app.route("/posts/new", methods=["GET", "POST"])
def new_post():
    if request.method == "POST":
        title = request.form["title"]
        db_conn = get_db()
        models.add_post(db_conn, title)
        flash("Post created successfully!")
        return redirect(url_for("list_posts"))
    return render_template("new_post.html")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
'''


# ---------------------------------------------------------------------------
# undefined_names
# ---------------------------------------------------------------------------


def test_finds_exactly_the_names_the_live_build_left_undefined():
    missing = undefined_names(LIVE_FAILURE)
    assert missing == {"get_db", "models", "request", "flash", "redirect", "url_for"}


def test_bound_names_are_never_reported():
    """Imports, defs, args, assignments, loops, with/except targets and
    comprehensions all bind — none of them may show up as missing."""
    source = """
import os
from flask import Flask

app = Flask(__name__)
TOTAL = 0


def handler(value, *rest, **options):
    local = value
    for item in rest:
        local += item
    with open(os.devnull) as fh:
        data = fh.read()
    try:
        pass
    except ValueError as err:
        data = str(err)
    return [x for x in local], {k: v for k, v in options.items()}, data, TOTAL


class Thing:
    pass


thing = Thing()
"""
    assert undefined_names(source) == set()


def test_module_dunders_are_never_reported():
    """Regression: `__file__` is NOT in dir(builtins), so a live build reported
    `BASE_DIR = Path(__file__).resolve()` — a line the SCAFFOLD itself ships —
    as an undefined name."""
    source = (
        "from flask import Flask\n"
        "from pathlib import Path\n\n"
        "BASE_DIR = Path(__file__).resolve().parent\n"
        "app = Flask(__name__)\n\n"
        'if __name__ == "__main__":\n    app.run()\n'
    )
    assert undefined_names(source) == set()


def test_builtins_are_not_missing():
    assert (
        undefined_names("from flask import Flask\nx = len([1]) + int('2')\n") == set()
    )


def test_unparseable_source_reports_nothing():
    """A file that doesn't parse belongs to the syntax repair, not this pass."""
    assert undefined_names("def broken(:\n") == set()


# ---------------------------------------------------------------------------
# uses_flask — the gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source,expected",
    [
        ("from flask import Flask\n", True),
        ("import flask\n", True),
        ("@app.route('/')\ndef x():\n    pass\n", True),
        ("import os\nprint(os.getcwd())\n", False),
    ],
)
def test_uses_flask_gate(source, expected):
    assert uses_flask(source) is expected


def test_non_flask_modules_are_never_touched():
    source = "import os\n\n\ndef main():\n    return undefined_thing\n"
    fixed, added, unresolved = add_missing_imports(source)
    assert fixed == source and added == [] and unresolved == []


# ---------------------------------------------------------------------------
# add_missing_imports
# ---------------------------------------------------------------------------


def test_repairs_the_live_failure_end_to_end():
    fixed, added, unresolved = add_missing_imports(
        LIVE_FAILURE, frozenset({"db", "models"})
    )

    assert unresolved == []
    assert undefined_names(fixed) == set()  # the whole point
    compile(fixed, "app.py", "exec")
    # Flask names joined the EXISTING import line rather than adding a second.
    assert fixed.count("from flask import") == 1
    for name in ("request", "redirect", "url_for", "flash", "render_template"):
        assert name in fixed.split("\n\n")[1] or f" {name}" in fixed
    assert "from db import get_db" in fixed
    assert "import models" in fixed
    assert added


def test_local_module_imports_require_the_file_to_exist():
    """`import models` is only correct if models.py is really there."""
    _, added, unresolved = add_missing_imports(LIVE_FAILURE, frozenset())
    assert not any("models" in stmt for stmt in added)
    assert "models" in unresolved
    assert "get_db" in unresolved


def test_unknown_names_are_reported_never_guessed():
    source = "from flask import Flask\n\napp = Flask(__name__)\nx = mystery_helper()\n"
    fixed, added, unresolved = add_missing_imports(source)
    assert fixed == source
    assert added == []
    assert unresolved == ["mystery_helper"]


def test_adds_a_flask_import_line_when_there_is_none():
    source = "import flask\n\napp = flask.Flask(__name__)\n\n\ndef v():\n    return jsonify({})\n"
    fixed, added, _ = add_missing_imports(source)
    assert "from flask import jsonify" in fixed
    compile(fixed, "app.py", "exec")


def test_known_third_party_helpers_resolve():
    source = (
        "from flask import Flask\n\napp = Flask(__name__)\n\n\n"
        "def up(f):\n    return secure_filename(f.filename)\n"
    )
    fixed, added, unresolved = add_missing_imports(source)
    assert "from werkzeug.utils import secure_filename" in fixed
    assert unresolved == []


def test_imports_land_after_the_docstring_not_before_it():
    source = '"""Doc."""\n\nfrom flask import Flask\n\napp = Flask(__name__)\np = Path(".")\n'
    fixed, _, _ = add_missing_imports(source)
    assert fixed.startswith('"""Doc."""')
    assert "from pathlib import Path" in fixed
    compile(fixed, "app.py", "exec")


def test_a_clean_file_is_returned_untouched():
    source = (
        "from flask import Flask, request\n\napp = Flask(__name__)\n\n\n"
        "def v():\n    return request.args.get('q')\n"
    )
    fixed, added, unresolved = add_missing_imports(source)
    assert fixed == source and added == [] and unresolved == []


def test_result_always_parses():
    """Belt and braces: the pass must never hand back a file it just broke."""
    for local in (frozenset(), frozenset({"db", "models"})):
        fixed, _, _ = add_missing_imports(LIVE_FAILURE, local)
        compile(fixed, "app.py", "exec")


# ---------------------------------------------------------------------------
# unresolved_local_calls — the AttributeError nothing else can see
# ---------------------------------------------------------------------------


MODELS_PY = '''from db import get_db


def add_post(db_conn, title, content):
    """Add a post."""
    return 1
'''


def test_detects_a_call_into_a_sibling_that_does_not_define_it():
    """Live failure: app.py calls models.get_all_posts, models.py only defines
    add_post. Both files compile, the import resolves, and /posts 500s."""
    missing = unresolved_local_calls(LIVE_FAILURE, {"models": MODELS_PY})
    assert "models.get_all_posts" in missing
    assert "models.add_post" not in missing


def test_detects_a_from_import_of_a_name_that_does_not_exist():
    """Worse than a 500 — this one kills the process at startup."""
    source = "from flask import Flask\nfrom db import get_db, nonexistent\n\napp = Flask(__name__)\n"
    missing = unresolved_local_calls(source, {"db": "def get_db():\n    return None\n"})
    assert missing == ["db.nonexistent"]


def test_module_level_constants_and_classes_count_as_defined():
    module = (
        "DATABASE = 'app.db'\n\n\nclass Store:\n    pass\n\n\ndef helper():\n    pass\n"
    )
    source = (
        "from flask import Flask\nimport models\n\n"
        "x = models.DATABASE\ny = models.Store()\nz = models.helper()\n"
    )
    assert unresolved_local_calls(source, {"models": module}) == []


def test_unknown_modules_are_ignored():
    """Only modules we were handed the source of are judged."""
    source = "from flask import Flask\nimport requests\n\nrequests.get('x')\n"
    assert unresolved_local_calls(source, {"models": MODELS_PY}) == []


def test_unparseable_source_reports_no_dangling_calls():
    assert unresolved_local_calls("def broken(:\n", {"models": MODELS_PY}) == []


# ---------------------------------------------------------------------------
# missing_tables / duplicate_definitions — the two live build-6 defects
# ---------------------------------------------------------------------------


def test_missing_table_is_reported():
    """Live build 6: init_db() kept only the scaffold's COMMENTED example, so
    nothing created `posts`, and every route touching it 500'd."""
    db_py = "def init_db():\n    # conn.execute('''CREATE TABLE IF NOT EXISTS products (...)''')\n    pass\n"
    app_py = "from flask import Flask\n\nrows = conn.execute('SELECT * FROM posts').fetchall()\n"
    assert missing_tables({"db": db_py, "app": app_py}) == ["posts"]


def test_created_tables_are_not_reported():
    db_py = "def init_db():\n    conn.execute('CREATE TABLE IF NOT EXISTS posts (id INTEGER)')\n"
    app_py = "rows = conn.execute('SELECT * FROM posts')\ncur = conn.execute('INSERT INTO posts (t) VALUES (?)', (1,))\n"
    assert missing_tables({"db": db_py, "app": app_py}) == []


def test_sqlite_internal_tables_are_not_reported():
    src = {
        "db": "conn.execute('SELECT name FROM sqlite_master WHERE type=\"table\"')\n"
    }
    assert missing_tables(src) == []


def test_duplicate_definitions_are_reported():
    """A surgical edit re-inserted db.py's whole tail; the SECOND, table-less
    init_db() is the one Python actually runs."""
    source = (
        "def get_db():\n    return 1\n\n\n"
        "def init_db():\n    pass\n\n\n"
        "def get_db():\n    return 2\n\n\n"
        "def init_db():\n    pass\n"
    )
    assert duplicate_definitions(source) == ["get_db", "init_db"]


def test_no_duplicates_in_a_normal_module():
    assert duplicate_definitions(MODELS_PY) == []


def test_duplicate_check_ignores_unparseable_source():
    assert duplicate_definitions("def broken(:\n") == []


def test_cross_module_check_reports_both_new_defects(tmp_path):
    a = AgentCore(session_id="pytest_defects")
    (tmp_path / "db.py").write_text(
        "def init_db():\n    pass\n\n\ndef init_db():\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "app.py").write_text(
        "from flask import Flask\n\napp = Flask(__name__)\n"
        "rows = conn.execute('SELECT * FROM posts')\n",
        encoding="utf-8",
    )

    found = " ".join(a._check_cross_module_calls(tmp_path))

    assert "init_db() twice" in found
    assert "no CREATE TABLE for `posts`" in found


# ---------------------------------------------------------------------------
# Wired into the write path
# ---------------------------------------------------------------------------


async def test_verify_and_repair_adds_the_missing_imports(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_imports")

    (tmp_path / "db.py").write_text(
        "def get_db():\n    return None\n", encoding="utf-8"
    )
    (tmp_path / "models.py").write_text(
        "def get_all_posts(c):\n    return []\n", encoding="utf-8"
    )
    app_py = tmp_path / "app.py"
    app_py.write_text(LIVE_FAILURE, encoding="utf-8")

    note, _ = await a._verify_and_repair(app_py, "app.py")

    assert "missing import" in note
    assert undefined_names(app_py.read_text(encoding="utf-8")) == set()


async def test_cross_module_check_runs_at_the_end_not_per_file(tmp_path, monkeypatch):
    """It must NOT fire during `_verify_and_repair`.

    Regression from a live build: app.py is written before models.py is
    regenerated, so a per-file check read the scaffold stub and reported
    `models.add_post` as missing — while the very next file in the same build
    defined it. A check that cries wolf is worse than no check.
    """
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_dangling_timing")

    (tmp_path / "db.py").write_text(
        "def get_db():\n    return None\n", encoding="utf-8"
    )
    # models.py is still the scaffold stub at this point in the build.
    (tmp_path / "models.py").write_text("from db import get_db\n", encoding="utf-8")
    app_py = tmp_path / "app.py"
    app_py.write_text(LIVE_FAILURE, encoding="utf-8")

    note, _ = await a._verify_and_repair(app_py, "app.py")

    assert "do not exist" not in note
    assert "AttributeError" not in note


def test_cross_module_check_reports_only_what_is_really_missing(tmp_path):
    """End of turn, every file final: add_post exists, get_all_posts does not."""
    a = AgentCore(session_id="pytest_dangling_end")

    (tmp_path / "db.py").write_text(
        "def get_db():\n    return None\n", encoding="utf-8"
    )
    (tmp_path / "models.py").write_text(MODELS_PY, encoding="utf-8")
    (tmp_path / "app.py").write_text(LIVE_FAILURE, encoding="utf-8")

    dangling = a._check_cross_module_calls(tmp_path)

    joined = " ".join(dangling)
    assert "models.get_all_posts" in joined
    assert "models.add_post" not in joined  # it IS defined — no crying wolf


def test_cross_module_check_is_quiet_on_a_consistent_project(tmp_path):
    a = AgentCore(session_id="pytest_dangling_clean")

    (tmp_path / "db.py").write_text(
        "def get_db():\n    return None\n", encoding="utf-8"
    )
    (tmp_path / "models.py").write_text(MODELS_PY, encoding="utf-8")
    (tmp_path / "app.py").write_text(
        "from flask import Flask\nimport models\n\n"
        "app = Flask(__name__)\n\n\n"
        "@app.route('/')\ndef v():\n    return str(models.add_post(None, 'a', 'b'))\n",
        encoding="utf-8",
    )

    assert a._check_cross_module_calls(tmp_path) == []


async def test_verify_and_repair_reports_names_it_cannot_resolve(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_imports_unresolved")

    app_py = tmp_path / "app.py"
    app_py.write_text(
        "from flask import Flask\n\napp = Flask(__name__)\n\n\n"
        "@app.route('/')\ndef v():\n    return mystery()\n",
        encoding="utf-8",
    )

    note, _ = await a._verify_and_repair(app_py, "app.py")

    assert "may not meet" in note and "mystery" in note


async def test_non_python_files_skip_the_pass(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "allow_network", True)  # keep stage 0 inert too
    a = AgentCore(session_id="pytest_imports_html")

    page = tmp_path / "index.html"
    page.write_text(
        "<!doctype html><html><body><h1>Hi</h1></body></html>", encoding="utf-8"
    )

    note, _ = await a._verify_and_repair(page, "index.html")

    assert "missing import" not in note
