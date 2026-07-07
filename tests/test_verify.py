"""Tests for the verify-and-repair loop (roadmap Tier 1 #1).

All offline: syntax checks run locally (in-process compile / node if present),
the LLM is a scripted fake, file writes go to tmp_path.
"""

import shutil
from types import SimpleNamespace

import pytest

from app.agent.core import AgentCore
from app.agent.verify import check_file


class ScriptedLLM:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    def invoke(self, messages):
        out = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        return SimpleNamespace(content=out)


# ---------------------------------------------------------------------------
# check_file — per-extension syntax/structure checks
# ---------------------------------------------------------------------------


def test_check_file_python_valid(tmp_path):
    p = tmp_path / "good.py"
    p.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    ok, err = check_file(p)
    assert ok is True
    assert err == ""


def test_check_file_python_syntax_error(tmp_path):
    p = tmp_path / "bad.py"
    p.write_text("def add(a, b:\n    return a + b\n", encoding="utf-8")
    ok, err = check_file(p)
    assert ok is False
    assert "bad.py" in err or "Syntax" in err or "syntax" in err


def test_check_file_html_balanced(tmp_path):
    p = tmp_path / "good.html"
    p.write_text(
        "<!DOCTYPE html>\n<html><head><title>x</title></head>"
        "<body><div><p>hi</p><br><img src='x.png'></div></body></html>",
        encoding="utf-8",
    )
    ok, err = check_file(p)
    assert ok is True


def test_check_file_html_unclosed_tag(tmp_path):
    p = tmp_path / "bad.html"
    p.write_text("<html><body><div><p>hi</p></body></html>", encoding="utf-8")
    ok, err = check_file(p)
    assert ok is False
    assert "div" in err


def test_check_file_html_stray_closing_tag(tmp_path):
    p = tmp_path / "stray.html"
    p.write_text("<html><body><p>hi</p></div></body></html>", encoding="utf-8")
    ok, err = check_file(p)
    assert ok is False
    assert "div" in err


def test_check_file_unknown_extension_skips(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("anything { at all", encoding="utf-8")
    ok, err = check_file(p)
    assert ok is True
    assert err == ""


def test_check_file_missing_file(tmp_path):
    ok, err = check_file(tmp_path / "ghost.py")
    assert ok is False


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_check_file_js_valid(tmp_path):
    p = tmp_path / "good.js"
    p.write_text("function add(a, b) { return a + b; }\n", encoding="utf-8")
    ok, err = check_file(p)
    assert ok is True


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_check_file_js_syntax_error(tmp_path):
    p = tmp_path / "bad.js"
    p.write_text("function add(a, b { return a + b; }\n", encoding="utf-8")
    ok, err = check_file(p)
    assert ok is False
    assert err


def test_check_file_js_skips_without_node(tmp_path, monkeypatch):
    # Checker binary missing → treated as unverifiable, not as a failure.
    monkeypatch.setattr("app.agent.verify.shutil.which", lambda name: None)
    p = tmp_path / "any.js"
    p.write_text("function ( broken", encoding="utf-8")
    ok, err = check_file(p)
    assert ok is True
    assert err == ""


# ---------------------------------------------------------------------------
# _file_op_flow verify-and-repair integration
# ---------------------------------------------------------------------------


async def test_file_op_flow_repairs_broken_python(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_verify_repair")
    a._llm_direct = ScriptedLLM(
        [
            # 1st call: generation → broken file
            "FILENAME: calc.py\ndef add(a, b:\n    return a + b",
            # 2nd call: repair → fixed file
            "FILENAME: calc.py\ndef add(a, b):\n    return a + b",
        ]
    )

    answer, trace = await a._file_op_flow("make a calc.py with an add function")

    body = (tmp_path / "calc.py").read_text(encoding="utf-8")
    assert "def add(a, b):" in body  # repaired content on disk
    assert a._llm_direct.calls == 2  # generate + one repair
    assert len(trace) == 2  # two write_file entries
    assert "repair" in answer.lower()


async def test_file_op_flow_verified_clean_needs_no_repair(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_verify_clean")
    a._llm_direct = ScriptedLLM(["FILENAME: ok.py\nx = 1\n"])

    answer, trace = await a._file_op_flow("make ok.py setting x to 1")

    assert a._llm_direct.calls == 1  # no repair call
    assert len(trace) == 1
    assert "verified" in answer.lower()


async def test_file_op_flow_reports_unfixable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_verify_giveup")
    # Every attempt returns the same broken file.
    a._llm_direct = ScriptedLLM(["FILENAME: bad.py\ndef broken(:\n    pass"])

    from config.settings import settings

    answer, trace = await a._file_op_flow("make bad.py")

    # generation + capped repair attempts, then give up with the error surfaced
    assert a._llm_direct.calls == 1 + settings.max_repair_attempts
    assert "fail" in answer.lower() or "error" in answer.lower()


async def test_file_op_flow_no_verify_for_unknown_ext(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_verify_txt")
    a._llm_direct = ScriptedLLM(["FILENAME: notes.txt\nhello { world"])

    answer, trace = await a._file_op_flow("make a notes.txt file")

    assert a._llm_direct.calls == 1
    assert len(trace) == 1
    assert "verified" not in answer.lower()


async def test_surgical_edit_repairs_broken_result(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "app.py"
    target.write_text("def greet():\n    return 'hi'\n", encoding="utf-8")

    a = AgentCore(session_id="pytest_verify_surgical")
    # Surgical edit produces a syntax error (dropped colon)…
    a._llm_edit = ScriptedLLM(
        [
            "<<<<<<< SEARCH\ndef greet():\n=======\ndef greet(name)\n>>>>>>> REPLACE",
        ]
    )
    # …then the repair pass fixes it.
    a._llm_direct = ScriptedLLM(
        ["FILENAME: app.py\ndef greet(name):\n    return 'hi'\n"]
    )

    answer, trace = await a._file_op_flow(
        "edit app.py to take a name parameter", target="app.py"
    )

    body = target.read_text(encoding="utf-8")
    assert "def greet(name):" in body
    assert "repair" in answer.lower()
