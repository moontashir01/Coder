"""Tests for built-in tools: filesystem, terminal, git."""
import pytest

from app.tools import filesystem as fs
from app.tools import git_tool
from app.tools.terminal import run_command
from config.settings import settings

# ---------------------------------------------------------------------------
# Filesystem tools
# ---------------------------------------------------------------------------

def test_write_then_read_file(tmp_path):
    target = tmp_path / "hello.txt"
    res = fs.write_file(str(target), "hello world")
    assert res["success"] is True
    assert res["error"] is None

    read = fs.read_file(str(target))
    assert read["success"] is True
    assert read["result"] == "hello world"


def test_read_missing_file_returns_error(tmp_path):
    res = fs.read_file(str(tmp_path / "nope.txt"))
    assert res["success"] is False
    assert "not found" in res["error"].lower()


def test_create_file_fails_if_exists(tmp_path):
    target = tmp_path / "a.txt"
    assert fs.create_file(str(target), "x")["success"] is True
    second = fs.create_file(str(target), "y")
    assert second["success"] is False
    assert "exists" in second["error"].lower()
    # original content untouched
    assert fs.read_file(str(target))["result"] == "x"


def test_edit_file_unique_replacement(tmp_path):
    target = tmp_path / "code.py"
    target.write_text("a = 1\nb = 2\n", encoding="utf-8")
    res = fs.edit_file(str(target), "b = 2", "b = 99")
    assert res["success"] is True
    assert target.read_text(encoding="utf-8") == "a = 1\nb = 99\n"


def test_edit_file_ambiguous_is_rejected(tmp_path):
    target = tmp_path / "dup.txt"
    target.write_text("x\nx\n", encoding="utf-8")
    res = fs.edit_file(str(target), "x", "y")
    assert res["success"] is False
    assert "ambiguous" in res["error"].lower()


