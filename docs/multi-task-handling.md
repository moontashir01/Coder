# Coder — Why multi-task prompts get dropped (and how to fix it)

**Symptom:** when you give one prompt containing several instructions
("create `index.html`, add a login form, **and** write a README"), Coder does
only one of them — or collapses them into a single file. The agent isn't
"ignoring" you; the **router only ever runs one thing, once**.

This is a focused companion to [improvement-suggestions.md](improvement-suggestions.md).
Every finding below is grounded in the current code.

> **Status: implemented (2026-07-08).** M1–M6 below are now in the codebase and
> covered by [tests/test_multitask.py](../tests/test_multitask.py) (26 tests) plus
> two live eval tasks. The "Fixes" sections are kept as the design rationale; the
> ✅ notes point at what shipped. Line references predate the change and have drifted.

---

## Root cause: the router is single-shot and mutually exclusive

`AgentCore.chat()` ([core.py:1195](../app/agent/core.py#L1195)) classifies the
message **once** and dispatches to exactly **one** branch, which executes
**once**:

```
chat(msg)
  task_type = planner.classify(msg)          # ONE label for the WHOLE message
  if wants_multifile(msg):        _multi_file_flow(msg)   # split/“N files” only
  elif _wants_file_op(msg):       _file_op_flow(msg)      # exactly ONE file
  elif task_type=="multi_step"    _run_tool_loop(...)     # gated on project loaded
        and project loaded:
  else:                           _direct_answer(msg)     # one prose/code reply
```

There is **no step where a compound request is decomposed into sub-tasks**.
Whatever branch wins runs to completion for *one* unit of work and returns. The
other instructions in the prompt are never scheduled. Concretely:

1. **The common case falls into the single-file path.** Any message with a
   create/edit verb + a file-ish word matches `_wants_file_op`
   ([core.py:94](../app/agent/core.py#L94)) and routes to `_file_op_flow`
   ([core.py:848](../app/agent/core.py#L848)), which does **one** `FILENAME: …`
   generation and writes **one** file. "Create the page **and** add a test
   **and** update the README" → you get the page only.

2. **`wants_multifile` is narrow.** It fires only for *split / separate /
   extract / reorganize* wording or explicit "*N files*" / "`a.css` and `b.js`"
   lists ([core.py:129](../app/agent/core.py#L129)). It catches "split X into
   two files" but **not** "do A, then B, then C" where A/B/C are different
   *actions* (edit this, run that, create the other). Those fall through to the
   single-file or direct-answer path.

3. **The one branch that *can* chain actions is double-gated and easily
   bypassed.** `_run_tool_loop` (native tool calling, the real multi-step
   engine) only runs when `task_type == "multi_step"` **and** a project is
   loaded ([core.py:1225](../app/agent/core.py#L1225)). But the `_wants_file_op`
   regex is checked *first*, so most compound requests that mention a file never
   reach it. And with **no project loaded**, multi-step work is impossible —
   it silently degrades to `_direct_answer`, one reply, no tools.

4. **The decomposition code already exists but is dead.**
   `Planner.plan()` ([planner.py:71](../app/agent/planner.py#L71)) returns an
   ordered `steps` list for `multi_step` tasks — exactly what's needed — but
   `chat()` only ever calls `classify()`, never `plan()`. The capability is
   built and unused.

5. **Even inside the tool loop, completion isn't enforced.** The model is
   trusted to self-decompose within `max_steps=8`
   ([core.py:667](../app/agent/core.py#L667)); a response with *no* tool call is
   treated as "done" ([core.py:693](../app/agent/core.py#L693)) even if only
   part of the request was handled. Nothing checks that **every** sub-task was
   addressed. The system prompt's rule 6 ("if a task needs multiple steps, plan
   them first", [system.md:11](../app/resources/prompts/system.md#L11)) is a
   soft nudge with no teeth.

**In one line:** Coder has no orchestrator. It picks a lane per message instead
of decomposing the message into a to-do list and working the list.

---

## Fixes, in priority order

### M1 — Decompose compound requests before routing 🔴 (M) — *the core fix*
✅ **Shipped.** `chat()` now calls `_split_compound()` (cheap regex splitter);
≥2 sub-tasks run through `_run_subtasks()`, which routes each via the extracted
`_route_one()` and threads a summary of completed work forward. When the cheap
split sees one task but the classifier says `multi_step`, `Planner.decompose()`
(LLM) is the robust fallback. `settings.decompose_multitask` toggles it.

Add a lightweight **task-splitting step** at the top of `chat()`. When a message
holds multiple independent instructions, split it and run each sub-task through
the existing routing, in order, accumulating the trace.

- **Cheap first pass (no LLM):** split on enumerations and imperative
  conjunctions — numbered/bulleted lists, "; ", and " and then / after that /
  also " joining two imperative clauses. If it yields ≥2 actionable clauses,
  loop them.
- **Robust pass (one LLM call):** reuse the *already-built* `Planner.plan()`
  ([planner.py:71](../app/agent/planner.py#L71)) to return ordered steps, then
  execute each `step_description` via a per-step call into the router.
- **Shape:** factor the current branch ladder in `chat()`
  ([core.py:1216-1236](../app/agent/core.py#L1216)) into a private
  `_route_one(sub_msg, …)` and have `chat()` call it once per sub-task. Feed
  each step a short summary of what previous steps produced (like
  `_multi_file_flow` already threads siblings via `extra_context`
  ([core.py:1176](../app/agent/core.py#L1176))) so later steps stay consistent.

This alone fixes the reported symptom: "create the page **and** the test **and**
the README" becomes three routed operations instead of one.

### M2 — Let multi-step work run without a loaded project 🟡 (S)
✅ **Shipped.** The `and self._project_path is not None` gate is gone from
`_route_one`; a `multi_step` request runs the tool loop rooted at cwd even
without a loaded project.

Drop (or relax) the `and self._project_path is not None` gate at
[core.py:1225](../app/agent/core.py#L1225). The tool loop's file tools default to
cwd already; requiring a loaded project means multi-step requests in a bare
folder silently collapse to a single prose answer. At minimum, when
`task_type == "multi_step"` with no project, still run the loop rooted at cwd.

### M3 — Check the file-op path for multiple targets 🟡 (S)
✅ **Largely subsumed by M1.** A compound file request now splits at `chat()`
and each clause routes to `_file_op_flow` independently; `wants_multifile`'s
existing filename-list detection still catches "create a.py and b.py". The
splitter is deliberately conservative (a clause must *start* with an imperative
verb), so ambiguous single-verb phrasings are left as one task on purpose.

Before committing to `_file_op_flow` (single file), test whether the message
implies **more than one file or more than one action** and, if so, prefer
`_multi_file_flow` / decomposition. Widen `wants_multifile`
([core.py:129](../app/agent/core.py#L129)) to also trigger on multiple
imperative verbs sharing distinct targets, not just *split/separate* wording.

### M4 — Enforce completion in the tool loop 🟡 (M)
✅ **Partially shipped.** `max_steps` is now `settings.max_tool_steps` (raised
8 → 12), and hitting the cap returns "Stopped after N rounds (K actions
completed) — the request may not be fully finished" instead of an opaque
message. Completion is primarily enforced by M1 (each sub-task is guaranteed to
run) and M5 (the loop prompt now says "complete ALL of them before a final
answer"). The heavier LLM coverage-recheck is intentionally deferred — it's the
fragile part, and M1+M5 cover the reported symptom.

When the loop ends (final answer or `max_steps`), do a cheap **coverage check**:
ask the model (or a regex over the original enumerated items) "were all parts of
the request completed? list any not yet done." If some remain, continue the loop
with those as an explicit follow-up instead of returning early. Also raise
`max_steps` for genuinely multi-part work, and surface a "handled 2 of 3
requested items" note when it gives up, rather than a silent partial success.

### M5 — Prompt changes so the model *expects* to do everything 🟢 (S)
✅ **Shipped.** `_tool_guidance()` now tells the model to enumerate every
request and complete all of them before a final answer; `system.md` rule 6 says
"list them all first, then complete EVERY one"; `_FILE_GEN_INSTRUCTIONS` notes
the caller splits multi-file work so this path stays one-file.

Prompt-only, no routing change — do these regardless of M1–M4:

- **`_tool_guidance()`** ([core.py:40](../app/agent/core.py#L40)): add
  "The user's message may contain **several distinct requests**. Enumerate every
  one first, then use tools to complete **all** of them before you give a final
  answer. Do not stop after the first."
- **`system.md`** ([system.md:11](../app/resources/prompts/system.md#L11)):
  strengthen rule 6 to "**If the request contains multiple tasks, list them all,
  then complete each — never address only the first.**" Keep it behavioral (no
  tool-protocol text — see the CLAUDE.md warning).
- **`_FILE_GEN_INSTRUCTIONS`** ([core.py:175](../app/agent/core.py#L175)): note
  that this path writes exactly one file, so if the request needs several files
  the caller (M1/M3) must split first — this keeps the single-file prompt honest.

### M6 — Show the user the plan (UX) 🟢 (S)
✅ **Shipped.** `AgentCore.split_tasks()` is a public preview; the REPL's
`_agent_turn` prints a "Plan" panel before executing when a request decomposes,
and `/plan <task>` shows both the regex split and the LLM planner's steps
without running anything.

Once decomposition exists, print the derived to-do list before executing (a
`/plan`-style preview) and tick items off as they complete. This makes partial
completion visible instead of mysterious, and lets the user catch a bad split
early. Ties into `get_plan()` ([core.py:1241](../app/agent/core.py#L1241)),
which is already exposed but unused by the REPL.

---

## Suggested order

| # | Item | Effort | Status | Why |
|---|------|--------|--------|-----|
| 1 | **M5** prompt: "do every task" | S | ✅ done | Zero-risk, helps immediately |
| 2 | **M1** decompose before routing | M | ✅ done | The actual fix; unblocks everything |
| 3 | **M2** allow multi-step without a project | S | ✅ done | Removes a silent dead-end |
| 4 | **M3** detect multiple targets pre-route | S | ✅ subsumed by M1 | Stops the single-file collapse |
| 5 | **M4** enforce loop completion | M | ◑ config+message (recheck deferred) | Guarantees "all parts done" |
| 6 | **M6** show the plan / progress | S | ✅ done | Makes behavior visible |

**Testing:** add eval tasks to `evals/tasks.py` for compound prompts — e.g.
"create `a.py` and `b.py` and a README", "make the file **and** run the tests" —
asserting **all** expected artifacts exist, not just the first. Today's golden
suite already caught a multi-file routing bug once; a "multi-task compliance"
case would guard M1–M4 against regressions.

---

*Grounded in a read of `app/agent/core.py`, `planner.py`, and
`app/resources/prompts/system.md`; line references may drift as code evolves.*
