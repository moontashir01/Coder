"""Tests for file-creation routing and the file_op flow.

All offline: the LLM is a scripted fake, file writes go to tmp_path.
"""

import os
from types import SimpleNamespace

import pytest

from app.agent import core as core_mod
from app.agent.core import (
    AgentCore,
    _apply_search_replace,
    _extract_at_refs,
    _extract_filename,
    _infer_filename,
    _parse_file_output,
    _parse_search_replace,
    _strip_at_refs,
    _strip_code_fences,
    _trim_html_prose,
    _wants_file_op,
)


def _sr_block(search: str, replace: str) -> str:
    return f"<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE"


class ScriptedLLM:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    def invoke(self, messages):
        out = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        return SimpleNamespace(content=out)


# ---------------------------------------------------------------------------
# Intent heuristic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msg",
    [
        "make me a html file, it should mimic a nicely built website",
        "create an index.html file for a landing page",
        "edit index.html to change the title",
        "add a navbar to index.html",
        "write a CSS file for the theme",
        # Named a UI element rather than a file — used to miss the target gate
        # entirely and dead-end on the tool-free _direct_answer.
        "fix the navigation on all the pages",
        "update the footer links",
        "change the hero section styles",
    ],
)
def test_wants_file_op_true(msg):
    assert _wants_file_op(msg) is True


@pytest.mark.parametrize(
    "msg",
    [
        "write a python function that adds two numbers",
        "explain what a decorator does",
        "what is the time complexity of quicksort",
        "create a class that represents a point",
    ],
)
def test_wants_file_op_false(msg):
    assert _wants_file_op(msg) is False


# ---------------------------------------------------------------------------
# Filename + content parsing
# ---------------------------------------------------------------------------


def test_extract_filename():
    assert _extract_filename("edit index.html now") == "index.html"
    assert _extract_filename("update src/app.py please") == "src/app.py"
    assert _extract_filename("make a website") is None


def test_extract_filename_skips_prose_abbreviations():
    # "e.g." must not be mistaken for a file (a live run created a junk `e.g`).
    assert (
        _extract_filename("Add JS to login.html, e.g. validate the form")
        == "login.html"
    )
    assert _extract_filename("style it nicely, e.g. modern colors") is None


def test_infer_filename():
    assert _infer_filename("make me an html page") == "index.html"
    assert _infer_filename("write a python script") == "main.py"
    assert _infer_filename("something unknown") == "output.txt"


def test_strip_code_fences():
    assert _strip_code_fences("```html\n<h1>hi</h1>\n```") == "<h1>hi</h1>"
    assert _strip_code_fences("no fences") == "no fences"
    # a stray unmatched closing fence must be dropped, not written to the file
    assert _strip_code_fences("def f():\n    return 1\n```") == "def f():\n    return 1"
    # prose around a real fenced block → keep only the block
    assert _strip_code_fences("Here:\n```\ncode\n```\nthanks") == "code"


def test_parse_file_output_with_filename_header():
    name, content = _parse_file_output(
        "FILENAME: page.html\n<html></html>", fallback="x.txt"
    )
    assert name == "page.html"
    assert content == "<html></html>"


def test_parse_file_output_fallback():
    name, content = _parse_file_output("<html></html>", fallback="index.html")
    assert name == "index.html"
    assert content == "<html></html>"


def test_trim_html_prose_removes_trailing_commentary():
    src = "<!DOCTYPE html><html><body>x</body></html>\n\nHere is your page!"
    assert _trim_html_prose(src) == "<!DOCTYPE html><html><body>x</body></html>"


def test_trim_html_prose_removes_leading_commentary():
    src = "Sure, here you go:\n<!DOCTYPE html><html></html>"
    assert _trim_html_prose(src) == "<!DOCTYPE html><html></html>"


def test_trim_html_prose_keeps_clean_document():
    src = "<!DOCTYPE html><html><body>x</body></html>"
    assert _trim_html_prose(src) == src


def test_trim_html_prose_noop_on_fragment():
    # No document boundaries → leave it entirely alone (real markup untouched).
    src = "Hello <span>world</span>"
    assert _trim_html_prose(src) == src