def test_edit_file_missing_string(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("hello", encoding="utf-8")
    res = fs.edit_file(str(target), "absent", "x")
    assert res["success"] is False
    assert "not found" in res["error"].lower()


def test_delete_file_requires_confirm(tmp_path):
    target = tmp_path / "del.txt"
    target.write_text("data", encoding="utf-8")

    no_confirm = fs.delete_file(str(target))
    assert no_confirm["success"] is False
    assert target.exists()

    confirmed = fs.delete_file(str(target), confirm=True)
    assert confirmed["success"] is True
    assert not target.exists()


def test_list_directory(tmp_path):
    (tmp_path / "one.txt").write_text("1", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    res = fs.list_directory(str(tmp_path))
    assert res["success"] is True
    assert "one.txt" in res["result"]
    assert "sub" in res["result"]


def test_list_directory_recursive(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "deep.txt").write_text("d", encoding="utf-8")
    res = fs.list_directory(str(tmp_path), recursive=True)
    assert "deep.txt" in res["result"]


def test_search_files(tmp_path):
    (tmp_path / "a.py").write_text("def target():\n    pass\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("x = 1\n", encoding="utf-8")
    res = fs.search_files(str(tmp_path), r"def target")
    assert res["success"] is True
    assert "a.py" in res["result"]
    assert "target" in res["result"]


def test_search_files_invalid_regex(tmp_path):
    res = fs.search_files(str(tmp_path), "([unclosed")
    assert res["success"] is False
    assert "regex" in res["error"].lower()


def test_read_file_truncates_over_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "max_read_file_bytes", 10)
    target = tmp_path / "big.txt"
    target.write_text("0123456789ABCDEFGHIJ", encoding="utf-8")
    res = fs.read_file(str(target))
    assert res["success"] is True
    assert res["result"].startswith("0123456789")
    assert "truncated" in res["result"].lower()
    assert "ABCDEF" not in res["result"]


def test_search_files_skips_binary(tmp_path):
    (tmp_path / "code.py").write_text("needle here\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"needle\x00\x00binary")
    res = fs.search_files(str(tmp_path), "needle")
    assert res["success"] is True
    assert "code.py" in res["result"]
    assert "blob.bin" not in res["result"]


def test_search_files_skips_vendored_dirs(tmp_path):
    (tmp_path / "app.py").write_text("token = 1\n", encoding="utf-8")
    vendor = tmp_path / "node_modules"
    vendor.mkdir()
    (vendor / "lib.py").write_text("token = 2\n", encoding="utf-8")
    res = fs.search_files(str(tmp_path), "token")
    assert "app.py" in res["result"]
    assert "node_modules" not in res["result"]


# ---------------------------------------------------------------------------
# Terminal tool
# ---------------------------------------------------------------------------

def test_run_command_echo():
    res = run_command("echo coder_test_marker")
    assert res["success"] is True
    assert "coder_test_marker" in res["result"]
    assert "[exit code] 0" in res["result"]


def test_run_command_blocked():
    res = run_command("sudo rm something")
    assert res["success"] is False
    assert "blocked" in res["error"].lower()


@pytest.mark.parametrize("cmd", ["rm -rf /", "sudo rm x", "dd if=/dev/zero of=/dev/sda", "format c:"])
def test_run_command_blocks_dangerous(cmd):
    res = run_command(cmd)
    assert res["success"] is False
    assert "blocked" in res["error"].lower()


def test_run_command_format_substring_not_blocked():
    # "format" appears only as a method call argument — must NOT be blocked
    res = run_command('python -c "print(\'{}\'.format(42))"')
    assert res["success"] is True
    assert "42" in res["result"]


def test_run_command_nonzero_exit():
    # `python -c "sys.exit(3)"` is portable across platforms
    res = run_command('python -c "import sys; sys.exit(3)"')
    assert res["success"] is False
    assert "Exit code 3" in res["error"]


def test_run_command_timeout():
    res = run_command('python -c "import time; time.sleep(5)"', timeout=1)
    assert res["success"] is False
    assert "timed out" in res["error"].lower()


# --- Step 7: allowlist, network gating ---


def test_allowlist_blocks_unlisted(monkeypatch):
    monkeypatch.setattr(settings, "command_allowlist", ["git", "python"])
    res = run_command("echo hi")
    assert res["success"] is False
    assert "allowlist" in res["error"].lower()


def test_allowlist_permits_listed(monkeypatch):
    monkeypatch.setattr(settings, "command_allowlist", ["echo"])
    res = run_command("echo allowed_marker")
    assert res["success"] is True
    assert "allowed_marker" in res["result"]


def test_allowlist_checks_every_chained_binary(monkeypatch):
    monkeypatch.setattr(settings, "command_allowlist", ["echo"])
    res = run_command("echo hi && curl http://x")
    assert res["success"] is False
    assert "allowlist" in res["error"].lower()


def test_network_command_blocked_by_default(monkeypatch):
    monkeypatch.setattr(settings, "allow_network", False)
    res = run_command("curl http://example.com")
    assert res["success"] is False
    assert "network" in res["error"].lower()


def test_pip_install_flagged_as_network(monkeypatch):
    monkeypatch.setattr(settings, "allow_network", False)
    res = run_command("pip install requests")
    assert res["success"] is False
    assert "network" in res["error"].lower()


def test_network_allowed_with_flag(monkeypatch):
    # With allow_network the gate is off; the command may still fail to run,
    # but it must not be rejected by the network check.
    monkeypatch.setattr(settings, "allow_network", True)
    res = run_command("curl --bogus-flag-xyz")
    assert "network" not in (res["error"] or "").lower()


def test_chained_network_command_blocked(monkeypatch):
    monkeypatch.setattr(settings, "allow_network", False)
    res = run_command("echo hi | wget http://x")
    assert res["success"] is False
    assert "network" in res["error"].lower()


# ---------------------------------------------------------------------------
# Git tool
# ---------------------------------------------------------------------------

@pytest.fixture
def git_repo(tmp_path):
    """Initialise a git repo with a committer identity, or skip if git missing."""
    git = pytest.importorskip("git")
    try:
        repo = git.Repo.init(tmp_path)
        with repo.config_writer() as cw:
            cw.set_value("user", "name", "Tester")
            cw.set_value("user", "email", "tester@example.com")
    except Exception as e:  # git binary not installed
        pytest.skip(f"git unavailable: {e}")
    return repo, tmp_path


def test_git_status_initial(git_repo):
    repo, path = git_repo
    (path / "new.txt").write_text("hi", encoding="utf-8")
    res = git_tool.git_status(str(path))
    assert res["success"] is True
    assert "new.txt" in res["result"]


def test_git_commit_and_log(git_repo):
    repo, path = git_repo
    (path / "file.txt").write_text("content", encoding="utf-8")

    commit = git_tool.git_commit(str(path), "initial commit")
    assert commit["success"] is True
    assert "initial commit" in commit["result"]

    log = git_tool.git_log(str(path))
    assert log["success"] is True
    assert "initial commit" in log["result"]


def test_git_status_clean_after_commit(git_repo):
    repo, path = git_repo
    (path / "file.txt").write_text("content", encoding="utf-8")
    git_tool.git_commit(str(path), "c1")
    res = git_tool.git_status(str(path))
    assert res["success"] is True
    assert "clean" in res["result"].lower()


def test_git_status_non_repo(tmp_path):
    pytest.importorskip("git")
    res = git_tool.git_status(str(tmp_path))
    assert res["success"] is False
    assert "not a git repository" in res["error"].lower()
