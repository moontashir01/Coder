"""Step 6 / S3, S6 — human-in-the-loop approval gate in the Executor.

The Executor consults an approval hook before running any tool whose
permissions intersect settings.approval_gated_permissions. Reads are never
gated; --yolo (auto_approve) bypasses; with no hook the default is allow,
except --safe denies shell/deletes.
"""
import pytest

from app.agent.executor import Executor
from app.agent.tool_registry import create_registry
from config.settings import settings


def _executor(hook=None):
    return Executor(create_registry(), approval_hook=hook)


async def test_gated_tool_consults_hook(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []

    async def hook(name, args, perms):
        calls.append((name, perms))
        return True

    ex = _executor(hook)
    res = await ex.execute(
        "write_file", {"path": str(tmp_path / "a.txt"), "content": "hi"}
    )
    assert res["success"] is True
    assert calls and calls[0][0] == "write_file"
    assert "fs:write" in calls[0][1]


async def test_denied_hook_blocks_and_does_not_write(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def deny(name, args, perms):
        return False

    ex = _executor(deny)
    target = tmp_path / "b.txt"
    res = await ex.execute("write_file", {"path": str(target), "content": "hi"})
    assert res["success"] is False
    assert "not approved" in res["error"].lower()
    assert not target.exists()


async def test_reads_are_not_gated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "r.txt"
    f.write_text("data", encoding="utf-8")
    called = []

    async def hook(name, args, perms):
        called.append(name)
        return True

    ex = _executor(hook)
    res = await ex.execute("read_file", {"path": str(f)})
    assert res["success"] is True
    assert called == []  # read_file is fs:read → never prompts


async def test_yolo_bypasses_hook(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "auto_approve", True)
    called = []

    async def hook(name, args, perms):
        called.append(name)
        return False

    ex = _executor(hook)
    res = await ex.execute(
        "write_file", {"path": str(tmp_path / "y.txt"), "content": "hi"}
    )
    assert res["success"] is True
    assert called == []  # auto_approve → hook never consulted


async def test_no_hook_allows_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ex = _executor(None)
    res = await ex.execute(
        "write_file", {"path": str(tmp_path / "d.txt"), "content": "hi"}
    )
    assert res["success"] is True


async def test_safe_mode_denies_shell_without_hook(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "safe_mode", True)
    ex = _executor(None)
    res = await ex.execute("run_command", {"command": "echo hi"})
    assert res["success"] is False
    assert "not approved" in res["error"].lower()


async def test_safe_mode_still_allows_writes_without_hook(tmp_path, monkeypatch):
    # fs:write is not in safe_deny_permissions → allowed even in --safe with no hook.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "safe_mode", True)
    ex = _executor(None)
    res = await ex.execute(
        "write_file", {"path": str(tmp_path / "w.txt"), "content": "hi"}
    )
    assert res["success"] is True
