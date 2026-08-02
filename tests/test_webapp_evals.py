"""Multi-turn eval harness + webapp checks (Phase 7). Fully offline.

The harness change is the load-bearing part: a task is now a CONVERSATION, run
against one workdir with one agent. A suite that rebuilds each task from scratch
cannot measure the only thing that was ever really complained about — whether
turn 3 breaks what turn 1 built.
"""

import sqlite3
import textwrap
from pathlib import Path

import pytest

from evals.checks import (
    app_serves,
    db_has_column,
    earlier_pages_still_work,
    post_persists,
    spec_has_endpoint,
    spec_has_entity,
)
from evals.harness import CheckContext, EvalTask, run_task
from evals.tasks import WEBAPP_TASKS


class _RecordingAgent:
    """Records every prompt it is asked, and what cwd it saw."""

    def __init__(self, answers=None):
        self.prompts: list[str] = []
        self.cwds: list[str] = []
        self._answers = list(answers or [])
        self.instances = 1

    async def chat(self, prompt):
        import os

        self.prompts.append(prompt)
        self.cwds.append(os.getcwd())
        answer = self._answers.pop(0) if self._answers else f"did: {prompt}"
        return answer, [{"tool": "write_file", "result": {"success": True}}]


class _ExplodingAgent:
    def __init__(self, fail_on: int):
        self.fail_on = fail_on
        self.prompts: list[str] = []

    async def chat(self, prompt):
        self.prompts.append(prompt)
        if len(self.prompts) == self.fail_on:
            raise RuntimeError("boom")
        return "ok", []


# ---------------------------------------------------------------------------
# The harness change
# ---------------------------------------------------------------------------


async def test_every_turn_runs_in_order_against_one_workdir(tmp_path):
    agent = _RecordingAgent()
    task = EvalTask(
        id="multi",
        prompts=["build a shop", "add a cart", "add search"],
        checks=[lambda ctx: (True, "ok")],
    )

    result = await run_task(agent, task, tmp_path)

    assert agent.prompts == ["build a shop", "add a cart", "add search"]
    assert len(set(agent.cwds)) == 1  # one workdir for the whole conversation
    assert result.passed


async def test_a_single_prompt_task_is_untouched(tmp_path):
    """All ~14 existing tasks must keep working exactly as before."""
    agent = _RecordingAgent()
    task = EvalTask(id="single", prompt="make a page", checks=[])

    await run_task(agent, task, tmp_path)

    assert agent.prompts == ["make a page"]


async def test_checks_run_only_after_the_last_turn(tmp_path):
    seen: list[str] = []

    def spy(ctx: CheckContext):
        seen.append(ctx.answer)
        return True, "ok"

    agent = _RecordingAgent(answers=["first", "second", "third"])
    task = EvalTask(id="m", prompts=["a", "b", "c"], checks=[spy])

    await run_task(agent, task, tmp_path)

    assert seen == ["third"]  # once, with the final answer


async def test_every_answer_is_available_to_a_check(tmp_path):
    captured = {}

    def spy(ctx: CheckContext):
        captured["answers"] = list(ctx.answers)
        return True, "ok"

    agent = _RecordingAgent(answers=["one", "two"])
    await run_task(
        _RecordingAgent(answers=["one", "two"]),
        EvalTask(id="m", prompts=["a", "b"], checks=[spy]),
        tmp_path,
    )

    assert captured["answers"] == ["one", "two"]


async def test_a_failing_turn_stops_the_conversation(tmp_path):
    """Later turns would be measuring the wrong thing."""
    agent = _ExplodingAgent(fail_on=2)
    task = EvalTask(id="m", prompts=["a", "b", "c"], checks=[])

    result = await run_task(agent, task, tmp_path)

    assert result.passed is False
    assert "turn 2 raised RuntimeError" in result.details[0]
    assert agent.prompts == ["a", "b"]  # "c" never ran


# ---------------------------------------------------------------------------
# The webapp checks
# ---------------------------------------------------------------------------


def _ctx(tmp_path) -> CheckContext:
    return CheckContext(answer="", trace=[], workdir=tmp_path)


def _save_spec(tmp_path, **kwargs):
    from app.agent.projectspec import Entity, Field, ProjectSpec, SpecEndpoint

    spec = ProjectSpec(
        name="shop",
        entities=(Entity("product", "products", (Field("id", "INTEGER", pk=True),)),),
        endpoints=(SpecEndpoint("POST", "/admin/products", entity="product"),),
        **kwargs,
    )
    spec.save(tmp_path)
    return spec


def test_spec_has_entity_reads_the_persisted_spec(tmp_path):
    _save_spec(tmp_path)
    ok, detail = spec_has_entity("product")(_ctx(tmp_path))
    assert ok, detail

    ok, detail = spec_has_entity("cart")(_ctx(tmp_path))
    assert not ok and "product" in detail  # says what IS there


