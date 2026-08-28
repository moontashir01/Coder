import difflib
import re
import time
from itertools import count
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from app.agent.patch import (
    apply_block,
    apply_edits,
    nearest_region,
    strip_line_numbers,
)
from app.agent.scope import effective_sandbox_root
from config.settings import settings

ToolResult = dict[str, Any]


def _ok(result: str) -> ToolResult:
    return {"success": True, "result": result, "error": None}


def _err(error: str) -> ToolResult:
    return {"success": False, "result": "", "error": error}


# ---------------------------------------------------------------------------
# Path jail (Step 5 / S2): resolve every caller-supplied path and reject any
# that escapes the sandbox root, unless allow_outside_root is set. The
# jail is inert when the root is None (tests / library use) so importing
# the tools imposes no policy; main.py + load_project set the root at runtime.
#
# T1: the root comes from `scope.effective_sandbox_root()`, not from the
# setting directly — with two front-ends, two turns on two projects can be in
# flight at once, and a process-global cannot answer "which project" for both.
# The setting is still the fallback and still what a single-front-end session
# uses.
# ---------------------------------------------------------------------------


def _jail_check(path: str) -> str | None:
    """Return an error string if ``path`` escapes the sandbox root, else None."""
    sandbox_root = effective_sandbox_root()
    if settings.allow_outside_root or sandbox_root is None:
        return None
    root = Path(sandbox_root).resolve()
    try:
        resolved = Path(path).resolve()
    except OSError as e:
        return f"Cannot resolve path {path}: {e}"
    if resolved == root or root in resolved.parents:
        return None
    return (
        f"Path escapes the project root: {path}\n"
        f"(root: {root}). Launch with --allow-outside-root to permit this."
    )


# ---------------------------------------------------------------------------
# Safe writes (Tier 3 #8): every mutating tool backs up the previous content
# into the backup root before touching the file; undo_write restores and
# consumes the most recent backup, so repeated undos walk back through history.
# The original absolute path is URL-quoted into the backup filename after the
# first "__" (quote() never emits "_", so "__" splits unambiguously).
#
# Per-project scoping (Step 10 / C3): when a project is loaded, backups live in
# `<sandbox_root>/.coder_backups/` so `/undo` in one project can never restore a
# file from another. Without a loaded project (or an absolute backups_dir) the
# relative default resolves against cwd, preserving the old behavior.
# ---------------------------------------------------------------------------

_backup_seq = count()


def _backup_root() -> Path:
    base = Path(settings.backups_dir)
    sandbox_root = effective_sandbox_root()
    if sandbox_root is not None and not base.is_absolute():
        return Path(sandbox_root) / base
    return base


def _backup_file(p: Path) -> None:
    """Snapshot p's current content. Raises on failure — callers must treat a
    failed backup as a failed mutation rather than proceed and lose data."""
    root = _backup_root()
    root.mkdir(parents=True, exist_ok=True)
    encoded = quote(str(p.resolve()), safe="")
    name = f"{time.time_ns():020d}-{next(_backup_seq) % 1_000_000:06d}__{encoded}"
    (root / name).write_bytes(p.read_bytes())
    _prune_backups(root)


def _prune_backups(root: Path) -> None:
    backups = sorted(root.iterdir(), key=lambda b: b.name)
    excess = len(backups) - settings.max_write_backups
    for old in backups[:excess] if excess > 0 else []:
        try:
            old.unlink()
        except OSError:
            pass  # pruning is best-effort; a stale backup is harmless


def _original_path(backup: Path) -> str | None:
    parts = backup.name.split("__", 1)
    return unquote(parts[1]) if len(parts) == 2 else None


def _attach_diff(res: ToolResult, old: str, new: str, path: str) -> ToolResult:
    """Add a unified diff of a mutating write to the tool result (Tier 3 #8).

    The diff rides on an extra "diff" key: the tool loop only feeds
    result["result"] back to the model, so this is display-only for the REPL.
    """
    diff = "\n".join(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )
    if diff:
        added = sum(
            1
            for l in diff.splitlines()
            if l.startswith("+") and not l.startswith("+++")
        )
        removed = sum(
            1
            for l in diff.splitlines()
            if l.startswith("-") and not l.startswith("---")
        )
        res["diff"] = diff
        res["result"] += f" (+{added}/-{removed} lines)"
    return res


