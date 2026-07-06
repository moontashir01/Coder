"""Tier 3 #8 — enforce ToolDefinition.permissions.

Builtins carry real permission tags; the Executor refuses any tool whose
permissions intersect settings.denied_permissions. Default deny list is empty,
so out-of-the-box behavior is unchanged.
"""
import pytest

from app.agent.executor import Executor
from app.agent.tool_registry import create_registry
from config.settings import settings


# ---------------------------------------------------------------------------
# Builtin tools carry permission tags
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool,expected",
    [
        ("read_file", "fs:read"),
        ("list_directory", "fs:read"),
        ("search_files", "fs:read"),
        ("find_symbol", "fs:read"),
        ("find_references", "fs:read"),
        ("write_file", "fs:write"),
        ("edit_file", "fs:write"),
        ("create_file", "fs:write"),
        ("undo_write", "fs:write"),
        ("delete_file", "fs:delete"),
        ("run_command", "shell"),
        ("git_status", "git:read"),
        ("git_diff", "git:read"),
        ("git_log", "git:read"),
        ("git_commit", "git:write"),
    ],
)
def test_builtin_tools_carry_permissions(tool, expected):
    registry = create_registry()
    assert expected in registry.get(tool).permissions


# ---------------------------------------------------------------------------
# Executor enforcement
# ---------------------------------------------------------------------------


async def test_executor_denies_denied_permission(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "denied_permissions", ["fs:write"])
    ex = Executor(create_registry())

    result = await ex.execute(
        "write_file", {"path": str(tmp_path / "x.txt"), "content": "nope"}
    )

    assert result["success"] is False
    assert "permission" in result["error"].lower()
    assert "fs:write" in result["error"]
    assert not (tmp_path / "x.txt").exists()


async def test_executor_allows_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ex = Executor(create_registry())

    result = await ex.execute(
        "write_file", {"path": str(tmp_path / "ok.txt"), "content": "yes"}
    )

    assert result["success"] is True
    assert (tmp_path / "ok.txt").exists()


async def test_deny_delete_blocks_delete_not_read(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "denied_permissions", ["fs:delete"])
    f = tmp_path / "keep.txt"
    f.write_text("safe", encoding="utf-8")
    ex = Executor(create_registry())

    denied = await ex.execute("delete_file", {"path": str(f), "confirm": True})
    allowed = await ex.execute("read_file", {"path": str(f)})

    assert denied["success"] is False
    assert f.exists()
    assert allowed["success"] is True
    assert allowed["result"] == "safe"


def test_default_deny_list_is_empty():
    assert settings.denied_permissions == []
