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
# search_files ranking + caps — the best matches, not the first 2000 chars
# ---------------------------------------------------------------------------


def test_search_ranks_definition_line_above_mention(tmp_path):
    # "zz_" prefix makes the mention file sort FIRST in the walk — the old
    # first-N-chars behaviour would have led with it.
    (tmp_path / "a_caller.py").write_text("x = handle_auth()\n", encoding="utf-8")
    (tmp_path / "zz_impl.py").write_text(
        "def handle_auth():\n    pass\n", encoding="utf-8"
    )
    res = fs.search_files(str(tmp_path), "handle_auth")
    first = res["result"].splitlines()[0]
    assert "zz_impl.py" in first
    assert "def handle_auth" in first


def test_search_ranks_filename_hit_above_unrelated_file(tmp_path):
    (tmp_path / "a_random.py").write_text("login = True\n", encoding="utf-8")
    (tmp_path / "zz_login.py").write_text("login = False\n", encoding="utf-8")
    res = fs.search_files(str(tmp_path), "login")
    assert "zz_login.py" in res["result"].splitlines()[0]


def test_search_prefers_shallow_over_deep(tmp_path):
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "deep.py").write_text("needle = 1\n", encoding="utf-8")
    (tmp_path / "shallow.py").write_text("needle = 2\n", encoding="utf-8")
    res = fs.search_files(str(tmp_path), "needle")
    assert "shallow.py" in res["result"].splitlines()[0]


def test_search_caps_matches_and_counts_the_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "max_search_matches", 3)
    for i in range(5):
        (tmp_path / f"f{i}.py").write_text("needle\nneedle\n", encoding="utf-8")
    res = fs.search_files(str(tmp_path), "needle")
    lines = res["result"].splitlines()
    assert len(lines) == 4  # 3 matches + the dropped-count note
    assert "7 more match(es) in" in lines[-1]
    assert "narrow the pattern" in lines[-1]


def test_search_under_cap_has_no_dropped_note(tmp_path):
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")
    res = fs.search_files(str(tmp_path), "needle")
    assert "more match" not in res["result"]


def test_search_truncates_a_minified_line(tmp_path):
    (tmp_path / "app.min.js").write_text(
        "var needle=" + "x" * 5000 + ";\n", encoding="utf-8"
    )
    res = fs.search_files(str(tmp_path), "needle")
    first = res["result"].splitlines()[0]
    assert "x" * 250 not in first  # the 5000-char body was cut
    assert first.endswith("…")


def test_search_ranking_is_deterministic(tmp_path):
    for i in range(4):
        (tmp_path / f"f{i}.py").write_text("needle\n", encoding="utf-8")
    a = fs.search_files(str(tmp_path), "needle")["result"]
    b = fs.search_files(str(tmp_path), "needle")["result"]
    assert a == b


# ---------------------------------------------------------------------------
# list_directory caps — recursive skips vendored dirs, both count what's cut
# ---------------------------------------------------------------------------


def test_list_recursive_skips_vendored_and_says_so(tmp_path):
    (tmp_path / "app.py").write_text("x", encoding="utf-8")
    vendor = tmp_path / "node_modules" / "lib"
    vendor.mkdir(parents=True)
    (vendor / "index.js").write_text("y", encoding="utf-8")
    res = fs.list_directory(str(tmp_path), recursive=True)
    assert "app.py" in res["result"]
    assert "index.js" not in res["result"]
    assert "skipped" in res["result"]


def test_list_non_recursive_still_shows_vendored_entry(tmp_path):
    # One level down is the truth about THIS directory — only the recursive
    # flood is filtered.
    (tmp_path / "node_modules").mkdir()
    res = fs.list_directory(str(tmp_path))
    assert "node_modules" in res["result"]


