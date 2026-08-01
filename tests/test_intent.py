"""Tests for the intent check — "is this file what the user asked for?"

Two halves, matching the module split: the parsing/filtering in
`app/agent/intent.py` is pure and tested directly, and the repair stage in
`AgentCore` is driven by a scripted LLM against tmp_path. Fully offline.

The bias under test is as important as the feature: nearly every ambiguous case
must resolve to "leave the file alone", because the judge is the same small
model that wrote the file.
"""

from types import SimpleNamespace

import pytest

from app.agent.core import AgentCore
from app.agent.intent import (
    build_judge_prompt,
    build_repair_prompt,
    filter_complaints,
    parse_verdict,
    should_check_intent,
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


# ---------------------------------------------------------------------------
# parse_verdict — every unreadable answer must mean PASS
# ---------------------------------------------------------------------------


def test_parse_plain_pass():
    assert parse_verdict("PASS") == []


@pytest.mark.parametrize(
    "raw",
    [
        "pass",
        "  PASS.  ",
        "**PASS**",
        "PASS\n\nThe file implements the login form as requested.",
    ],
)
def test_parse_pass_variants(raw):
    assert parse_verdict(raw) == []


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "I think the file looks quite good overall.",
        '{"verdict": "unclear"}',
        "Here is my analysis of the file:\nIt has a header and a footer.",
    ],
)
def test_unreadable_verdict_is_a_pass(raw):
    """A verdict we cannot read is not evidence of a defect."""
    assert parse_verdict(raw) == []


def test_parse_missing_list():
    raw = "MISSING:\n- a password input\n- a submit button\n"
    assert parse_verdict(raw) == ["a password input", "a submit button"]


def test_parse_missing_same_line_and_numbered():
    assert parse_verdict("MISSING: a password input") == ["a password input"]
    assert parse_verdict("MISSING:\n1. a password input\n2) a submit button") == [
        "a password input",
        "a submit button",
    ]


def test_parse_stops_at_trailing_commentary():
    raw = (
        "MISSING:\n"
        "- a password input\n"
        "\n"
        "Overall the file is well structured and uses semantic HTML.\n"
        "- this trailing bullet is part of the commentary\n"
    )
    assert parse_verdict(raw) == ["a password input"]


def test_parse_strips_fences_and_dedupes():
    raw = "```\nMISSING:\n- a password input\n- A password input.\n```"
    assert parse_verdict(raw) == ["a password input"]


def test_parse_caps_complaint_count():
    raw = "MISSING:\n" + "\n".join(f"- item number {i}" for i in range(20))
    assert len(parse_verdict(raw)) == 5


def test_parse_drops_paragraph_length_complaint():
    raw = "MISSING:\n- " + "x" * 300
    assert parse_verdict(raw) == []


# ---------------------------------------------------------------------------
# filter_complaints — the deterministic gates
# ---------------------------------------------------------------------------

_LOGIN_HTML = (
    "<!DOCTYPE html><html><body><form>"
    '<input type="text" name="username">'
    '<input type="password" name="password">'
    '<button type="submit">Log in</button>'
    "</form></body></html>"
)


def test_real_missing_requirement_survives():
    kept = filter_complaints(["no remember me checkbox"], _LOGIN_HTML, "login.html")
    assert kept == ["no remember me checkbox"]


def test_suggestion_is_dropped():
    complaints = [
        "could also add a footer",
        "consider using semantic elements",
        "the markup would be better with comments",
    ]
    assert filter_complaints(complaints, _LOGIN_HTML, "login.html") == []


def test_complaint_about_another_file_is_dropped():
    """_repair_dead_references owns missing siblings; a rewrite can't fix them."""
    complaints = ["styles.css is not present", "no script.js in the project"]
    assert filter_complaints(complaints, _LOGIN_HTML, "login.html") == []


def test_complaint_naming_this_file_is_kept():
    kept = filter_complaints(
        ["login.html has no remember-me checkbox"], _LOGIN_HTML, "login.html"
    )
    assert kept


def test_already_satisfied_complaint_is_dropped():
    """The characteristic false alarm: it reports what it just read as absent."""
    assert filter_complaints(["no password input"], _LOGIN_HTML, "login.html") == []


def test_vague_complaint_is_dropped():
    assert (
        filter_complaints(["it does not have that", "no"], _LOGIN_HTML, "x.html") == []
    )


# ---------------------------------------------------------------------------
# should_check_intent / prompt construction
# ---------------------------------------------------------------------------


def test_short_or_empty_request_is_not_judged():
    assert not should_check_intent("", "index.html")
    assert not should_check_intent("fix it", "index.html")
    assert should_check_intent("make a login page with a password field", "index.html")


def test_judge_prompt_contains_request_and_content():
    prompt = build_judge_prompt("make a login page", "login.html", _LOGIN_HTML)
    assert "make a login page" in prompt
    assert "password" in prompt
    assert "login.html" in prompt
    assert "PASS" in prompt


def test_judge_prompt_truncates_huge_build_context():
    prompt = build_judge_prompt("make a login page", "login.html", "x", "C" * 50_000)
    assert len(prompt) < 5_000


def test_repair_prompt_names_the_unmet_items_and_forbids_removal():
    prompt = build_repair_prompt(
        "make a login page", "login.html", _LOGIN_HTML, ["no remember me checkbox"]
    )
    assert "no remember me checkbox" in prompt
    assert "Do not remove existing content" in prompt


