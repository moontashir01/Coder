# Multi-File Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Coder create, read, edit, and rewrite *multiple* files in a single turn (e.g. "separate index.html into html/css/js") by planning a list of per-file operations and running each through the existing single-file flow.

**Architecture:** Add a multi-file orchestration layer in front of `_file_op_flow`. When a request is multi-file, `chat()` routes to a new `_multi_file_flow`: it reads the relevant existing files, makes ONE planning LLM call that returns a JSON list of `{filename, action, instruction}`, then executes each item by reusing `_file_op_flow` (which already does create vs surgical-edit vs whole-file-rewrite per file). A per-extension "content guard" string is injected into generation/edit prompts so the 3B model stops writing JS into a `.css` file.

**Tech Stack:** Python 3.11, LangChain Ollama wrappers, pytest (offline, `ScriptedLLM` fakes + `tmp_path`). All new code lives in `app/agent/core.py`; all new tests in `tests/test_multifile.py`. No new dependencies.

**Why this design:** The root cause of the current bug is that `_file_op_flow` ([app/agent/core.py:593](../../../app/agent/core.py)) handles exactly ONE file per turn, so "separate into separate files" only ever touches one file and never strips the inline code from `index.html`. Planning N operations and looping the existing, already-tested flow gives create-many + edit-many + read-many for free.

---

## File Structure

- **Modify** `app/agent/core.py`
  - Add module-level helpers near the other file helpers (~line 120–280): `_EXT_GUARD`, `_extension_guard()`, `_MULTIFILE_VERB_RE`, `wants_multifile()`, `FileOp` dataclass, `_MULTIFILE_PLAN_INSTRUCTIONS`, `_parse_file_plan()`.
  - Add `AgentCore` methods: `_plan_file_ops()`, `_multi_file_flow()`.
  - Inject the extension guard into `_file_op_flow` and `_surgical_edit`.
  - Add a routing branch in `chat()`.
- **Create** `tests/test_multifile.py` — all new tests (mirrors `tests/test_file_flow.py` conventions: `ScriptedLLM`, `monkeypatch.chdir(tmp_path)`, real `write_file`).

Keep every helper in `core.py` (not a new module) to match the established pattern — all file helpers already live there and are tested via one file.

---

## Task 1: Per-extension content guard (fixes "JS written into styles.css")

**Files:**
- Modify: `app/agent/core.py` (add helpers after `_infer_filename`, ~line 145)
- Modify: `app/agent/core.py` — `_file_op_flow` (~line 632) and `_surgical_edit` (~line 685)
- Test: `tests/test_multifile.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_multifile.py`:

```python
"""Tests for multi-file planning, the extension guard, and multi-file orchestration.

All offline: the LLM is a scripted fake, file writes go to tmp_path.
"""
from types import SimpleNamespace

import pytest

from app.agent.core import (
    AgentCore,
    FileOp,
    _extension_guard,
    _parse_file_plan,
    wants_multifile,
)


class ScriptedLLM:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    def invoke(self, messages):
        out = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        return SimpleNamespace(content=out)


def test_extension_guard_css():
    g = _extension_guard("styles.css")
    assert "CSS" in g
    assert "JavaScript" in g or "JS" in g  # tells the model NOT to emit JS


def test_extension_guard_js():
    g = _extension_guard("script.js")
    assert "JavaScript" in g


def test_extension_guard_unknown_is_empty():
    assert _extension_guard("notes.txt") == ""
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_multifile.py -k extension_guard -v`
Expected: FAIL with `ImportError: cannot import name '_extension_guard'` (and `FileOp`, `_parse_file_plan`, `wants_multifile`).

- [ ] **Step 3: Write the minimal implementation**

In `app/agent/core.py`, add after `_infer_filename` (~line 145):

