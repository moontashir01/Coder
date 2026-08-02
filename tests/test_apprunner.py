"""Demo surface (Phase 6): /run, /plan's amendment preview, README from the spec.

Offline. The runner tests start a real subprocess, because a process manager
tested only against a fake has not been tested — and an orphan holding port 5000
is exactly the failure that would show up mid-demo.
"""

import io
import socket
import textwrap
from types import SimpleNamespace

import pytest
from rich.console import Console

import app.cli.commands as commands_mod
from app.agent.apprunner import AppRunner, get_runner
from app.agent.projectspec import Entity, Field, Page, ProjectSpec, SpecEndpoint
from app.cli.commands import handle_command


def _port_free(port: int = 5000) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", port)) != 0


_TINY_APP = """
from http.server import BaseHTTPRequestHandler, HTTPServer


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<html><body>up</body></html>")

    def log_message(self, *a):
        pass


HTTPServer(("127.0.0.1", 5000), H).serve_forever()
"""

_DEAD_APP = "import sys\nsys.stderr.write('ValueError: nope\\n')\nsys.exit(1)\n"


@pytest.fixture
def runner():
    r = AppRunner()
    try:
        yield r
    finally:
        r.stop()


# ---------------------------------------------------------------------------
# AppRunner
# ---------------------------------------------------------------------------


def test_a_started_app_reports_a_url_and_stays_up(tmp_path, runner):
    if not _port_free():
        pytest.skip("port 5000 in use")
    (tmp_path / "app.py").write_text(textwrap.dedent(_TINY_APP), encoding="utf-8")

    ok, message = runner.start(tmp_path)

    assert ok, message
    assert runner.is_running()
    assert runner.url == "http://127.0.0.1:5000"
    assert "5000" in message
    assert "running at" in runner.status()


def test_stopping_actually_frees_the_port(tmp_path, runner):
    """An orphan holding :5000 is the failure that shows up mid-demo."""
    if not _port_free():
        pytest.skip("port 5000 in use")
    (tmp_path / "app.py").write_text(textwrap.dedent(_TINY_APP), encoding="utf-8")
    runner.start(tmp_path)

    assert runner.stop() is True
    assert runner.is_running() is False
    assert runner.url == ""
    for _ in range(20):
        if _port_free():
            break
        import time

        time.sleep(0.25)
    assert _port_free(), "the port is still held after stop()"


def test_starting_twice_does_not_launch_a_second_copy(tmp_path, runner):
    """Two copies would fight over the port and over app.db."""
    if not _port_free():
        pytest.skip("port 5000 in use")
    (tmp_path / "app.py").write_text(textwrap.dedent(_TINY_APP), encoding="utf-8")
    runner.start(tmp_path)
    first = runner._proc

    ok, message = runner.start(tmp_path)

    assert ok and runner._proc is first
    assert "already" in message


def test_restart_reuses_the_same_directory(tmp_path, runner):
    if not _port_free():
        pytest.skip("port 5000 in use")
    (tmp_path / "app.py").write_text(textwrap.dedent(_TINY_APP), encoding="utf-8")
    runner.start(tmp_path)

    ok, message = runner.restart()

    assert ok, message
    assert runner.workdir == tmp_path


def test_restart_before_anything_ran_is_a_clear_message(runner):
    ok, message = runner.restart()
    assert ok is False and "nothing has been run" in message


def test_a_missing_app_is_reported_not_raised(tmp_path, runner):
    ok, message = runner.start(tmp_path)
    assert ok is False and "no app.py" in message


def test_an_app_that_dies_on_startup_reports_its_error(tmp_path, runner):
    (tmp_path / "app.py").write_text(_DEAD_APP, encoding="utf-8")

    ok, message = runner.start(tmp_path)

    assert ok is False
    assert "ValueError: nope" in message
    assert runner.is_running() is False


def test_stopping_when_nothing_runs_is_false_not_an_error(runner):
    assert runner.stop() is False


def test_the_session_runner_is_a_singleton():
    assert get_runner() is get_runner()


# ---------------------------------------------------------------------------
# /run
# ---------------------------------------------------------------------------


class _FakeAgent:
    def __init__(self, spec=None, preview=None):
        self.project_path = None
        self._spec = spec
        self._preview = preview or {}

    def get_spec(self):
        return self._spec

    async def preview_amendment(self, message):
        return self._preview

    def split_tasks(self, task):
        return [task]

    def get_plan(self, task):
        return {"task_type": "code_generation", "steps": []}


class _FakeRepl:
    def __init__(self, agent):
        self.agent = agent