def test_parse_file_output_keeps_only_the_requested_block():
    """The model sometimes answers a one-file call with the WHOLE build. Every
    block after the first used to land inside the first file — a stylesheet with
    a script and an HTML document appended to it (seen live in the eval suite)."""
    raw = (
        "FILENAME: styles.css\n"
        "h1 { color: blue; }\n\n"
        "FILENAME: script.js\n"
        "console.log('hi');\n\n"
        "FILENAME: index.html\n"
        "<!DOCTYPE html><html><body>x</body></html>"
    )
    name, content = _parse_file_output(raw, fallback="x.txt", target="styles.css")
    assert name == "styles.css"
    assert content == "h1 { color: blue; }"

    # …and the call that asked for a later file gets that file, not the first.
    name, content = _parse_file_output(raw, fallback="x.txt", target="index.html")
    assert name == "index.html"
    assert content == "<!DOCTYPE html><html><body>x</body></html>"


def test_parse_file_output_falls_back_to_the_first_block():
    raw = "FILENAME: a.css\nbody{}\n\nFILENAME: b.js\nconsole.log(1);"
    name, content = _parse_file_output(raw, fallback="x.txt")
    assert (name, content) == ("a.css", "body{}")


def test_parse_file_output_trims_prose_for_html():
    raw = "FILENAME: index.html\n<html><body>hi</body></html>\n\nHope this helps!"
    name, content = _parse_file_output(raw, fallback="x.txt")
    assert name == "index.html"
    assert content == "<html><body>hi</body></html>"


# ---------------------------------------------------------------------------
# _file_op_flow — deterministic create/update (scripted LLM, real write_file)
# ---------------------------------------------------------------------------


async def test_file_op_flow_creates_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_create")
    a._llm_direct = ScriptedLLM(["FILENAME: hello.html\n<html><body>Hi</body></html>"])

    answer, trace = await a._file_op_flow("make me an html file")

    written = tmp_path / "hello.html"
    assert written.exists()
    assert written.read_text(encoding="utf-8") == "<html><body>Hi</body></html>"
    assert trace[0]["tool"] == "write_file"
    assert trace[0]["result"]["success"] is True
    assert "Created" in answer


async def test_file_op_flow_untargeted_repair_escalates_to_tool_loop(
    tmp_path, monkeypatch
):
    """A repair request with no identifiable target must not write output.txt.

    Regression: "fix the navigation" named no file, _extract_filename and the
    last-write fallback both came back None, so _infer_filename's last resort
    ("output.txt") won and the model — handed no file to work from — wrote
    "please provide the contents of these files" straight to disk.
    """
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_untargeted")
    a._llm_direct = ScriptedLLM(
        ["I need more information. Please provide the contents of these files."]
    )

    async def fake_loop(messages, max_steps=None):
        return "loop done", [{"tool": "read_file"}]

    monkeypatch.setattr(a, "_run_tool_loop", fake_loop)

    answer, trace = await a._file_op_flow("fix the navigation")

    assert answer == "loop done"
    assert not (tmp_path / "output.txt").exists()


async def test_file_op_flow_untargeted_creation_still_infers_name(
    tmp_path, monkeypatch
):
    """The escalation is scoped to repairs — creation still infers a filename."""
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_untargeted_create")
    a._llm_direct = ScriptedLLM(["FILENAME: index.html\n<html><body>Hi</body></html>"])

    async def fail_loop(*args, **kwargs):
        raise AssertionError("creation must not escalate")

    monkeypatch.setattr(a, "_run_tool_loop", fail_loop)

    answer, trace = await a._file_op_flow("make me a landing page")

    assert (tmp_path / "index.html").exists()
    assert "Created" in answer


async def test_file_op_flow_updates_existing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    existing = tmp_path / "index.html"
    existing.write_text("<html>old</html>", encoding="utf-8")

    a = AgentCore(session_id="pytest_update")
    # force the whole-file rewrite path: surgical emits no blocks
    a._llm_edit = ScriptedLLM(["no blocks"])
    a._llm_direct = ScriptedLLM(["FILENAME: index.html\n<html>new + contact</html>"])

    answer, trace = await a._file_op_flow("add a contact section to index.html")

    assert existing.read_text(encoding="utf-8") == "<html>new + contact</html>"
    assert "Updated" in answer


# ---------------------------------------------------------------------------
# @ file references (Slice 1)
# ---------------------------------------------------------------------------


def test_extract_at_refs():
    assert _extract_at_refs("change @src/app.py and @utils.py now") == [
        "src/app.py",
        "utils.py",
    ]
    assert _extract_at_refs("no refs here") == []
    # an email must not be mistaken for a reference
    assert _extract_at_refs("mail me at a@b.com") == []