```python
# Per-extension content guard — the 3B model otherwise writes JS into a .css
# file (and vice-versa) when a request mentions several languages at once.
_EXT_GUARD: dict[str, str] = {
    ".css": "This file is CSS. Output ONLY CSS rules and selectors. "
            "Do NOT include any HTML tags or JavaScript.",
    ".js": "This file is JavaScript. Output ONLY JavaScript. "
           "Do NOT include any HTML tags, <script> wrappers, or CSS.",
    ".ts": "This file is TypeScript. Output ONLY TypeScript. No HTML or CSS.",
    ".html": "This file is HTML. Link external CSS with <link rel=\"stylesheet\"> "
             "and external JS with <script src> — do NOT inline large blocks.",
    ".py": "This file is Python. Output ONLY Python source.",
}


def _extension_guard(filename: str) -> str:
    """Return a one-line content rule for the file's extension, or '' if unknown."""
    return _EXT_GUARD.get(Path(filename).suffix.lower(), "")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_multifile.py -k extension_guard -v`
Expected: PASS (3 passed). Other imports in the test module still fail to resolve until later tasks — run with `-k extension_guard` to scope.

- [ ] **Step 5: Wire the guard into the single-file generation prompt**

In `_file_op_flow`, find the block that builds `ctx` (~line 634):

```python
        ctx = f"User request: {user_message}\n\nWorking directory: {workdir}"
        if full_existing:
            ctx += (
                f"\n\nThe file '{filename}' already exists. Apply the requested change "
                f"and return the COMPLETE updated file:\n\n{full_existing[:4000]}"
            )
```

Replace with (adds the guard when a filename is known):

```python
        ctx = f"User request: {user_message}\n\nWorking directory: {workdir}"
        guard = _extension_guard(filename) if filename else ""
        if guard:
            ctx += f"\n\nIMPORTANT: {guard}"
        if full_existing:
            ctx += (
                f"\n\nThe file '{filename}' already exists. Apply the requested change "
                f"and return the COMPLETE updated file:\n\n{full_existing[:4000]}"
            )
```

- [ ] **Step 6: Wire the guard into the surgical-edit prompt**

In `_surgical_edit`, find the `ctx` build (~line 687):

```python
        ctx = (
            f"File: {filename}\nCurrent content:\n{full_content[:6000]}\n\n"
            f"Request: {user_message}\n\n"
            f"Output the SEARCH/REPLACE block(s) now:"
        )
```

Replace with:

```python
        guard = _extension_guard(filename)
        guard_line = f"IMPORTANT: {guard}\n\n" if guard else ""
        ctx = (
            f"File: {filename}\nCurrent content:\n{full_content[:6000]}\n\n"
            f"{guard_line}"
            f"Request: {user_message}\n\n"
            f"Output the SEARCH/REPLACE block(s) now:"
        )
```

- [ ] **Step 7: Run the existing file-flow tests to confirm no regression**

Run: `pytest tests/test_file_flow.py -v`
Expected: PASS (all existing tests green — the guard only appends text, it does not change scripted outputs).

- [ ] **Step 8: Commit**

```bash
git add app/agent/core.py tests/test_multifile.py
git commit -m "feat(coder): per-extension content guard for file generation/edit"
```

---

## Task 2: Multi-file intent detection (`wants_multifile`)

**Files:**
- Modify: `app/agent/core.py` (add after `_wants_file_op`, ~line 88)
- Test: `tests/test_multifile.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_multifile.py`:

```python
@pytest.mark.parametrize("msg", [
    "separate the html, css and js into separate files",
    "split index.html into separate files",
    "extract the styles and scripts into their own files",
    "move the css and javascript out of index.html into separate files",
])
def test_wants_multifile_true(msg):
    assert wants_multifile(msg) is True


@pytest.mark.parametrize("msg", [
    "make me an index.html file",          # single-file create
    "edit index.html to change the title",  # single-file edit
    "write a python function that adds two numbers",
    "explain what a decorator does",
])
def test_wants_multifile_false(msg):
    assert wants_multifile(msg) is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_multifile.py -k wants_multifile -v`
Expected: FAIL with `ImportError: cannot import name 'wants_multifile'`.