# ---------------------------------------------------------------------------
# _intent_repair — the AgentCore stage
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _intent_on(monkeypatch):
    monkeypatch.setattr(settings, "check_intent", True)
    monkeypatch.setattr(settings, "max_intent_repairs", 1)


async def test_pass_verdict_leaves_the_file_untouched(tmp_path):
    page = tmp_path / "login.html"
    page.write_text(_LOGIN_HTML, encoding="utf-8")
    a = AgentCore(session_id="pytest_intent_pass")
    a._llm_edit = ScriptedLLM(["PASS"])
    a._llm_direct = ScriptedLLM(["should not be called"])

    note, trace = await a._intent_repair(page, "login.html", "make a login page")

    assert note == "intent OK"
    assert trace == []
    assert a._llm_direct.calls == 0  # no rewrite
    assert page.read_text(encoding="utf-8") == _LOGIN_HTML


async def test_missing_requirement_triggers_a_regeneration(tmp_path):
    page = tmp_path / "login.html"
    page.write_text(_LOGIN_HTML, encoding="utf-8")
    fixed = _LOGIN_HTML.replace(
        "</form>", '<input type="checkbox" name="remember">Remember me</form>'
    )
    a = AgentCore(session_id="pytest_intent_fix")
    # judge: complains, then passes the rewrite
    a._llm_edit = ScriptedLLM(["MISSING:\n- no remember me checkbox", "PASS"])
    a._llm_direct = ScriptedLLM([f"FILENAME: login.html\n{fixed}"])

    note, trace = await a._intent_repair(
        page, "login.html", "make a login page with a remember me checkbox"
    )

    assert "intent-repaired after 1 attempt(s)" == note
    assert 'name="remember"' in page.read_text(encoding="utf-8")
    assert trace and trace[0]["tool"] == "write_file"


async def test_rewrite_that_breaks_syntax_is_reverted(tmp_path):
    """The safety property: intent repair never leaves a file broken."""
    page = tmp_path / "login.html"
    page.write_text(_LOGIN_HTML, encoding="utf-8")
    a = AgentCore(session_id="pytest_intent_revert")
    a._llm_edit = ScriptedLLM(["MISSING:\n- no remember me checkbox"])
    # A rewrite that satisfies the complaint but leaves <div> unclosed.
    a._llm_direct = ScriptedLLM(
        ["FILENAME: login.html\n<html><body><div><input name='remember'>"]
    )

    note, trace = await a._intent_repair(page, "login.html", "make a login page")

    assert "reverted" in note
    assert page.read_text(encoding="utf-8") == _LOGIN_HTML  # original restored
    assert len(trace) == 2  # the rewrite, then the revert


async def test_unfixed_requirement_is_reported_not_hidden(tmp_path):
    page = tmp_path / "login.html"
    page.write_text(_LOGIN_HTML, encoding="utf-8")
    other = _LOGIN_HTML.replace("Log in", "Sign in")
    a = AgentCore(session_id="pytest_intent_unfixed")
    a._llm_edit = ScriptedLLM(["MISSING:\n- no remember me checkbox"])  # always
    a._llm_direct = ScriptedLLM([f"FILENAME: login.html\n{other}"])

    note, _ = await a._intent_repair(page, "login.html", "make a login page")

    assert note.startswith("may not meet:")
    assert "remember me checkbox" in note


async def test_disabled_setting_spends_no_call(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "check_intent", False)
    page = tmp_path / "login.html"
    page.write_text(_LOGIN_HTML, encoding="utf-8")
    a = AgentCore(session_id="pytest_intent_off")
    a._llm_edit = ScriptedLLM(["MISSING:\n- anything"])

    note, trace = await a._intent_repair(page, "login.html", "make a login page")

    assert (note, trace) == ("", [])
    assert a._llm_edit.calls == 0


async def test_judge_llm_error_is_non_fatal(tmp_path):
    page = tmp_path / "login.html"
    page.write_text(_LOGIN_HTML, encoding="utf-8")

    class Boom:
        calls = 0

        def invoke(self, messages):
            raise RuntimeError("ollama down")

    a = AgentCore(session_id="pytest_intent_boom")
    a._llm_edit = Boom()

    note, trace = await a._intent_repair(page, "login.html", "make a login page")

    assert note == "intent OK"  # silence is the safe answer
    assert page.read_text(encoding="utf-8") == _LOGIN_HTML


async def test_broken_syntax_skips_the_judge_entirely(tmp_path, monkeypatch):
    """Stage 2 must not spend a call on a file stage 1 could not fix."""
    monkeypatch.setattr(settings, "max_repair_attempts", 1)
    page = tmp_path / "broken.py"
    page.write_text("def f(:\n", encoding="utf-8")
    a = AgentCore(session_id="pytest_intent_after_syntax")
    a._llm_direct = ScriptedLLM(["FILENAME: broken.py\ndef f(:\n"])  # still broken
    a._llm_edit = ScriptedLLM(["MISSING:\n- should never be asked"])

    note, _ = await a._verify_and_repair(page, "broken.py", "write a function f")

    assert note.startswith("verification failed")
    assert a._llm_edit.calls == 0


async def test_verify_and_repair_combines_both_notes(tmp_path):
    page = tmp_path / "app.py"
    page.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    a = AgentCore(session_id="pytest_intent_combined")
    a._llm_edit = ScriptedLLM(["PASS"])

    note, _ = await a._verify_and_repair(
        page, "app.py", "write a function that adds two numbers"
    )

    assert note == "verified OK; intent OK"