@pytest.fixture
def captured_console(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(
        commands_mod, "console", Console(file=buf, force_terminal=False, width=200)
    )
    return buf


async def test_run_status_when_nothing_is_running(captured_console):
    handled = await handle_command("/run status", _FakeRepl(_FakeAgent()))
    assert handled is True
    assert "not running" in captured_console.getvalue()


async def test_run_stop_when_nothing_is_running(captured_console):
    handled = await handle_command("/run stop", _FakeRepl(_FakeAgent()))
    assert handled is True
    assert "Nothing was running" in captured_console.getvalue()


async def test_run_reports_a_missing_app(tmp_path, captured_console, monkeypatch):
    monkeypatch.chdir(tmp_path)
    handled = await handle_command("/run", _FakeRepl(_FakeAgent()))
    assert handled is True
    assert "no app.py" in captured_console.getvalue()


# ---------------------------------------------------------------------------
# /plan — the amendment preview
# ---------------------------------------------------------------------------


async def test_plan_shows_the_delta_and_the_files_it_will_touch(captured_console):
    """ "These 4 files will be updated, and here's why" — shown BEFORE it happens."""
    preview = {
        "summary": "add product images",
        "revision": 1,
        "changes": ["product.image_path (TEXT)"],
        "new_files": ["templates/admin.html"],
        "edits": [
            ("models.py", "include image_path in the column lists for products"),
            ("templates/index.html", "show image_path for each product"),
        ],
    }
    repl = _FakeRepl(_FakeAgent(preview=preview))

    handled = await handle_command("/plan add a picture to products", repl)

    out = captured_console.getvalue()
    assert handled is True
    assert "Amendment" in out and "revision 1" in out
    assert "product.image_path" in out
    assert "templates/admin.html" in out
    assert "models.py" in out and "column lists" in out
    assert "Nothing has been changed" in out


async def test_plan_falls_back_to_the_ordinary_planner(captured_console):
    """No spec, or not an amendment → the plan command behaves as it always did."""
    repl = _FakeRepl(_FakeAgent(preview={}))

    handled = await handle_command("/plan build me a blog", repl)

    out = captured_console.getvalue()
    assert handled is True
    assert "Amendment" not in out
    assert "Planner" in out


# ---------------------------------------------------------------------------
# README from the spec
# ---------------------------------------------------------------------------


def _shop_spec() -> ProjectSpec:
    return ProjectSpec(
        name="bookshop",
        summary="Online bookstore",
        revision=2,
        entities=(
            Entity(
                "product",
                "products",
                (
                    Field("id", "INTEGER", pk=True, required=True),
                    Field("title", "TEXT", required=True),
                    Field("image_path", "TEXT", added_in=2),
                ),
            ),
        ),
        endpoints=(SpecEndpoint("POST", "/admin/products", entity="product"),),
        pages=(Page("/", "templates/index.html", "Home"),),
    )


def test_readme_describes_this_project_not_the_template():
    text = _shop_spec().to_readme()

    assert text.startswith("# bookshop")
    assert "Online bookstore" in text
    assert "`products`" in text and "`image_path`" in text
    assert "`/admin/products`" in text
    assert "`templates/index.html`" in text


def test_readme_records_which_revision_a_field_arrived_in():
    """Proof of memory, in the file a human reads first."""
    text = _shop_spec().to_readme()
    assert "added in revision 2" in text
    assert "revision 2" in text.splitlines()[-1]  # the footer line


def test_readme_deploy_line_binds_correctly():
    """gunicorn's default 127.0.0.1 would deploy green and stay unreachable."""
    text = _shop_spec().to_readme()
    assert "gunicorn app:app --bind 0.0.0.0:$PORT" in text


def test_readme_for_an_empty_spec_is_still_useful():
    text = ProjectSpec(name="thing").to_readme()
    assert "# thing" in text and "python app.py" in text


def test_the_build_replaces_the_scaffold_readme(tmp_path, monkeypatch):
    """The scaffold ships a generic README; leaving it means the file describes
    the template rather than the project. Rewritten on every spec change,
    because a README documenting turn 1 is worse than none by turn 3.

    Uses the REAL scaffold README, not a stand-in for it: `_write_readme` only
    overwrites a README carrying `README_MARKER` (so D1's adopted projects keep
    their hand-written ones), which an approximation of the shipped file cannot
    prove anything about.
    """
    from app.agent.core import AgentCore
    from app.agent.projectspec import README_MARKER
    from app.agent.scaffold import scaffold_flask

    monkeypatch.chdir(tmp_path)
    scaffold_flask(tmp_path, "Bookshop")
    assert "Run it" in (tmp_path / "README.md").read_text(encoding="utf-8")
    agent = AgentCore(session_id="pytest_readme")

    agent._write_readme(tmp_path, _shop_spec())

    text = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "# bookshop" in text and "`products`" in text
    assert README_MARKER in text  # generated from the spec, not the template


def test_writing_the_readme_never_raises(tmp_path, monkeypatch):
    """Best-effort: a README that won't write must not cost a built turn."""
    from app.agent.core import AgentCore

    monkeypatch.chdir(tmp_path)
    agent = AgentCore(session_id="pytest_readme_fail")
    (tmp_path / "README.md").mkdir()  # a directory where the file should go

    agent._write_readme(tmp_path, _shop_spec())  # must not raise