- [ ] **Step 3: Write the minimal implementation**

In `app/agent/core.py`, add after `_wants_file_op` (~line 88):

```python
# A separation/restructure verb that implies touching more than one file.
_MULTIFILE_VERB_RE = re.compile(
    r"\b(separate|split|extract|reorganize|reorganise|restructure)\b",
    re.IGNORECASE,
)
_MOVE_INTO_FILES_RE = re.compile(
    r"\bmove\b.*\binto\b.*\bfiles?\b", re.IGNORECASE | re.DOTALL
)
_FILETYPE_RE = re.compile(
    r"\b(html|css|js|javascript|ts|typescript|python|json|scss)\b", re.IGNORECASE
)


def wants_multifile(message: str) -> bool:
    """True when the request implies operating on several files at once.

    Catches "separate/split/extract … files" and "move the css and js into
    separate files". Deliberately tighter than _wants_file_op so ordinary
    single-file create/edit requests still go through _file_op_flow.
    """
    if _MOVE_INTO_FILES_RE.search(message):
        return True
    if not _MULTIFILE_VERB_RE.search(message):
        return False
    if re.search(r"\bfiles\b", message, re.IGNORECASE):  # plural "files"
        return True
    # …or it names two or more distinct languages to pull apart.
    types = {m.lower() for m in _FILETYPE_RE.findall(message)}
    types.discard("ts")  # avoid double-counting typescript/ts overlap noise
    return len(types) >= 2
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_multifile.py -k wants_multifile -v`
Expected: PASS (8 passed).

- [ ] **Step 5: Commit**

```bash
git add app/agent/core.py tests/test_multifile.py
git commit -m "feat(coder): detect multi-file requests (wants_multifile)"
```

---

## Task 3: File-plan data model + parser (`FileOp`, `_parse_file_plan`)

**Files:**
- Modify: `app/agent/core.py` (add after the surgical-edit helpers, ~line 282)
- Test: `tests/test_multifile.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_multifile.py`:

```python
def test_parse_file_plan_basic():
    raw = '''{"files": [
        {"filename": "styles.css", "action": "create", "instruction": "move the css here"},
        {"filename": "index.html", "action": "edit", "instruction": "remove inline css, link styles.css"}
    ]}'''
    ops = _parse_file_plan(raw)
    assert ops == [
        FileOp(filename="styles.css", action="create", instruction="move the css here"),
        FileOp(filename="index.html", action="edit", instruction="remove inline css, link styles.css"),
    ]


def test_parse_file_plan_tolerates_surrounding_prose():
    raw = 'Here is the plan:\n{"files": [{"filename": "a.js", "action": "create", "instruction": "x"}]}\nDone.'
    ops = _parse_file_plan(raw)
    assert ops == [FileOp(filename="a.js", action="create", instruction="x")]


def test_parse_file_plan_defaults_action_to_create():
    raw = '{"files": [{"filename": "new.css", "instruction": "styles"}]}'
    ops = _parse_file_plan(raw)
    assert ops == [FileOp(filename="new.css", action="create", instruction="styles")]


def test_parse_file_plan_skips_entries_without_filename():
    raw = '{"files": [{"action": "create", "instruction": "no name"}, {"filename": "ok.js", "instruction": "y"}]}'
    ops = _parse_file_plan(raw)
    assert ops == [FileOp(filename="ok.js", action="create", instruction="y")]


def test_parse_file_plan_empty_on_garbage():
    assert _parse_file_plan("not json at all") == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_multifile.py -k parse_file_plan -v`
Expected: FAIL with `ImportError: cannot import name 'FileOp'`.

- [ ] **Step 3: Write the minimal implementation**

In `app/agent/core.py`, add `from dataclasses import dataclass` to the top imports (after `import re`), then add after `_apply_search_replace` (~line 282):

