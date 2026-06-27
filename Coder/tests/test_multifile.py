"""Tests for multi-file planning, the extension guard, and multi-file orchestration.

All offline: the LLM is a scripted fake, file writes go to tmp_path.
"""

from types import SimpleNamespace

import pytest

from app.agent.core import (AgentCore, FileOp, _extension_guard,
                            _parse_file_plan, wants_multifile)


class ScriptedLLM:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    def invoke(self, messages):
        out = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        return SimpleNamespace(content=out)


def test_extension_guard_css():
    g = _extension_guard("styles.css")
    assert "CSS" in g
    assert "JavaScript" in g or "JS" in g  # tells the model NOT to emit JS


def test_extension_guard_js():
    g = _extension_guard("script.js")
    assert "JavaScript" in g


def test_extension_guard_unknown_is_empty():
    assert _extension_guard("notes.txt") == ""


@pytest.mark.parametrize(
    "msg",
    [
        "separate the html, css and js into separate files",
        "split index.html into separate files",
        "extract the styles and scripts into their own files",
        "move the css and javascript out of index.html into separate files",
    ],
)
def test_wants_multifile_true(msg):
    assert wants_multifile(msg) is True


@pytest.mark.parametrize(
    "msg",
    [
        "make me an index.html file",  # single-file create
        "edit index.html to change the title",  # single-file edit
        "write a python function that adds two numbers",
        "explain what a decorator does",
    ],
)
def test_wants_multifile_false(msg):
    assert wants_multifile(msg) is False


def test_parse_file_plan_basic():
    raw = """{"files": [
        {"filename": "styles.css", "action": "create", "instruction": "move the css here"},
        {"filename": "index.html", "action": "edit", "instruction": "remove inline css, link styles.css"}
    ]}"""
    ops = _parse_file_plan(raw)
    assert ops == [
        FileOp(filename="styles.css", action="create", instruction="move the css here"),
        FileOp(
            filename="index.html",
            action="edit",
            instruction="remove inline css, link styles.css",
        ),
    ]


def test_parse_file_plan_tolerates_surrounding_prose():
    raw = 'Here is the plan:\n{"files": [{"filename": "a.js", "action": "create", "instruction": "x"}]}\nDone.'
    ops = _parse_file_plan(raw)
    assert ops == [FileOp(filename="a.js", action="create", instruction="x")]


def test_parse_file_plan_defaults_action_to_create():
    raw = '{"files": [{"filename": "new.css", "instruction": "styles"}]}'
    ops = _parse_file_plan(raw)
    assert ops == [FileOp(filename="new.css", action="create", instruction="styles")]


def test_parse_file_plan_skips_entries_without_filename():
    raw = '{"files": [{"action": "create", "instruction": "no name"}, {"filename": "ok.js", "instruction": "y"}]}'
    ops = _parse_file_plan(raw)
    assert ops == [FileOp(filename="ok.js", action="create", instruction="y")]


def test_parse_file_plan_empty_on_garbage():
    assert _parse_file_plan("not json at all") == []


async def test_plan_file_ops_parses_scripted_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_plan")
    a._llm_direct = ScriptedLLM(["""{"files": [
        {"filename": "styles.css", "action": "create", "instruction": "the css"},
        {"filename": "script.js", "action": "create", "instruction": "the js"},
        {"filename": "index.html", "action": "edit", "instruction": "link them, drop inline"}
    ]}"""])

    ops = await a._plan_file_ops(
        "separate index.html into files",
        context="### index.html\n<html><style>x</style></html>",
    )

    assert [o.filename for o in ops] == ["styles.css", "script.js", "index.html"]
    assert [o.action for o in ops] == ["create", "create", "edit"]


async def test_plan_file_ops_empty_on_llm_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_plan_err")

    class Boom:
        def invoke(self, messages):
            raise RuntimeError("offline")

    a._llm_direct = Boom()
    assert await a._plan_file_ops("separate it", context="") == []


async def test_multi_file_flow_separates_html(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    index = tmp_path / "index.html"
    index.write_text(
        "<html>\n<style>body{color:red}</style>\n"
        "<script>console.log(1)</script>\n</html>\n",
        encoding="utf-8",
    )

    a = AgentCore(session_id="pytest_multi")

    # 1st _llm_direct call = the PLAN. Subsequent _llm_direct calls = whole-file
    # generations for the two new files (surgical is not used for new files).
    a._llm_direct = ScriptedLLM(
        [
            # plan
            '{"files": ['
            '{"filename": "styles.css", "action": "create", "instruction": "move css"},'
            '{"filename": "script.js", "action": "create", "instruction": "move js"},'
            '{"filename": "index.html", "action": "edit", "instruction": "drop inline, link both"}'
            "]}",
            # create styles.css
            "FILENAME: styles.css\nbody{color:red}",
            # create script.js
            "FILENAME: script.js\nconsole.log(1)",
            # whole-file fallback for index.html IF surgical yields no blocks
            'FILENAME: index.html\n<html>\n<link rel="stylesheet" href="styles.css">\n'
            '<script src="script.js"></script>\n</html>',
        ]
    )
    # Surgical edit on index.html: strip the two inline blocks + add links.
    a._llm_edit = ScriptedLLM(
        [
            "<<<<<<< SEARCH\n<style>body{color:red}</style>\n=======\n"
            '<link rel="stylesheet" href="styles.css">\n>>>>>>> REPLACE\n'
            "<<<<<<< SEARCH\n<script>console.log(1)</script>\n=======\n"
            '<script src="script.js"></script>\n>>>>>>> REPLACE'
        ]
    )

    answer, trace = await a._multi_file_flow(
        "separate index.html into html, css and js files", refs=["index.html"]
    )

    # New files created with the right content
    assert (tmp_path / "styles.css").read_text(encoding="utf-8") == "body{color:red}"
    assert (tmp_path / "script.js").read_text(encoding="utf-8") == "console.log(1)"
    # index.html no longer holds the inline code
    html = index.read_text(encoding="utf-8")
    assert "color:red" not in html
    assert "console.log(1)" not in html
    assert 'href="styles.css"' in html
    assert 'src="script.js"' in html
    # one trace entry per planned file
    assert len(trace) == 3
    assert "3" in answer  # mentions 3 files handled


async def test_multi_file_flow_empty_plan_falls_back(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_multi_fallback")
    # planner returns junk → no ops → flow signals fallback with (None-ish) answer
    a._llm_direct = ScriptedLLM(["not a plan"])
    answer, trace = await a._multi_file_flow("separate things", refs=[])
    assert trace == []
    assert "couldn't" in answer.lower() or "could not" in answer.lower()
