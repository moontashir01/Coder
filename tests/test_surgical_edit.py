"""The write path's two truncation guards, and the failed-block retry.

Every test here is a regression for something the pipeline did to a file the
model got right: a prompt that showed a cut copy of the file under the words
"return the COMPLETE updated file", a rewrite written despite coming back a
fraction of the size, and a SEARCH miss that ended the attempt instead of
showing the model the lines it was trying to quote.

Offline: the LLM is a scripted fake and every write goes to tmp_path.
"""

from types import SimpleNamespace

from app.agent.core import AgentCore
from config.settings import settings


class ScriptedLLM:
    """Returns the next canned string per `.invoke`, recording the prompts."""

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0
        self.prompts = []

    def invoke(self, messages):
        self.prompts.append("\n".join(str(m.content) for m in messages))
        out = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        return SimpleNamespace(content=out)


def _sr_block(search: str, replace: str) -> str:
    return f"<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE"


def _big_python(lines: int = 400) -> str:
    return "".join(f"def f{i}():\n    return {i}\n" for i in range(lines))


# ---------------------------------------------------------------------------
# What the editing model is shown
# ---------------------------------------------------------------------------


def test_the_edit_view_is_numbered_and_not_cut_at_6000(tmp_path, monkeypatch):
    """The old hardcoded 6000 made every edit past it unmatchable by construction.

    An unmatchable edit falls through to the whole-file rewrite, which is the
    path this one exists to avoid, so the cap being wrong was not a cosmetic
    problem — it decided which path ran.
    """
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_editview")
    source = _big_python(400)
    assert len(source) > 6000
    view = a._edit_view(source)
    assert "def f399():" in view
    assert "TRUNCATED" not in view
    assert view.startswith("   1 | def f0():")