def test_strip_at_refs():
    assert _strip_at_refs("edit @src/app.py please") == "edit src/app.py please"
    assert _strip_at_refs("nothing") == "nothing"


def test_resolve_ref_prefers_existing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "real.py").write_text("x = 1\n", encoding="utf-8")
    a = AgentCore(session_id="pytest_resolve")
    # ghost.py does not exist, real.py does → resolve to the existing one
    assert a._resolve_ref(["ghost.py", "real.py"]) == "real.py"
    # none exist → fall back to the first (so it can be created)
    assert a._resolve_ref(["new.py"]) == "new.py"
    assert a._resolve_ref([]) is None


def test_read_refs_injects_content(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.py").write_text("print('hello a')\n", encoding="utf-8")
    a = AgentCore(session_id="pytest_readrefs")
    ctx = a._read_refs(["a.py", "missing.py"])
    assert "### a.py" in ctx
    assert "hello a" in ctx
    assert "missing.py" not in ctx  # non-existent refs are skipped


async def test_at_ref_targets_file_over_message_guess(tmp_path, monkeypatch):
    """An @ref pins the edit target even when the message names another file."""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "real.py"
    target.write_text("v = 1\n", encoding="utf-8")

    a = AgentCore(session_id="pytest_attarget")
    # surgical emits no blocks → falls back to the whole-file rewrite path
    a._llm_edit = ScriptedLLM(["no blocks here"])
    a._llm_direct = ScriptedLLM(["FILENAME: real.py\nv = 2\n"])

    # _resolve_ref would be fed ["real.py"]; pass it directly as target
    answer, trace = await a._file_op_flow("bump the version", target="real.py")

    # content is written verbatim minus surrounding whitespace (fence-stripping)
    assert target.read_text(encoding="utf-8") == "v = 2"
    assert "Updated" in answer
    assert "real.py" in answer


# ---------------------------------------------------------------------------
# Last-write fallback: a follow-up that names no file edits the previous file
# ---------------------------------------------------------------------------


async def test_follow_up_without_filename_edits_last_written_file(
    tmp_path, monkeypatch
):
    """ "make me an html file" then "add a footer to the page" must edit the
    file written first, not invent a new generic filename."""
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_lastwrite")
    a._llm_direct = ScriptedLLM(["FILENAME: hello.html\n<html><body>Hi</body></html>"])
    a._llm_edit = ScriptedLLM(
        [_sr_block("<body>Hi</body>", "<body>Hi<footer>f</footer></body>")]
    )

    await a._file_op_flow("make me an html file")
    answer, trace = await a._file_op_flow("add a footer to the page")

    written = tmp_path / "hello.html"
    assert written.read_text(encoding="utf-8") == (
        "<html><body>Hi<footer>f</footer></body></html>"
    )
    assert "hello.html" in answer
    # and no bogus second file appeared
    assert {p.name for p in tmp_path.iterdir() if p.is_file()} == {"hello.html"}


async def test_new_artifact_request_is_not_hijacked_by_last_write(
    tmp_path, monkeypatch
):
    """ "now write a css file" after writing hello.html must create a NEW file,
    not surgically edit hello.html."""
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_lastwrite_new")
    a._llm_direct = ScriptedLLM(
        [
            "FILENAME: hello.html\n<html>hi</html>",
            "FILENAME: theme.css\nbody { color: red; }",
        ]
    )

    await a._file_op_flow("make me an html file")
    await a._file_op_flow("now write a css file for the theme")

    assert (tmp_path / "theme.css").is_file()
    assert (tmp_path / "hello.html").read_text(encoding="utf-8") == "<html>hi</html>"


def test_last_write_fallback_guards(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    a = AgentCore(session_id="pytest_lastwrite_guard")

    # nothing written yet
    assert a._last_write_fallback("add a footer") is None

    # last write no longer on disk
    a._last_write_path = str(proj / "gone.html")
    assert a._last_write_fallback("add a footer") is None

    # exists, but outside the current workdir (e.g. project switched) → no hijack
    outside = tmp_path / "outside.html"
    outside.write_text("<html></html>", encoding="utf-8")
    a._last_write_path = str(outside)
    assert a._last_write_fallback("add a footer") is None

    # exists in the workdir → workdir-relative target
    inside = proj / "index.html"
    inside.write_text("<html></html>", encoding="utf-8")
    a._last_write_path = str(inside)
    assert a._last_write_fallback("add a footer") == "index.html"
    # ...unless the request asks for a NEW artifact
    assert a._last_write_fallback("now make a css file") is None
    assert a._last_write_fallback("build a new page for contacts") is None


# ---------------------------------------------------------------------------
# Surgical SEARCH/REPLACE editing (Slice 3)
# ---------------------------------------------------------------------------


def test_parse_search_replace_single():
    blocks = _parse_search_replace(_sr_block("old line", "new line"))
    assert blocks == [("old line", "new line")]


def test_parse_search_replace_multiple():
    text = _sr_block("a", "A") + "\n" + _sr_block("b", "B")
    assert _parse_search_replace(text) == [("a", "A"), ("b", "B")]


def test_parse_search_replace_none():
    assert _parse_search_replace("just prose, no blocks") == []


def test_apply_exact_match_changes_only_target():
    content = "line1\nline2\nline3\n"
    new, applied, failed = _apply_search_replace(content, [("line2", "LINE2")])
    assert new == "line1\nLINE2\nline3\n"
    assert (applied, failed) == (1, 0)


def test_apply_whitespace_tolerant():
    content = "def f():\n    return 1\n"
    # SEARCH has different trailing whitespace than the file
    new, applied, failed = _apply_search_replace(
        content, [("    return 1   ", "    return 2")]
    )
    assert "return 2" in new
    assert applied == 1


def test_apply_no_match_counts_failure():
    content = "alpha\nbeta\n"
    new, applied, failed = _apply_search_replace(content, [("zeta", "z")])
    assert new == content
    assert (applied, failed) == (0, 1)


def test_apply_reindents_when_search_drops_indentation():
    # The 3B copies SEARCH lines without the file's leading indent; the matched
    # region must still be replaced AND the replacement re-indented to match.
    content = 'def greet(name):\n    return f"Hello {name}"\n'
    blocks = [('return f"Hello {name}"', 'return f"Goodbye {name}"')]
    new, applied, failed = _apply_search_replace(content, blocks)
    assert new == 'def greet(name):\n    return f"Goodbye {name}"\n'
    assert (applied, failed) == (1, 0)


async def test_surgical_edit_changes_one_line(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "app.py"
    f.write_text('def greet(name):\n    return f"Hello {name}"\n', encoding="utf-8")

    a = AgentCore(session_id="pytest_surgical")
    a._llm_edit = ScriptedLLM(
        [_sr_block('    return f"Hello {name}"', '    return f"Goodbye {name}"')]
    )

    result = await a._surgical_edit(
        "app.py", f, f.read_text(encoding="utf-8"), "say goodbye"
    )
    assert result is not None
    answer, trace = result
    assert (
        f.read_text(encoding="utf-8")
        == 'def greet(name):\n    return f"Goodbye {name}"\n'
    )
    assert "Edited" in answer
    assert trace[0]["result"]["success"] is True


async def test_surgical_edit_returns_none_without_blocks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "app.py"
    f.write_text("x = 1\n", encoding="utf-8")
    a = AgentCore(session_id="pytest_noblocks")
    # both the initial call and the one retry yield no blocks → None (fall back)
    a._llm_edit = ScriptedLLM(["sorry, here is some prose with no blocks"])
    assert await a._surgical_edit("app.py", f, "x = 1\n", "change it") is None


async def test_file_op_flow_prefers_surgical_then_falls_back(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "app.py"
    f.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")

    a = AgentCore(session_id="pytest_flow_surgical")
    # Surgical (uses _llm_edit) returns a valid block → only line b changes.
    a._llm_edit = ScriptedLLM([_sr_block("b = 2", "b = 20")])
    answer, trace = await a._file_op_flow("change b", target="app.py")
    assert f.read_text(encoding="utf-8") == "a = 1\nb = 20\nc = 3\n"
    assert "Edited" in answer

    # Now surgical fails (no blocks, incl. retry) → falls back to whole-file rewrite.
    a._llm_edit = ScriptedLLM(["no blocks"])
    a._llm_direct = ScriptedLLM(["FILENAME: app.py\nWHOLE NEW FILE"])
    answer2, trace2 = await a._file_op_flow("rewrite it", target="app.py")
    assert f.read_text(encoding="utf-8") == "WHOLE NEW FILE"
    assert "Updated" in answer2