def test_list_caps_entries_and_counts_the_rest(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "max_list_entries", 3)
    for i in range(6):
        (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
    res = fs.list_directory(str(tmp_path))
    lines = res["result"].splitlines()
    assert len(lines) == 4  # 3 entries + the count note
    assert "3 more entries not shown" in lines[-1]
    assert "subdirectory" in lines[-1]


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


@pytest.mark.parametrize(
    "cmd", ["rm -rf /", "sudo rm x", "dd if=/dev/zero of=/dev/sda", "format c:"]
)
def test_run_command_blocks_dangerous(cmd):
    res = run_command(cmd)
    assert res["success"] is False
    assert "blocked" in res["error"].lower()


def test_run_command_format_substring_not_blocked():
    # "format" appears only as a method call argument — must NOT be blocked
    res = run_command("python -c \"print('{}'.format(42))\"")
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


# ---------------------------------------------------------------------------
# Tolerant editing + apply_diff
#
# The tools are the tool loop's only way to change a file without replacing it.
# Exact-only matching meant a misquoted indent was refused here while the
# agent's own editor would have applied the same edit — and a refused edit is
# answered by a 7B with write_file and the whole file regenerated.
# ---------------------------------------------------------------------------


def test_edit_file_tolerates_a_misquoted_indent(tmp_path):
    target = tmp_path / "code.py"
    target.write_text('def greet(name):\n    return "hi"\n', encoding="utf-8")
    # The model dropped the file's leading indentation when copying old_str.
    res = fs.edit_file(str(target), 'return "hi"', 'return "hello"')
    assert res["success"] is True
    assert (
        target.read_text(encoding="utf-8") == 'def greet(name):\n    return "hello"\n'
    )


def test_edit_file_strips_a_line_number_gutter(tmp_path):
    target = tmp_path / "code.py"
    target.write_text("a = 1\nb = 2\n", encoding="utf-8")
    res = fs.edit_file(str(target), "   2 | b = 2", "b = 99")
    assert res["success"] is True
    assert target.read_text(encoding="utf-8") == "a = 1\nb = 99\n"


def test_edit_file_failure_shows_the_nearest_text(tmp_path):
    """An error the next call can act on, rather than one that ends the turn."""
    target = tmp_path / "code.py"
    target.write_text(
        "def alpha():\n    return 1\n\ndef beta():\n    return 2\n", "utf-8"
    )
    res = fs.edit_file(str(target), "def beta(request):\n    return compute(2)", "x")
    assert res["success"] is False
    assert "not found" in res["error"].lower()
    assert "def beta():" in res["error"]  # the real lines, numbered


def test_edit_file_still_refuses_an_ambiguous_target(tmp_path):
    target = tmp_path / "dup.txt"
    target.write_text("x\nx\n", encoding="utf-8")
    res = fs.edit_file(str(target), "x", "y")
    assert res["success"] is False
    assert "ambiguous" in res["error"].lower()
    assert target.read_text(encoding="utf-8") == "x\nx\n"


def test_apply_diff_applies_several_edits_at_once(tmp_path):
    target = tmp_path / "code.py"
    target.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    res = fs.apply_diff(
        str(target),
        [
            {"search": "a = 1", "replace": "a = 11"},
            {"search": "c = 3", "replace": "c = 33"},
        ],
    )
    assert res["success"] is True
    assert target.read_text(encoding="utf-8") == "a = 11\nb = 2\nc = 33\n"


def test_apply_diff_is_all_or_nothing(tmp_path):
    """A half-applied multi-edit leaves a file nobody planned."""
    target = tmp_path / "code.py"
    original = "a = 1\nb = 2\n"
    target.write_text(original, encoding="utf-8")
    res = fs.apply_diff(
        str(target),
        [
            {"search": "a = 1", "replace": "a = 11"},
            {"search": "zzz = 9", "replace": "zzz = 0"},
        ],
    )
    assert res["success"] is False
    assert "edits[1]" in res["error"]
    assert target.read_text(encoding="utf-8") == original


def test_apply_diff_rejects_an_empty_edit_list(tmp_path):
    target = tmp_path / "code.py"
    target.write_text("a = 1\n", encoding="utf-8")
    assert fs.apply_diff(str(target), [])["success"] is False


def test_apply_diff_backs_up_before_writing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "backups_dir", str(tmp_path / "backups"))
    target = tmp_path / "code.py"
    target.write_text("a = 1\n", encoding="utf-8")
    fs.apply_diff(str(target), [{"search": "a = 1", "replace": "a = 2"}])
    undone = fs.undo_write(str(target))
    assert undone["success"] is True
    assert target.read_text(encoding="utf-8") == "a = 1\n"