```python
# --- Multi-file planning --------------------------------------------------

@dataclass(frozen=True)
class FileOp:
    """One planned per-file operation produced by the multi-file planner."""
    filename: str
    action: str           # "create" | "edit"
    instruction: str


def _parse_file_plan(raw: str) -> list[FileOp]:
    """Parse a planner response of {"files": [{filename, action, instruction}]}.

    Tolerant of prose around the JSON (reuses _extract_json). Entries without a
    filename are skipped; a missing/blank action defaults to "create".
    """
    try:
        data = _extract_json(raw)
    except Exception:
        return []
    items = data.get("files") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    ops: list[FileOp] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("filename") or "").strip()
        if not name:
            continue
        action = str(item.get("action") or "create").strip().lower()
        if action not in ("create", "edit"):
            action = "create"
        ops.append(
            FileOp(
                filename=name,
                action=action,
                instruction=str(item.get("instruction") or "").strip(),
            )
        )
    return ops
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_multifile.py -k parse_file_plan -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add app/agent/core.py tests/test_multifile.py
git commit -m "feat(coder): FileOp model + tolerant _parse_file_plan"
```

---

## Task 4: The planning LLM call (`_plan_file_ops`)

**Files:**
- Modify: `app/agent/core.py` (add `_MULTIFILE_PLAN_INSTRUCTIONS` near `_EDIT_INSTRUCTIONS`, ~line 208; add `AgentCore._plan_file_ops` after `_surgical_edit`, ~line 742)
- Test: `tests/test_multifile.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_multifile.py`:

```python
async def test_plan_file_ops_parses_scripted_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_plan")
    a._llm_direct = ScriptedLLM(['''{"files": [
        {"filename": "styles.css", "action": "create", "instruction": "the css"},
        {"filename": "script.js", "action": "create", "instruction": "the js"},
        {"filename": "index.html", "action": "edit", "instruction": "link them, drop inline"}
    ]}'''])

    ops = await a._plan_file_ops(
        "separate index.html into files",
        context="### index.html\n<html><style>x</style></html>",
    )

    assert [o.filename for o in ops] == ["styles.css", "script.js", "index.html"]
    assert [o.action for o in ops] == ["create", "create", "edit"]


async def test_plan_file_ops_empty_on_llm_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_plan_err")

    class Boom:
        def invoke(self, messages):
            raise RuntimeError("offline")

    a._llm_direct = Boom()
    assert await a._plan_file_ops("separate it", context="") == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_multifile.py -k plan_file_ops -v`
Expected: FAIL with `AttributeError: 'AgentCore' object has no attribute '_plan_file_ops'`.

- [ ] **Step 3: Write the minimal implementation**

In `app/agent/core.py`, add after `_EDIT_INSTRUCTIONS` (~line 208):

```python
_MULTIFILE_PLAN_INSTRUCTIONS = """
You are planning how to split or reorganize code across MULTIPLE files.
Return ONLY a JSON object, nothing else, in exactly this shape:
{"files": [
  {"filename": "<relative path>", "action": "create" | "edit", "instruction": "<what to put in / change about this file>"}
]}

Rules:
- "create" = a brand-new file. "edit" = modify a file that already exists.
- When you move code OUT of an existing file, you MUST include an "edit" entry
  for that existing file whose instruction says to REMOVE the moved code and add
  the link/import (e.g. for index.html: remove the inline <style>/<script> and
  add <link rel="stylesheet" href="styles.css"> and <script src="script.js">).
- Keep each instruction specific and self-contained.
- Output ONLY the JSON. No prose, no markdown fences."""
```

Then add this method to `AgentCore`, after `_surgical_edit` (~line 742):

