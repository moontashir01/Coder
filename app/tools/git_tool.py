from pathlib import Path
from typing import Any

import git
from git import InvalidGitRepositoryError, Repo

ToolResult = dict[str, Any]


def _ok(result: str) -> ToolResult:
    return {"success": True, "result": result, "error": None}


def _err(error: str) -> ToolResult:
    return {"success": False, "result": "", "error": error}


def _get_repo(repo_path: str) -> Repo:
    return Repo(str(Path(repo_path).resolve()), search_parent_directories=True)


def git_status(repo_path: str) -> ToolResult:
    try:
        repo = _get_repo(repo_path)

        try:
            branch = repo.active_branch.name
        except TypeError:
            branch = "(no branch / detached HEAD)"

        lines: list[str] = [f"Branch: {branch}"]

        # is_dirty / diff("HEAD") are meaningless on an empty repo
        try:
            has_head = bool(repo.head.commit)
        except (ValueError, Exception):
            has_head = False

        if has_head and repo.is_dirty(untracked_files=True):
            for item in repo.index.diff(None):
                lines.append(f"  M  {item.a_path}")
            for item in repo.index.diff("HEAD"):
                lines.append(f"  S  {item.a_path}")
            for path in repo.untracked_files:
                lines.append(f"  ?  {path}")
        elif not has_head:
            for path in repo.untracked_files:
                lines.append(f"  ?  {path}")
            lines.append("(initial commit — no HEAD yet)")
        else:
            lines.append("Working tree clean")

        return _ok("\n".join(lines))
    except InvalidGitRepositoryError:
        return _err(f"Not a git repository: {repo_path}")
    except Exception as e:
        return _err(str(e))


def git_diff(repo_path: str, file: str | None = None) -> ToolResult:
    try:
        repo = _get_repo(repo_path)
        if file:
            diff = repo.git.diff(file)
        else:
            diff = repo.git.diff()
        return _ok(diff or "(no changes)")
    except InvalidGitRepositoryError:
        return _err(f"Not a git repository: {repo_path}")
    except Exception as e:
        return _err(str(e))


def git_commit(repo_path: str, message: str) -> ToolResult:
    try:
        repo = _get_repo(repo_path)
        repo.git.add("--all")

        # Detect initial commit (no HEAD yet)
        is_initial = False
        try:
            repo.head.commit  # raises ValueError on empty repo
        except (ValueError, Exception):
            is_initial = True

        if not is_initial:
            staged = repo.index.diff("HEAD")
            if not staged and not repo.untracked_files:
                return _err("Nothing to commit — working tree is clean")

        # pass parent_commits=[] for the very first commit
        commit = repo.index.commit(
            message,
            parent_commits=[] if is_initial else None,
        )
        return _ok(f"Committed {commit.hexsha[:8]}: {message}")
    except InvalidGitRepositoryError:
        return _err(f"Not a git repository: {repo_path}")
    except Exception as e:
        return _err(str(e))


def git_log(repo_path: str, n: int = 10) -> ToolResult:
    try:
        repo = _get_repo(repo_path)
        lines: list[str] = []
        for commit in repo.iter_commits(max_count=n):
            short_hash = commit.hexsha[:8]
            author = commit.author.name
            date = commit.committed_datetime.strftime("%Y-%m-%d %H:%M")
            lines.append(f"{short_hash}  {date}  {author}  {commit.summary}")
        return _ok("\n".join(lines) if lines else "(no commits)")
    except InvalidGitRepositoryError:
        return _err(f"Not a git repository: {repo_path}")
    except Exception as e:
        return _err(str(e))
