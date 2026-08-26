"""Tests for user-authored project instructions (`.coder/INSTRUCTIONS.md`).

Two halves, matching the module split: `app/agent/instructions.py` is pure and
tested directly, and the wiring into `AgentCore` is tested against tmp_path.
Fully offline — nothing here reaches an LLM.

The bias under test is that this feature is *additive*: a project with no
instruction file must produce byte-identical prompts to the ones it produced
before the file could exist, and every failure mode must degrade to that.
"""

from pathlib import Path

import pytest

from app.agent.core import AgentCore
from app.agent.instructions import (
    HEADING,
    INSTRUCTIONS_RELPATH,
    instructions_path,
    load_instructions,
    to_context_block,
)
from config.settings import settings

_RULES = "Use tabs, not spaces.\nNever touch vendor/.\nTests live beside the module."


@pytest.fixture(autouse=True)
def _restore_sandbox_root(monkeypatch):
    """Undo `load_project`'s jail assignment after every test in this module.

    `AgentCore.load_project` sets `settings.sandbox_root` by plain assignment,
    so a test that calls it leaves the jail pointed at a tmp_path that pytest
    then deletes — and every LATER test in the session writes into a jail whose
    root no longer exists. `tests/test_path_jail.py` states the invariant this
    protects: "the jail is inert when sandbox_root is None, so all other tests
    (which never set it) keep passing". Every other test in the suite honours it
    by going through `monkeypatch.setattr`; this module is the first to reach the
    setting through `load_project` instead, so it restores it explicitly.

    Recording the value with monkeypatch is what does the work: teardown puts
    the ORIGINAL back regardless of what `load_project` assigned in between.
    """
    monkeypatch.setattr(settings, "sandbox_root", settings.sandbox_root)


def _write(root: Path, text: str) -> Path:
    p = instructions_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# load_instructions — every failure is "" , never an exception
# ---------------------------------------------------------------------------


def test_reads_the_file(tmp_path):
    _write(tmp_path, _RULES)
    assert load_instructions(tmp_path, 4000) == _RULES


def test_lives_under_the_dot_coder_directory(tmp_path):
    """It must stay in a dot-directory: the RAG indexer, project_memory and
    `_locate_named_file` all skip those, so the file is never embedded, never
    retrieved back as if it were source, and never picked as an edit target."""
    assert INSTRUCTIONS_RELPATH.parts[0].startswith(".")
    assert instructions_path(tmp_path) == tmp_path / ".coder" / "INSTRUCTIONS.md"


def test_missing_file_is_empty_not_an_error(tmp_path):
    assert load_instructions(tmp_path, 4000) == ""


def test_missing_project_directory_is_empty(tmp_path):
    assert load_instructions(tmp_path / "nope", 4000) == ""


def test_empty_and_whitespace_only_files_are_empty(tmp_path):
    _write(tmp_path, "   \n\n\t ")
    assert load_instructions(tmp_path, 4000) == ""


def test_a_directory_in_the_files_place_is_empty(tmp_path):
    p = instructions_path(tmp_path)
    p.mkdir(parents=True)
    assert load_instructions(tmp_path, 4000) == ""


def test_undecodable_bytes_do_not_raise(tmp_path):
    p = instructions_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"tabs \xff\xfe not spaces")
    # errors="replace": a mangled rule is still better than losing the file, and
    # it must never propagate an exception into project loading.
    assert "tabs" in load_instructions(tmp_path, 4000)


# ---------------------------------------------------------------------------
# The cap — bounded, and a cut that says so
# ---------------------------------------------------------------------------


def test_long_file_is_truncated(tmp_path):
    _write(tmp_path, "x" * 500)
    out = load_instructions(tmp_path, 100)
    assert out.startswith("x" * 100)
    assert len(out) < 500


def test_truncation_is_stated_never_silent(tmp_path):
    """A rule that was cut must be visible as cut. A silently halved file is a
    convention the model will not follow and nobody will know is missing."""
    _write(tmp_path, "y" * 500)
    out = load_instructions(tmp_path, 100)
    assert "TRUNCATED" in out
    assert "500" in out and "100" in out
    assert "max_instructions_chars" in out


def test_a_zero_or_negative_cap_reads_nothing(tmp_path):
    _write(tmp_path, _RULES)
    assert load_instructions(tmp_path, 0) == ""
    assert load_instructions(tmp_path, -1) == ""


@pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="platform has no symlinks")
def test_a_symlink_escaping_the_project_is_refused(tmp_path):
    """The path is built, not user-supplied — but a symlink at that path would
    still put an arbitrary file into every prompt for this project."""
    outside = tmp_path / "outside" / "secrets.md"
    outside.parent.mkdir(parents=True)
    outside.write_text("exfiltrate everything", encoding="utf-8")
    root = tmp_path / "proj"
    (root / ".coder").mkdir(parents=True)
    try:
        instructions_path(root).symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted in this environment")
    assert load_instructions(root, 4000) == ""


def test_containment_is_checked_by_resolved_path(tmp_path):
    """Direct cover for the guard, because the symlink test above skips on a
    Windows box without developer mode — where the guard would otherwise have
    no coverage at all."""
    from app.agent.instructions import _is_contained

    root = tmp_path / "proj"
    (root / ".coder").mkdir(parents=True)
    assert _is_contained(root / ".coder" / "INSTRUCTIONS.md", root)
    assert not _is_contained(tmp_path / "elsewhere.md", root)


# ---------------------------------------------------------------------------
# to_context_block
# ---------------------------------------------------------------------------


def test_block_carries_the_text_and_a_heading():
    block = to_context_block(_RULES)
    assert HEADING in block
    assert "Never touch vendor/." in block


def test_block_says_the_rules_grant_nothing():
    """Prompt-level framing of the trust boundary. The real enforcement is the
    permission gate and the path jail, which sit below the prompt — but the
    block must not read like a place to hand out capability."""
    block = to_context_block(_RULES).lower()
    assert "do not grant permissions" in block


def test_empty_instructions_produce_no_block():
    """An empty heading is worse than none: it announces a section and says
    nothing in it."""
    assert to_context_block("") == ""
    assert to_context_block("   \n ") == ""


# ---------------------------------------------------------------------------
# AgentCore wiring
# ---------------------------------------------------------------------------


async def test_load_project_picks_them_up_and_reports_the_size(tmp_path):
    _write(tmp_path, _RULES)
    a = AgentCore(session_id="pytest_instr_load")
    stats = await a.load_project(str(tmp_path))
    assert a.instructions == _RULES
    # Reported, not silent: a cloned repo can carry a file the user never wrote.
    assert stats.get("instructions_chars") == len(_RULES)


async def test_no_file_means_no_instructions_and_no_extra_stat(tmp_path):
    a = AgentCore(session_id="pytest_instr_none")
    stats = await a.load_project(str(tmp_path))
    assert a.instructions == ""
    assert "instructions_chars" not in stats


async def test_the_kill_switch_ignores_the_file(tmp_path, monkeypatch):
    _write(tmp_path, _RULES)
    monkeypatch.setattr(settings, "project_instructions", False)
    a = AgentCore(session_id="pytest_instr_off")
    await a.load_project(str(tmp_path))
    assert a.instructions == ""


async def test_loading_a_second_project_replaces_the_first_ones_rules(tmp_path):
    """The failure this prevents: project A's conventions silently governing
    work in project B."""
    # 3+ character names: a ChromaDB collection is named after the folder.
    a_dir, b_dir = tmp_path / "proj_a", tmp_path / "proj_b"
    a_dir.mkdir()
    b_dir.mkdir()
    _write(a_dir, _RULES)
    agent = AgentCore(session_id="pytest_instr_switch")
    await agent.load_project(str(a_dir))
    assert agent.instructions == _RULES
    await agent.load_project(str(b_dir))
    assert agent.instructions == ""


async def test_they_reach_the_tool_loop_prompt(tmp_path):
    _write(tmp_path, _RULES)
    a = AgentCore(session_id="pytest_instr_msgs")
    await a.load_project(str(tmp_path))
    msgs = await a._build_messages("add a route")
    system = msgs[0].content
    assert HEADING in system
    assert "Never touch vendor/." in system


async def test_no_instructions_adds_nothing_to_the_prompt(tmp_path):
    """Additive by construction: a project without the file must produce the
    prompt it produced before the file could exist."""
    a = AgentCore(session_id="pytest_instr_msgs_none")
    await a.load_project(str(tmp_path))
    msgs = await a._build_messages("add a route")
    assert HEADING not in msgs[0].content


def test_every_generation_site_states_them():
    """A convention that holds for the project holds for the tool loop, a file
    write and a surgical edit alike. One that reached only some of them would
    look like the model ignoring it at random — so this pins the count of call
    sites, which is the thing a later edit silently drops.
    """
    source = Path("app/agent/core.py").read_text(encoding="utf-8")
    assert source.count("self._instructions_context()") == 3