```python
    async def _plan_file_ops(self, user_message: str, context: str) -> list[FileOp]:
        """One LLM call → an ordered list of per-file operations.

        ``context`` is the text of the existing files relevant to the request
        (so the planner knows what to split out). Returns [] on any failure;
        the caller falls back to the single-file flow.
        """
        messages = [
            SystemMessage(
                content="You are a precise multi-file refactoring planner. "
                "You output only JSON." + _MULTIFILE_PLAN_INSTRUCTIONS
            ),
            HumanMessage(
                content=(
                    f"Request: {user_message}\n\n"
                    f"Existing files:\n{context or '(none)'}\n\n"
                    f"Output the JSON plan now:"
                )
            ),
        ]
        try:
            raw = self._llm_direct.invoke(messages).content
        except Exception:
            return []
        return _parse_file_plan(raw)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_multifile.py -k plan_file_ops -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add app/agent/core.py tests/test_multifile.py
git commit -m "feat(coder): _plan_file_ops planning LLM call"
```

---

## Task 5: The orchestrator (`_multi_file_flow`)

**Files:**
- Modify: `app/agent/core.py` (add `AgentCore._multi_file_flow` after `_plan_file_ops`)
- Test: `tests/test_multifile.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_multifile.py`. This is the end-to-end behavior that fixes the user's bug — `styles.css` and `script.js` get created AND `index.html` gets its inline code stripped, all in one turn:

```python
async def test_multi_file_flow_separates_html(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    index = tmp_path / "index.html"
    index.write_text(
        '<html>\n<style>body{color:red}</style>\n'
        '<script>console.log(1)</script>\n</html>\n',
        encoding="utf-8",
    )

    a = AgentCore(session_id="pytest_multi")

    # 1st _llm_direct call = the PLAN. Subsequent _llm_direct calls = whole-file
    # generations for the two new files (surgical is not used for new files).
    a._llm_direct = ScriptedLLM([
        # plan
        '{"files": ['
        '{"filename": "styles.css", "action": "create", "instruction": "move css"},'
        '{"filename": "script.js", "action": "create", "instruction": "move js"},'
        '{"filename": "index.html", "action": "edit", "instruction": "drop inline, link both"}'
        ']}',
        # create styles.css
        "FILENAME: styles.css\nbody{color:red}",
        # create script.js
        "FILENAME: script.js\nconsole.log(1)",
        # whole-file fallback for index.html IF surgical yields no blocks
        'FILENAME: index.html\n<html>\n<link rel="stylesheet" href="styles.css">\n'
        '<script src="script.js"></script>\n</html>',
    ])
    # Surgical edit on index.html: strip the two inline blocks + add links.
    a._llm_edit = ScriptedLLM([
        "<<<<<<< SEARCH\n<style>body{color:red}</style>\n=======\n"
        '<link rel="stylesheet" href="styles.css">\n>>>>>>> REPLACE\n'
        "<<<<<<< SEARCH\n<script>console.log(1)</script>\n=======\n"
        '<script src="script.js"></script>\n>>>>>>> REPLACE'
    ])

    answer, trace = await a._multi_file_flow(
        "separate index.html into html, css and js files", refs=["index.html"]
    )

    # New files created with the right content
    assert (tmp_path / "styles.css").read_text(encoding="utf-8") == "body{color:red}"
    assert (tmp_path / "script.js").read_text(encoding="utf-8") == "console.log(1)"
    # index.html no longer holds the inline code
    html = index.read_text(encoding="utf-8")
    assert "color:red" not in html
    assert "console.log(1)" not in html
    assert 'href="styles.css"' in html
    assert 'src="script.js"' in html
    # one trace entry per planned file
    assert len(trace) == 3
    assert "3" in answer  # mentions 3 files handled


async def test_multi_file_flow_empty_plan_falls_back(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_multi_fallback")
    # planner returns junk → no ops → flow signals fallback with (None-ish) answer
    a._llm_direct = ScriptedLLM(["not a plan"])
    answer, trace = await a._multi_file_flow("separate things", refs=[])
    assert trace == []
    assert "couldn't" in answer.lower() or "could not" in answer.lower()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_multifile.py -k multi_file_flow -v`
Expected: FAIL with `AttributeError: 'AgentCore' object has no attribute '_multi_file_flow'`.

- [ ] **Step 3: Write the minimal implementation**

In `app/agent/core.py`, add after `_plan_file_ops`:

