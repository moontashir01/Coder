"""Tier 3 #8 — safe writes: backup-before-mutate + undo_write.

Every mutating filesystem tool (write_file over an existing file, edit_file,
delete_file) must first copy the current content into settings.backups_dir.
undo_write() restores the most recent backup (optionally for one path) and
consumes it, so repeated undos walk back through history.
"""
import pytest

from app.agent.executor import Executor
from app.agent.tool_registry import create_registry
from app.tools.filesystem import delete_file, edit_file, undo_write, write_file
from config.settings import settings


def _backups(tmp_path):
    d = tmp_path / settings.backups_dir
    return sorted(d.iterdir()) if d.exists() else []


# ---------------------------------------------------------------------------
# Backup on mutate
# ---------------------------------------------------------------------------


def test_write_file_backs_up_existing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "a.txt"
    f.write_text("old", encoding="utf-8")

    res = write_file(str(f), "new")

    assert res["success"] is True
    assert f.read_text(encoding="utf-8") == "new"
    backups = _backups(tmp_path)
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "old"


def test_write_file_new_file_makes_no_backup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    res = write_file(str(tmp_path / "fresh.txt"), "hi")

    assert res["success"] is True
    assert _backups(tmp_path) == []


def test_edit_file_backs_up(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "b.txt"
    f.write_text("hello world", encoding="utf-8")

    res = edit_file(str(f), "world", "there")

    assert res["success"] is True
    backups = _backups(tmp_path)
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "hello world"


def test_failed_edit_makes_no_backup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "c.txt"
    f.write_text("content", encoding="utf-8")

    res = edit_file(str(f), "NOT PRESENT", "x")

    assert res["success"] is False
    assert _backups(tmp_path) == []


def test_delete_file_backs_up(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "d.txt"
    f.write_text("precious", encoding="utf-8")

    res = delete_file(str(f), confirm=True)

    assert res["success"] is True
    assert not f.exists()
    backups = _backups(tmp_path)
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "precious"


# ---------------------------------------------------------------------------
# undo_write
# ---------------------------------------------------------------------------


def test_undo_restores_last_write(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "u.txt"
    f.write_text("old", encoding="utf-8")
    write_file(str(f), "new")

    res = undo_write()

    assert res["success"] is True
    assert f.read_text(encoding="utf-8") == "old"
    # backup consumed → nothing left to undo
    assert undo_write()["success"] is False


def test_undo_restores_deleted_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "gone.txt"
    f.write_text("bring me back", encoding="utf-8")
    delete_file(str(f), confirm=True)
    assert not f.exists()

    res = undo_write()

    assert res["success"] is True
    assert f.read_text(encoding="utf-8") == "bring me back"


def test_undo_with_path_targets_that_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f1 = tmp_path / "one.txt"
    f2 = tmp_path / "two.txt"
    f1.write_text("one-old", encoding="utf-8")
    f2.write_text("two-old", encoding="utf-8")
    write_file(str(f1), "one-new")
    write_file(str(f2), "two-new")  # most recent backup is f2's

    res = undo_write(path=str(f1))

    assert res["success"] is True
    assert f1.read_text(encoding="utf-8") == "one-old"
    assert f2.read_text(encoding="utf-8") == "two-new"  # untouched


def test_undo_walks_history_backwards(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "h.txt"
    f.write_text("v1", encoding="utf-8")
    write_file(str(f), "v2")
    write_file(str(f), "v3")

    assert undo_write()["success"] is True
    assert f.read_text(encoding="utf-8") == "v2"
    assert undo_write()["success"] is True
    assert f.read_text(encoding="utf-8") == "v1"


def test_undo_nothing_to_undo_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    res = undo_write()

    assert res["success"] is False
    assert "no backup" in res["error"].lower()


def test_backups_pruned_to_max(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "max_write_backups", 5)
    f = tmp_path / "p.txt"
    f.write_text("v0", encoding="utf-8")
    for i in range(1, 9):
        write_file(str(f), f"v{i}")

    assert len(_backups(tmp_path)) == 5


# ---------------------------------------------------------------------------
# Per-project scoping (Step 10 / C3)
# ---------------------------------------------------------------------------


def test_backups_land_under_project_root(tmp_path, monkeypatch):
    proj = tmp_path / "A"
    proj.mkdir()
    monkeypatch.setattr(settings, "sandbox_root", proj)
    f = proj / "x.txt"
    f.write_text("old", encoding="utf-8")

    write_file(str(f), "new")

    assert (proj / settings.backups_dir).exists()
    # Not in the parent (cwd-relative) location.
    assert not (tmp_path / settings.backups_dir).exists()


def test_undo_scoped_to_active_project(tmp_path, monkeypatch):
    proj_a = tmp_path / "A"
    proj_b = tmp_path / "B"
    proj_a.mkdir()
    proj_b.mkdir()
    f = proj_a / "x.txt"
    f.write_text("old", encoding="utf-8")

    monkeypatch.setattr(settings, "sandbox_root", proj_a)
    write_file(str(f), "new")

    # A different project can't see (or undo) project A's backup.
    monkeypatch.setattr(settings, "sandbox_root", proj_b)
    other = undo_write()
    assert other["success"] is False
    assert "no backup" in other["error"].lower()

    # Back in project A, undo works and the confirmation names the restored file.
    monkeypatch.setattr(settings, "sandbox_root", proj_a)
    res = undo_write()
    assert res["success"] is True
    assert "x.txt" in res["result"]
    assert f.read_text(encoding="utf-8") == "old"


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


async def test_repl_undo_command(tmp_path, monkeypatch):
    import io

    from rich.console import Console

    import app.cli.commands as commands_mod
    from app.cli.commands import handle_command

    monkeypatch.chdir(tmp_path)
    buf = io.StringIO()
    monkeypatch.setattr(
        commands_mod, "console", Console(file=buf, force_terminal=False, width=80)
    )
    f = tmp_path / "cmd.txt"
    f.write_text("old", encoding="utf-8")
    write_file(str(f), "new")

    class FakeRepl:
        pass

    handled = await handle_command("/undo", FakeRepl())

    assert handled is True
    assert f.read_text(encoding="utf-8") == "old"
    assert "Restored" in buf.getvalue()


async def test_undo_write_registered_and_executes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "r.txt"
    f.write_text("old", encoding="utf-8")
    write_file(str(f), "new")

    registry = create_registry()
    assert "undo_write" in registry.names()

    result = await Executor(registry).execute("undo_write", {})

    assert result["success"] is True
    assert f.read_text(encoding="utf-8") == "old"