def test_spec_checks_fail_clearly_with_no_spec(tmp_path):
    ok, detail = spec_has_entity("product")(_ctx(tmp_path))
    assert not ok and "no project spec" in detail


def test_spec_has_endpoint_matches_method_and_fragment(tmp_path):
    _save_spec(tmp_path)
    assert spec_has_endpoint("POST", "/admin")(_ctx(tmp_path))[0]
    assert not spec_has_endpoint("GET", "/admin")(_ctx(tmp_path))[0]
    ok, detail = spec_has_endpoint("POST", "/nope")(_ctx(tmp_path))
    assert not ok and "POST /admin/products" in detail


def test_db_has_column_asks_the_database_not_the_source(tmp_path):
    """A CREATE TABLE in a file nobody executes proves nothing."""
    conn = sqlite3.connect(tmp_path / "app.db")
    conn.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, title TEXT)")
    conn.commit()
    conn.close()

    assert db_has_column("products", "title")(_ctx(tmp_path))[0]

    ok, detail = db_has_column("products", "image_path")(_ctx(tmp_path))
    assert not ok
    assert "title" in detail  # reports what the table actually has


def test_db_has_column_without_a_database(tmp_path):
    ok, detail = db_has_column("products", "title")(_ctx(tmp_path))
    assert not ok and "no .db file" in detail


_TINY_APP = """
from http.server import BaseHTTPRequestHandler, HTTPServer


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/" else 404)
        self.end_headers()
        self.wfile.write(b"<html><body>ok</body></html>")

    def log_message(self, *a):
        pass


HTTPServer(("127.0.0.1", 5000), H).serve_forever()
"""


def test_app_serves_runs_the_real_app(tmp_path):
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        if sock.connect_ex(("127.0.0.1", 5000)) == 0:
            pytest.skip("port 5000 in use")

    (tmp_path / "app.py").write_text(textwrap.dedent(_TINY_APP), encoding="utf-8")

    ok, detail = app_serves(["/"])(_ctx(tmp_path))
    assert ok, detail

    ok, detail = earlier_pages_still_work(["/", "/gone"])(_ctx(tmp_path))
    assert not ok
    assert "/gone -> 404" in detail  # names the page that regressed


def test_app_serves_without_an_app(tmp_path):
    ok, detail = app_serves(["/"])(_ctx(tmp_path))
    assert not ok and "no app.py" in detail


# ---------------------------------------------------------------------------
# The suite itself
# ---------------------------------------------------------------------------


def test_the_webapp_suite_mirrors_the_demo():
    """The demo turns are the spine of the suite and must stay in order.

    Asserted by containment, not equality: Phase E added request-shape tasks
    alongside these, and pinning the exact list would make every future task an
    edit to this test rather than a decision about the suite.
    """
    demo = ["web_turn1_build", "web_turn2_amend", "web_turn3_cart", "web_turn4_search"]
    ids = [t.id for t in WEBAPP_TASKS]
    assert [i for i in ids if i in demo] == demo

    by_id = {t.id: t for t in WEBAPP_TASKS}
    # Each demo task is the whole conversation up to its turn.
    assert [len(by_id[i].turns()) for i in demo] == [1, 2, 3, 4]
    assert by_id["web_turn1_build"].turns()[0].startswith("build me an e-commerce")


def test_every_multi_turn_task_checks_that_turn_1_survived():
    """The headline number — it is not "did turn 3 work" but "did turn 3 break
    turn 1". Every task that runs more than one turn has to ask it."""
    multi = [t for t in WEBAPP_TASKS if len(t.turns()) > 1]
    assert len(multi) >= 4  # the three demo amendments + Phase E's off-list one
    for task in multi:
        names = [getattr(c, "__qualname__", "") for c in task.checks]
        assert any("app_serves" in n or "check" in n for n in names), task.id


def test_phase_e_covers_several_request_shapes():
    """One task per shape, all asserting the same three spec-driven checks — and
    one whose wording is deliberately outside `_BLUEPRINT_NOUN_RE`, which is the
    Phase B regression test."""
    shapes = [t for t in WEBAPP_TASKS if t.id.startswith("web_shape_")]
    assert len(shapes) >= 4

    off_list = next(t for t in WEBAPP_TASKS if t.id == "web_shape_offlist")
    from app.agent.blueprint import may_be_web_build, should_blueprint

    prompt = off_list.turns()[0]
    assert should_blueprint(prompt) is False  # tier 1 cannot see it…
    assert may_be_web_build(prompt) is True  # …and tier 2 is what catches it

    # Every shape task asserts a real server, a real schema and a usable app.
    for task in shapes:
        names = [getattr(c, "__qualname__", "") for c in task.checks]
        assert len(names) >= 3, task.id


def test_run_py_exposes_the_webapp_flag():
    import evals.run as run_mod

    source = Path(run_mod.__file__).read_text(encoding="utf-8")
    assert "--webapp" in source
    assert "WEBAPP_TASKS" in source