def undo_write(path: str | None = None) -> ToolResult:
    """Restore the most recent backup; with ``path``, the most recent backup
    of that file. The used backup is deleted (undo again → previous state)."""
    try:
        root = _backup_root()
        backups = (
            sorted(
                (b for b in root.iterdir() if _original_path(b)), key=lambda b: b.name
            )
            if root.exists()
            else []
        )
        if path is not None:
            wanted = str(Path(path).resolve())
            backups = [b for b in backups if _original_path(b) == wanted]
        if not backups:
            target = f" for {path}" if path else ""
            return _err(f"No backup to undo{target} in {root}.")
        latest = backups[-1]
        original = Path(_original_path(latest))
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(latest.read_bytes())
        latest.unlink()
        return _ok(f"Restored {original} from backup ({root}).")
    except Exception as e:
        return _err(str(e))


# Directories never worth searching/reading through (mirrors the indexer skips).
_IGNORE_DIRS = {"__pycache__", "node_modules", ".git", ".venv"}


def _is_binary(p: Path) -> bool:
    """Heuristic: a NUL byte in the first 1 KiB means binary (mirrors git)."""
    try:
        with p.open("rb") as fh:
            return b"\x00" in fh.read(1024)
    except OSError:
        return False


def read_file(path: str) -> ToolResult:
    jail = _jail_check(path)
    if jail:
        return _err(jail)
    try:
        p = Path(path)
        cap = settings.max_read_file_bytes
        with p.open("rb") as fh:
            data = fh.read(cap + 1)
        truncated = len(data) > cap
        content = data[:cap].decode("utf-8", errors="replace")
        if truncated:
            total = p.stat().st_size
            content += f"\n... [truncated — file is {total} bytes, showing first {cap}]"
        return _ok(content)
    except FileNotFoundError:
        return _err(f"File not found: {path}")
    except Exception as e:
        return _err(str(e))