```python
    async def _multi_file_flow(
        self, user_message: str, refs: list[str]
    ) -> tuple[str, list[dict]]:
        """Plan a set of per-file operations, then run each through _file_op_flow.

        Reads the existing files relevant to the request (the @refs plus any
        file named in the message that exists on disk) so the planner can decide
        what to split out, then executes create/edit for each planned file by
        delegating to the already-tested single-file flow.
        """
        workdir = Path(self._project_path or Path.cwd())

        # Gather context: @refs first, then any existing filename mentioned in text.
        ctx_names: list[str] = list(refs)
        guessed = _extract_filename(user_message)
        if guessed and guessed not in ctx_names:
            ctx_names.append(guessed)
        context = self._read_refs([n for n in ctx_names if (workdir / n).is_file()])

        ops = await self._plan_file_ops(user_message, context)
        if not ops:
            return (
                "I couldn't plan the multi-file change — try naming the files, "
                "e.g. 'split index.html into styles.css and script.js'.",
                [],
            )

        trace: list[dict] = []
        summaries: list[str] = []
        for op in ops:
            # Each op reuses the single-file flow: create → FILENAME gen,
            # edit on an existing file → surgical SEARCH/REPLACE then rewrite.
            sub_msg = op.instruction or user_message
            ans, sub_trace = await self._file_op_flow(sub_msg, target=op.filename)
            trace.extend(sub_trace)
            summaries.append(f"- {op.filename}: {ans}")

        answer = (
            f"Handled {len(ops)} file(s):\n" + "\n".join(summaries)
        )
        return answer, trace
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_multifile.py -k multi_file_flow -v`
Expected: PASS (2 passed).

> Note: in `test_multi_file_flow_separates_html`, the surgical edit on `index.html` succeeds, so the 4th scripted `_llm_direct` output (the whole-file fallback) is simply unused — `ScriptedLLM` clamps to the last output and never errors on extra entries.

- [ ] **Step 5: Commit**

```bash
git add app/agent/core.py tests/test_multifile.py
git commit -m "feat(coder): _multi_file_flow orchestrator (plan + per-file execute)"
```

---

## Task 6: Route multi-file requests in `chat()`

**Files:**
- Modify: `app/agent/core.py` — `chat()` (~line 755)
- Test: `tests/test_multifile.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_multifile.py`:

```python
async def test_chat_routes_to_multifile(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "index.html").write_text(
        "<html><style>a{}</style></html>\n", encoding="utf-8"
    )
    a = AgentCore(session_id="pytest_chat_multi")

    # classify() must not need a live LLM — force a harmless task type.
    monkeypatch.setattr(a.planner, "classify", lambda msg: "file_edit")

    called = {}

    async def fake_multi(message, refs):
        called["message"] = message
        called["refs"] = refs
        return "multi done", [{"tool": "write_file"}]

    monkeypatch.setattr(a, "_multi_file_flow", fake_multi)

    answer, trace = await a.chat("separate index.html into separate files")

    assert answer == "multi done"
    assert called["message"] == "separate index.html into separate files"


async def test_chat_single_file_still_uses_file_op(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_chat_single")
    monkeypatch.setattr(a.planner, "classify", lambda msg: "file_edit")

    multi_called = {"hit": False}

    async def fake_multi(message, refs):
        multi_called["hit"] = True
        return "x", []

    monkeypatch.setattr(a, "_multi_file_flow", fake_multi)
    a._llm_edit = ScriptedLLM(["no blocks"])
    a._llm_direct = ScriptedLLM(["FILENAME: index.html\n<html>new</html>"])

    await a.chat("make me an index.html file")

    assert multi_called["hit"] is False  # single-file requests skip the multi flow
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_multifile.py -k chat_routes_to_multifile -v`
Expected: FAIL — `_multi_file_flow` is not called, so `answer != "multi done"` (the request currently falls into `_file_op_flow`).

- [ ] **Step 3: Write the minimal implementation**

In `app/agent/core.py`, in `chat()`, find (~line 755):

