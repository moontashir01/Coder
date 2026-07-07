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


@pytest.mark.parametrize(
    "msg",
    [
        # eval-driven: explicit multi-file CREATE ("create three files: a, b, c")
        "Create three files: index.html, styles.css and script.js",
        "make index.html, styles.css and app.js",
        "generate two separate files for the frontend",
        "build a webpage using multiple files",
        "create several files for the project",
    ],
)
def test_wants_multifile_true_explicit_create(msg):
    assert wants_multifile(msg) is True


@pytest.mark.parametrize(
    "msg",
    [
        # ordinary single-file creates must NOT be pulled into the multi-file flow
        "create an index.html file for a landing page",
        "make me a styles.css file",
        "write a config.json file with a version key",
        "what is the difference between a.py and b.py",
    ],
)
def test_wants_multifile_false_single_create(msg):
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


async def test_chat_routes_to_multifile(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "index.html").write_text(
        "<html><style>a{}</style></html>\n", encoding="utf-8"
    )
    a = AgentCore(session_id="pytest_chat_multi")

    # classify() must not need a live LLM — force a harmless task type.
    monkeypatch.setattr(a.planner, "classify", lambda msg: "file_edit")

    called = {}

    async def fake_multi(message, refs):
        called["message"] = message
        called["refs"] = refs
        return "multi done", [{"tool": "write_file"}]

    monkeypatch.setattr(a, "_multi_file_flow", fake_multi)

    answer, trace = await a.chat("separate index.html into separate files")

    assert answer == "multi done"
    assert called["message"] == "separate index.html into separate files"


async def test_chat_single_file_still_uses_file_op(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_chat_single")
    monkeypatch.setattr(a.planner, "classify", lambda msg: "file_edit")

    multi_called = {"hit": False}

    async def fake_multi(message, refs):
        multi_called["hit"] = True
        return "x", []

    monkeypatch.setattr(a, "_multi_file_flow", fake_multi)
    a._llm_edit = ScriptedLLM(["no blocks"])
    a._llm_direct = ScriptedLLM(["FILENAME: index.html\n<html>new</html>"])

    await a.chat("make me an index.html file")

    assert multi_called["hit"] is False  # single-file requests skip the multi flow


# ---------------------------------------------------------------------------
# Cross-file consistency (roadmap Tier 1 #3): manifest + sibling context
# ---------------------------------------------------------------------------


class RecordingLLM(ScriptedLLM):
    """ScriptedLLM that also records the full prompt text of every call."""

    def __init__(self, outputs):
        super().__init__(outputs)
        self.prompts: list[str] = []

    def invoke(self, messages):
        self.prompts.append("\n".join(str(getattr(m, "content", m)) for m in messages))
        return super().invoke(messages)


async def test_file_op_flow_injects_extra_context(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_extra_ctx")
    a._llm_direct = RecordingLLM(["FILENAME: a.css\nbody{color:blue}"])

    await a._file_op_flow(
        "make a.css", target="a.css", extra_context="MANIFEST-MARKER-42"
    )

    assert "MANIFEST-MARKER-42" in a._llm_direct.prompts[0]


async def test_multi_file_flow_manifest_and_sibling_context(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_multi_consistency")

    a._llm_direct = RecordingLLM(
        [
            # call 0: the plan
            '{"files": ['
            '{"filename": "a.css", "action": "create", "instruction": "site styles"},'
            '{"filename": "b.html", "action": "create", "instruction": "page linking a.css"}'
            "]}",
            # call 1: generate a.css
            "FILENAME: a.css\nbody{color:blue}",
            # call 2: generate b.html (balanced so verify does not fire)
            'FILENAME: b.html\n<html><head><link rel="stylesheet" href="a.css">'
            "</head><body><p>x</p></body></html>",
        ]
    )

    answer, trace = await a._multi_file_flow(
        "split the site into css and html files", refs=[]
    )

    # Both files written
    assert (tmp_path / "a.css").read_text(encoding="utf-8") == "body{color:blue}"
    assert "a.css" in (tmp_path / "b.html").read_text(encoding="utf-8")

    # First generation call sees the full plan manifest (knows b.html is coming)
    assert "b.html" in a._llm_direct.prompts[1]
    # Second generation call sees the already-generated sibling's CONTENT
    assert "body{color:blue}" in a._llm_direct.prompts[2]
    assert "a.css" in a._llm_direct.prompts[2]


async def test_multi_file_flow_edit_sees_generated_siblings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    index = tmp_path / "index.html"
    index.write_text(
        "<html><head><style>body{color:red}</style></head><body></body></html>",
        encoding="utf-8",
    )

    a = AgentCore(session_id="pytest_multi_edit_ctx")
    a._llm_direct = RecordingLLM(
        [
            # plan: create styles.css, then edit index.html
            '{"files": ['
            '{"filename": "styles.css", "action": "create", "instruction": "move css"},'
            '{"filename": "index.html", "action": "edit", "instruction": "link styles.css"}'
            "]}",
            # generate styles.css — content deliberately DIFFERENT from the
            # inline <style> so the assertion below can only be satisfied by
            # the sibling-context injection, not by index.html's own body.
            "FILENAME: styles.css\nbody{color:blue;font-size:14px}",
        ]
    )
    a._llm_edit = RecordingLLM(
        [
            "<<<<<<< SEARCH\n<style>body{color:red}</style>\n=======\n"
            '<link rel="stylesheet" href="styles.css">\n>>>>>>> REPLACE'
        ]
    )

    await a._multi_file_flow("separate the css into its own file", refs=["index.html"])

    # The surgical edit prompt for index.html must include the sibling's content
    assert "body{color:blue;font-size:14px}" in a._llm_edit.prompts[0]
    assert "styles.css" in a._llm_edit.prompts[0]
    # And the edit actually landed
    assert 'href="styles.css"' in index.read_text(encoding="utf-8")