def write_file(path: str, content: str) -> ToolResult:
    jail = _jail_check(path)
    if jail:
        return _err(jail)
    try:
        p = Path(path)
        old_content: str | None = None
        if p.is_file():
            old_content = p.read_text(encoding="utf-8", errors="replace")
            _backup_file(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        res = _ok(f"Written {len(content)} bytes to {path}")
        if old_content is not None:
            res = _attach_diff(res, old_content, content, path)
        return res
    except Exception as e:
        return _err(str(e))


def _no_match_error(path: str, original: str, old_str: str) -> str:
    """Why the edit missed — and the text it came closest to, numbered.

    Told only "String not found", a small model's next move is `write_file`
    with the whole file regenerated, which is how a one-line change becomes a
    truncated file. Shown the real lines, it quotes them back and the second
    call lands. The region is computed deterministically (`patch.nearest_region`)
    and omitted entirely when nothing is close enough to be worth showing — a
    wrong region is a wrong instruction.
    """
    msg = f"String not found in {path}: {old_str[:80]!r}"
    near = nearest_region(original, old_str)
    if near:
        msg += (
            "\nThe closest text in the file is below. Copy old_str from it "
            "verbatim (without the line-number gutter):\n" + near
        )
    return msg


def edit_file(path: str, old_str: str, new_str: str) -> ToolResult:
    """Replace one unique block of text in a file.

    Matching is the shared ladder in `app/agent/patch.py`: exact first, then
    whitespace-tolerant, then a unique multi-line fuzzy match. Before that this
    was exact-only, so a 7B that misquoted the indentation got a refusal and
    answered it by rewriting the file — the agent's own editor would have
    applied the very same edit. Ambiguity is still refused, never guessed.
    """
    jail = _jail_check(path)
    if jail:
        return _err(jail)
    try:
        p = Path(path)
        original = p.read_text(encoding="utf-8", errors="replace")
        old_str = strip_line_numbers(old_str)
        if not old_str:
            return _err("old_str is empty — nothing to find.")
        count = original.count(old_str)
        if count > 1:
            return _err(
                f"Ambiguous edit: {count} occurrences of the target string in {path}. "
                "Provide more context to make it unique."
            )
        if count == 1:
            updated = original.replace(old_str, new_str, 1)
        else:
            patched = apply_block(original, old_str, new_str)
            if patched is None:
                return _err(_no_match_error(path, original, old_str))
            updated = patched
        _backup_file(p)  # only after validation — a rejected edit leaves no backup
        p.write_text(updated, encoding="utf-8")
        return _attach_diff(
            _ok(f"Edited {path}: replaced 1 occurrence"), original, updated, path
        )
    except FileNotFoundError:
        return _err(f"File not found: {path}")
    except Exception as e:
        return _err(str(e))


def apply_diff(path: str, edits: list[dict] | None = None) -> ToolResult:
    """Apply several search/replace edits to one file, all or nothing.

    All-or-nothing is the point of having this beside `edit_file`: a partly
    applied multi-edit leaves a file nobody planned, and the model cannot see
    what state it is now in, so its next move is a full rewrite. Either every
    edit matched and the file is written once, or nothing is touched and the
    report names which edit missed and what it came closest to.
    """
    jail = _jail_check(path)
    if jail:
        return _err(jail)
    if not edits:
        return _err("apply_diff needs a non-empty 'edits' list.")
    pairs: list[tuple[str, str]] = []
    for i, e in enumerate(edits):
        if not isinstance(e, dict):
            return _err(f"edits[{i}] is not an object with 'search'/'replace'.")
        search = e.get("search", e.get("old_str", ""))
        replace = e.get("replace", e.get("new_str", ""))
        if not isinstance(search, str) or not isinstance(replace, str):
            return _err(f"edits[{i}]: 'search' and 'replace' must both be strings.")
        if not search.strip():
            return _err(f"edits[{i}]: 'search' is empty — nothing to find.")
        pairs.append((search, replace))
    try:
        p = Path(path)
        original = p.read_text(encoding="utf-8", errors="replace")
        updated, applied, failed = apply_edits(original, pairs)
        if failed:
            report = [
                f"Applied nothing: {len(failed)} of {len(pairs)} edit(s) did not "
                f"match {path}."
            ]
            for i in failed:
                report.append(
                    f"\nedits[{i}] search did not match: "
                    f"{strip_line_numbers(pairs[i][0]).splitlines()[0][:80]!r}"
                )
                near = nearest_region(original, pairs[i][0])
                if near:
                    report.append("closest text in the file:\n" + near)
            return _err("\n".join(report))
        _backup_file(p)
        p.write_text(updated, encoding="utf-8")
        return _attach_diff(
            _ok(f"Edited {path}: applied {len(applied)} edit(s)"),
            original,
            updated,
            path,
        )
    except FileNotFoundError:
        return _err(f"File not found: {path}")
    except Exception as e:
        return _err(str(e))


def create_file(path: str, content: str = "") -> ToolResult:
    jail = _jail_check(path)
    if jail:
        return _err(jail)
    try:
        p = Path(path)
        if p.exists():
            return _err(f"File already exists: {path}. Use write_file to overwrite.")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return _ok(f"Created {path}")
    except Exception as e:
        return _err(str(e))


def delete_file(path: str, confirm: bool = False) -> ToolResult:
    if not confirm:
        return _err(
            f"delete_file requires confirm=True to prevent accidental deletion of {path}"
        )
    jail = _jail_check(path)
    if jail:
        return _err(jail)
    try:
        p = Path(path)
        if not p.exists():
            return _err(f"File not found: {path}")
        if p.is_file():
            _backup_file(p)
        p.unlink()
        return _ok(f"Deleted {path}")
    except Exception as e:
        return _err(str(e))


def list_directory(path: str, recursive: bool = False) -> ToolResult:
    jail = _jail_check(path)
    if jail:
        return _err(jail)
    try:
        p = Path(path)
        if not p.exists():
            return _err(f"Path not found: {path}")
        if not p.is_dir():
            return _err(f"Not a directory: {path}")

        entries = sorted(p.rglob("*") if recursive else p.iterdir())
        lines: list[str] = []
        skipped_vendored = 0
        for entry in entries:
            rel = entry.relative_to(p)
            # A recursive listing of a real project is dominated by
            # node_modules/.git/__pycache__ — thousands of entries the model
            # read once and can do nothing with, evicting the context the turn
            # needs (the indexer and search_files already skip these for the
            # same reason). One level down (non-recursive) stays unfiltered:
            # the caller asked what THIS directory holds, so show the truth.
            if recursive and any(
                part in _IGNORE_DIRS or part.startswith(".") for part in rel.parts
            ):
                skipped_vendored += 1
                continue
            prefix = "DIR  " if entry.is_dir() else "FILE "
            lines.append(f"{prefix}{rel}")

        # Cap the listing, and COUNT the remainder — "... [context truncated]"
        # tells the model nothing, while a number tells it to list a
        # subdirectory instead.
        cap = settings.max_list_entries
        dropped = len(lines) - cap
        if dropped > 0:
            lines = lines[:cap]
            lines.append(
                f"... {dropped} more entr{'y' if dropped == 1 else 'ies'} not "
                f"shown — list a subdirectory to see them."
            )
        if skipped_vendored:
            lines.append(
                f"({skipped_vendored} entries in vendored/hidden dirs skipped)"
            )

        return _ok("\n".join(lines) if lines else "(empty directory)")
    except Exception as e:
        return _err(str(e))


# search_files ranking: pick the matches worth the model's context window,
# deterministically. The tool used to return EVERY matching line and the tool
# loop then kept the first 2000 characters — i.e. whatever the sorted walk
# found first, usually the alphabetically earliest folder rather than the
# relevant hit. Ranking is pure sorting (no LLM, no latency): a definition
# line beats a mention, a hit in a file NAMED like the pattern beats one deep
# in an unrelated file, and shallow paths beat deep ones. Ties keep walk
# order, so two runs of the same search return the same answer.
_DEF_LINE_RE = re.compile(
    r"^\s*(?:async\s+def|def|class|function|const|let|var|interface|type|"
    r"public|private|export)\b"
    r"|^\s*@\w+"  # a decorator line (e.g. @app.route) is a definition site
)
_MAX_MATCH_LINE_CHARS = 200  # one minified-JS line must not eat the budget


def _pattern_tokens(pattern: str) -> set[str]:
    """The identifier-ish words in a regex, for filename affinity. Regex
    syntax contributes nothing (`\\bdef\\b` yields only sane tokens because
    escapes are split away from the word characters)."""
    return {t.lower() for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", pattern)}


def _match_score(rel_parts: tuple[str, ...], line: str, tokens: set[str]) -> float:
    stem = Path(rel_parts[-1]).stem.lower() if rel_parts else ""
    score = 0.0
    if tokens and any(t in stem for t in tokens):
        score += 3.0
    if _DEF_LINE_RE.match(line):
        score += 2.0
    score -= 0.25 * (len(rel_parts) - 1)  # depth penalty
    return score


def search_files(path: str, pattern: str) -> ToolResult:
    jail = _jail_check(path)
    if jail:
        return _err(jail)
    try:
        root = Path(path)
        if not root.exists():
            return _err(f"Path not found: {path}")

        regex = re.compile(pattern)
        tokens = _pattern_tokens(pattern)
        # (score, walk_index, rendered_line, file) per match; scored now,
        # ranked once at the end.
        scored: list[tuple[float, int, str, Path]] = []
        order = count()

        is_dir = root.is_dir()
        targets = root.rglob("*") if is_dir else [root]
        for file in targets:
            if not file.is_file():
                continue
            # Skip vendored/hidden dirs and binary files (C4). When searching a
            # single file directly, honor the caller and don't second-guess it.
            rel_parts = (file.name,)
            if is_dir:
                rel_parts = file.relative_to(root).parts
                if any(
                    part in _IGNORE_DIRS or part.startswith(".")
                    for part in rel_parts[:-1]
                ):
                    continue
                if _is_binary(file):
                    continue
            try:
                for i, line in enumerate(
                    file.read_text(encoding="utf-8", errors="replace").splitlines(), 1
                ):
                    if regex.search(line):
                        text = line.rstrip()
                        if len(text) > _MAX_MATCH_LINE_CHARS:
                            text = text[:_MAX_MATCH_LINE_CHARS] + "…"
                        scored.append(
                            (
                                _match_score(rel_parts, line, tokens),
                                next(order),
                                f"{file}:{i}: {text}",
                                file,
                            )
                        )
            except Exception:
                continue

        if not scored:
            return _ok(f"No matches for pattern {pattern!r} in {path}")

        # Best first; ties in walk order (stable — same search, same answer).
        scored.sort(key=lambda m: (-m[0], m[1]))
        cap = settings.max_search_matches
        kept, dropped = scored[:cap], scored[cap:]
        lines = [m[2] for m in kept]
        if dropped:
            in_files = len({m[3] for m in dropped})
            lines.append(
                f"... {len(dropped)} more match(es) in {in_files} file(s) not "
                f"shown — narrow the pattern or search a subdirectory."
            )
        return _ok("\n".join(lines))
    except re.error as e:
        return _err(f"Invalid regex pattern: {e}")
    except Exception as e:
        return _err(str(e))