def test_a_view_that_really_does_not_fit_says_so(tmp_path, monkeypatch):
    """A silent cut reads to the model as the whole file, so it edits an end
    that is not there. Stated, it knows not to."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "max_edit_context_chars", 1000)
    a = AgentCore(session_id="pytest_editview_cut")
    view = a._edit_view(_big_python(400))
    assert "TRUNCATED" in view
    assert "Do not write a SEARCH block for text you cannot see" in view


# ---------------------------------------------------------------------------
# The retry that shows the model where it missed
# ---------------------------------------------------------------------------


async def test_a_missed_search_is_retried_with_the_real_lines(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "app.py"
    f.write_text("def alpha():\n    return 1\n\ndef beta():\n    return 2\n", "utf-8")

    a = AgentCore(session_id="pytest_retry")
    a._llm_edit = ScriptedLLM(
        [
            # Misquotes the function badly enough that no rung rescues it.
            _sr_block(
                "def beta(request):\n    value = compute()\n    return value",
                "def beta():\n    return 22",
            ),
            # Shown the real lines, it quotes them correctly.
            _sr_block("def beta():\n    return 2", "def beta():\n    return 22"),
        ]
    )
    result = await a._surgical_edit(
        "app.py", f, f.read_text(encoding="utf-8"), "make beta return 22"
    )
    assert result is not None
    assert f.read_text(encoding="utf-8") == (
        "def alpha():\n    return 1\n\ndef beta():\n    return 22\n"
    )
    # The correction prompt has to CONTAIN the file's real text, or it is just
    # a louder way of saying "you failed".
    correction = [p for p in a._llm_edit.prompts if "matched nothing" in p]
    assert len(correction) == 1
    assert "def beta():" in correction[0]


async def test_the_retry_happens_once_and_then_gives_up(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "app.py"
    f.write_text("x = 1\ny = 2\n", encoding="utf-8")
    a = AgentCore(session_id="pytest_retry_once")
    a._llm_edit = ScriptedLLM([_sr_block("nothing\nlike this", "z = 3")])
    assert (
        await a._surgical_edit("app.py", f, f.read_text("utf-8"), "change it") is None
    )
    assert a._llm_edit.calls == 2  # first attempt + one retry, never a loop
    assert f.read_text(encoding="utf-8") == "x = 1\ny = 2\n"


async def test_a_landed_block_is_not_reverted_by_a_failed_one(tmp_path, monkeypatch):
    """Partial success must stay: the retry runs against what already applied."""
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "app.py"
    f.write_text("a = 1\nb = 2\n", encoding="utf-8")
    a = AgentCore(session_id="pytest_partial")
    a._llm_edit = ScriptedLLM(
        [
            _sr_block("a = 1", "a = 11") + "\n" + _sr_block("zzz", "qqq"),
            "no blocks here",  # the retry helps nothing
        ]
    )
    result = await a._surgical_edit("app.py", f, f.read_text("utf-8"), "bump a")
    assert result is not None
    assert f.read_text(encoding="utf-8") == "a = 11\nb = 2\n"


# ---------------------------------------------------------------------------
# The shrink guard
# ---------------------------------------------------------------------------


async def test_a_rewrite_that_comes_back_truncated_is_refused(tmp_path, monkeypatch):
    """The reported failure, reproduced: the file on disk must survive it."""
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "app.py"
    original = _big_python(30)
    f.write_text(original, encoding="utf-8")

    a = AgentCore(session_id="pytest_shrink")
    a._llm_edit = ScriptedLLM(["no blocks"])  # force the rewrite path
    a._llm_direct = ScriptedLLM(["FILENAME: app.py\ndef f0():\n    return 0\n"])
    answer, trace = await a._file_op_flow("make f0 return zero", target="app.py")

    assert f.read_text(encoding="utf-8") == original
    assert "Refused" in answer and "truncation" in answer
    assert trace == []


async def test_a_requested_rewrite_is_allowed_to_shrink(tmp_path, monkeypatch):
    """The guard must not become a way to refuse the thing the user asked for."""
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "app.py"
    f.write_text(_big_python(30), encoding="utf-8")

    a = AgentCore(session_id="pytest_shrink_ok")
    a._llm_edit = ScriptedLLM(["no blocks"])
    a._llm_direct = ScriptedLLM(["FILENAME: app.py\ndef f0():\n    return 0\n"])
    answer, _ = await a._file_op_flow("rewrite it from scratch", target="app.py")
    assert f.read_text(encoding="utf-8") == "def f0():\n    return 0"
    assert "Refused" not in answer


async def test_the_guard_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "shrink_guard", False)
    f = tmp_path / "app.py"
    f.write_text(_big_python(30), encoding="utf-8")
    a = AgentCore(session_id="pytest_shrink_off")
    a._llm_edit = ScriptedLLM(["no blocks"])
    a._llm_direct = ScriptedLLM(["FILENAME: app.py\nshort\n"])
    await a._file_op_flow("change it", target="app.py")
    assert f.read_text(encoding="utf-8") == "short"


# ---------------------------------------------------------------------------
# The oversized-rewrite refusal
# ---------------------------------------------------------------------------


async def test_a_file_too_big_to_rewrite_is_not_rewritten(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "max_rewrite_chars", 1000)
    f = tmp_path / "big.py"
    original = _big_python(200)
    f.write_text(original, encoding="utf-8")

    a = AgentCore(session_id="pytest_toobig")
    a._llm_edit = ScriptedLLM(["no blocks"])
    a._llm_direct = ScriptedLLM(["FILENAME: big.py\nwhatever\n"])
    answer, trace = await a._file_op_flow("change something", target="big.py")

    assert f.read_text(encoding="utf-8") == original
    assert "Refused to rewrite" in answer
    assert trace == []
    assert a._llm_direct.calls == 0  # refused before spending the call


async def test_creating_a_new_file_is_untouched_by_either_guard(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "max_rewrite_chars", 10)
    a = AgentCore(session_id="pytest_create_ok")
    a._llm_direct = ScriptedLLM(["FILENAME: new.txt\nhello\n"])
    answer, _ = await a._file_op_flow("create new.txt saying hello", target="new.txt")
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "hello"
    assert "Created" in answer