```python
        if _wants_file_op(clean_message) or task_type == "file_edit":
            # Create/update a single file deterministically; an @ref pins the target.
            target = self._resolve_ref(at_refs)
            answer, trace = await self._file_op_flow(clean_message, target=target)
```

Insert a multi-file branch BEFORE it:

```python
        if wants_multifile(clean_message):
            # Plan + execute several file operations in one turn.
            answer, trace = await self._multi_file_flow(clean_message, refs=at_refs)
        elif _wants_file_op(clean_message) or task_type == "file_edit":
            # Create/update a single file deterministically; an @ref pins the target.
            target = self._resolve_ref(at_refs)
            answer, trace = await self._file_op_flow(clean_message, target=target)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_multifile.py -k "chat_routes_to_multifile or chat_single_file" -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Run the whole suite to confirm no regression**

Run: `pytest tests/ -v`
Expected: PASS (all green, including `tests/test_file_flow.py` and `tests/test_agent.py`).

- [ ] **Step 6: Format**

Run: `black app/agent/core.py tests/test_multifile.py && isort app/agent/core.py tests/test_multifile.py`
Expected: files reformatted/clean, no errors.

- [ ] **Step 7: Commit**

```bash
git add app/agent/core.py tests/test_multifile.py
git commit -m "feat(coder): route multi-file requests through _multi_file_flow"
```

---

## Task 7: Manual end-to-end smoke test (real Ollama, no automation)

**Files:** none (manual verification)

- [ ] **Step 1: Start Ollama and the REPL**

Run:
```bash
ollama serve            # in one terminal
python main.py          # in another (from repo root, venv active)
```

- [ ] **Step 2: Build a single-file site**

In the REPL: `make me a nice looking website using html, css and js`
Expected: one `index.html` created in cwd with inline `<style>` and `<script>`.

- [ ] **Step 3: Trigger the multi-file split**

In the REPL: `separate the html, css and js into separate files`
Expected outcome:
- `styles.css` created (CSS only — no JS), `script.js` created (JS only — no HTML).
- `index.html` no longer contains the inline `<style>`/`<script>` bodies; it has `<link rel="stylesheet" href="styles.css">` and `<script src="script.js"></script>`.
- The REPL prints "Handled 3 file(s): …".

- [ ] **Step 4: Confirm on disk**

Run: `ls && cat styles.css | head && cat index.html | head`
Expected: three files present; `styles.css` holds CSS, `index.html` references the externals.

If step 3 produces wrong-language content or a missing edit, that is a prompt-tuning issue in `_MULTIFILE_PLAN_INSTRUCTIONS` / `_EXT_GUARD`, not a structural one — adjust those strings and re-run; the unit tests still guard the wiring.

---

## Self-Review

**1. Spec coverage** — the request was: create / write / read / modify *multiple* files.
- Create multiple → Task 5 loops `_file_op_flow` create path per planned new file. ✓
- Write multiple → same loop calls `write_file` per file (trace has one entry each). ✓
- Read multiple → Task 5 `_read_refs` reads all context files before planning; each edit op re-reads its target inside `_file_op_flow`. ✓
- Modify multiple → edit ops run surgical SEARCH/REPLACE (or whole-file fallback) per existing file. ✓
- The original bug (inline code left in `index.html`, JS written into `.css`) → fixed by Task 1 (guard) + Task 5 (explicit edit op for the source file). ✓

**2. Placeholder scan** — every code step contains complete code; no TODO/TBD; all test bodies are concrete. ✓

**3. Type consistency** — `FileOp(filename, action, instruction)` defined in Task 3 is used identically in Tasks 4–5. `_plan_file_ops(user_message, context)`, `_multi_file_flow(user_message, refs)`, `wants_multifile(message)`, `_extension_guard(filename)` signatures match across all tasks and tests. `_file_op_flow(message, target=...)` matches its existing signature at [app/agent/core.py:593](../../../app/agent/core.py). ✓
