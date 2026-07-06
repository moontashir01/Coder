"""Tier 3 #8 — diff preview: mutating writes report a unified diff.

write_file (over an existing file) and edit_file attach a "diff" key to the
tool result. The model feedback path only ever reads result["result"], so the
diff is REPL-display-only; _print_tool_step renders it under the tool status.
"""
import io

from rich.console import Console

from app.tools.filesystem import edit_file, write_file


def test_write_file_overwrite_includes_diff(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "a.py"
    f.write_text("old line\n", encoding="utf-8")

    res = write_file(str(f), "new line\n")

    assert res["success"] is True
    assert "-old line" in res["diff"]
    assert "+new line" in res["diff"]
    assert "+1/-1" in res["result"]


def test_write_file_new_file_has_no_diff(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    res = write_file(str(tmp_path / "fresh.py"), "x = 1\n")

    assert res["success"] is True
    assert "diff" not in res


def test_write_file_identical_content_has_no_diff(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "same.py"
    f.write_text("same\n", encoding="utf-8")

    res = write_file(str(f), "same\n")

    assert res["success"] is True
    assert "diff" not in res


def test_edit_file_includes_diff(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "b.py"
    f.write_text("hello world\n", encoding="utf-8")

    res = edit_file(str(f), "world", "there")

    assert res["success"] is True
    assert "-hello world" in res["diff"]
    assert "+hello there" in res["diff"]


# ---------------------------------------------------------------------------
# REPL rendering
# ---------------------------------------------------------------------------


def _captured_repl_console(monkeypatch):
    import app.cli.repl as repl_mod

    buf = io.StringIO()
    monkeypatch.setattr(
        repl_mod, "console", Console(file=buf, force_terminal=False, width=100)
    )
    return repl_mod, buf


def test_print_tool_step_renders_diff(monkeypatch):
    repl_mod, buf = _captured_repl_console(monkeypatch)

    repl_mod._print_tool_step(
        "write_file",
        {"success": True, "diff": "--- a/x.py\n+++ b/x.py\n-old\n+new"},
    )

    out = buf.getvalue()
    assert "write_file" in out
    assert "-old" in out
    assert "+new" in out


def test_print_tool_step_truncates_long_diff(monkeypatch):
    repl_mod, buf = _captured_repl_console(monkeypatch)
    long_diff = "\n".join(f"+line{i}" for i in range(200))

    repl_mod._print_tool_step("write_file", {"success": True, "diff": long_diff})

    out = buf.getvalue()
    assert "+line0" in out
    assert "+line199" not in out
    assert "more diff lines" in out


async def test_agent_turn_passes_result_dict_to_tool_step(monkeypatch):
    repl_mod, buf = _captured_repl_console(monkeypatch)

    class FakeAgent:
        async def chat(self, msg, on_token=None):
            trace = [
                {
                    "tool": "write_file",
                    "arguments": {},
                    "result": {"success": True, "result": "ok", "diff": "-a\n+b"},
                }
            ]
            return "done", trace

    r = repl_mod.CoderREPL(agent=FakeAgent())
    await r._agent_turn("edit the file")

    out = buf.getvalue()
    assert "+b" in out
    assert "done" in out
