"""Step 5 / S2 — project-root path jail for file tools.

File tools reject paths that resolve outside settings.sandbox_root unless
allow_outside_root is set. The jail is inert when sandbox_root is None, so all
other tests (which never set it) keep passing.
"""
import pytest

from app.tools import filesystem as fs
from config.settings import settings


@pytest.fixture
def jailed(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(settings, "sandbox_root", root)
    monkeypatch.setattr(settings, "allow_outside_root", False)
    return root


def test_jail_inert_when_no_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "sandbox_root", None)
    outside = tmp_path / "anywhere.txt"
    assert fs.write_file(str(outside), "ok")["success"] is True


def test_write_inside_root_allowed(jailed):
    target = jailed / "inside.txt"
    assert fs.write_file(str(target), "hello")["success"] is True
    assert target.read_text(encoding="utf-8") == "hello"


def test_write_outside_root_blocked(jailed, tmp_path):
    outside = tmp_path / "escape.txt"
    res = fs.write_file(str(outside), "nope")
    assert res["success"] is False
    assert "escapes the project root" in res["error"]
    assert not outside.exists()


def test_read_traversal_blocked(jailed):
    res = fs.read_file(str(jailed / ".." / ".." / "etc" / "passwd"))
    assert res["success"] is False
    assert "escapes the project root" in res["error"]


def test_delete_outside_root_blocked(jailed, tmp_path):
    victim = tmp_path / "keep.txt"
    victim.write_text("safe", encoding="utf-8")
    res = fs.delete_file(str(victim), confirm=True)
    assert res["success"] is False
    assert victim.exists()


def test_edit_outside_root_blocked(jailed, tmp_path):
    victim = tmp_path / "code.py"
    victim.write_text("a = 1\n", encoding="utf-8")
    res = fs.edit_file(str(victim), "a = 1", "a = 2")
    assert res["success"] is False
    assert victim.read_text(encoding="utf-8") == "a = 1\n"


def test_allow_outside_root_bypasses(jailed, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allow_outside_root", True)
    outside = tmp_path / "power.txt"
    assert fs.write_file(str(outside), "ok")["success"] is True
    assert outside.exists()


def test_list_and_search_jailed(jailed, tmp_path):
    assert fs.list_directory(str(tmp_path))["success"] is False
    assert fs.search_files(str(tmp_path), "x")["success"] is False
