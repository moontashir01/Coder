# Coder — Where it's weak (an honest assessment)

My take after reading the code and running it live (including the "Daraz" multi-page
build twice). This is deliberately opinionated and focuses on the **deepest** weaknesses —
the ones that limit how good the product can get — not a lint list. For the broad structural
audit (security defaults, packaging, CI, performance) see
[improvement-suggestions.md](improvement-suggestions.md); I don't repeat those here.

> Balance first: the architecture is genuinely clever. The three-path router, verify-and-repair,
> surgical edits, per-file content threading, safe writes, the path jail, and the offline design
> are all solid engineering. The weaknesses below are mostly *consequences of one root constraint*,
> not sloppiness.

---

## 1. The root weakness: everything hangs off a 7B local model, and most of the code is scaffolding to compensate 🔴

This is the big one; almost everything else is downstream of it.

Coder's whole value is "an offline coding assistant." But an offline assistant is only as good as
the local model, and `qwen2.5-coder:7b` is not reliable enough to just *use* — so the codebase is a
large, ingenious **scaffold that works around the model**: the deterministic `_file_op_flow`, the
surgical SEARCH/REPLACE matcher with three tolerance tiers, `verify_and_repair`, the `_EXT_GUARD`
per-extension nags, the JSON-repair-free tool loop plus the textual-tool-call fallback, and now the
task decomposer. Each layer exists because the model misbehaves without it.

The live Daraz runs show the ceiling directly:
- **Run 1**: referenced `script.js` but never created it; the login JS was missing; `index.html`
  got contaminated with the login form; a junk `e.g` file appeared.
- **Run 2** (after fixes): correct — but with a duplicate `<script src="script.js">`, a stray
  "Logout" link on the login page, prose leaking after `</html>`, and the password read as
  `password` instead of `admin`.

No amount of prompt engineering removes this ceiling. The honest framing: **Coder is a very good
harness wrapped around a model that isn't quite good enough for the harness's ambitions.** The
`/model` escape hatch (14B/32B) helps, but then "runs on my machine offline" quietly becomes "runs
if you have a big GPU."

---

## 2. "Verified OK" is a lie of omission — verification is syntax-only 🔴

`verify.py` checks that a file *parses* (`compile()` for `.py`, `node --check`, `tsc`, HTML
tag-balance). It does **not** check that the code does what was asked, that cross-file references
resolve, or that links point at files that exist.

Concrete: in Daraz run 1, `login.html` contained `<script src="script.js">` while **no `script.js`
existed**, and the file was still reported **"verified OK"** because the HTML tags balanced. So the
status the user sees (`— verified OK`) means "it parses," not "it works" — which is misleading in
exactly the situation where they'd most want to trust it.

What's missing: dead-reference checks (does every `href`/`src`/`import` resolve?), running the
project's tests/linter, and any behavioral smoke test. The `verify` step is the natural home for
this and today does the least useful 20%.

---

## 3. Nothing checks that the *whole* request was satisfied 🔴

Decomposition (M1) guarantees each sub-task *runs*, but nothing confirms the **overall goal** is
met. `_run_subtasks` executes N steps and concatenates their summaries; if step 3 produced garbage
or a step was silently dropped from the plan, no one notices. The M4 "coverage recheck" was
explicitly deferred. So Coder can confidently report *"Completed 8 tasks"* for a website whose login
button does nothing — and it did essentially that in run 1.

There is no closing loop: **plan → execute → verify the feature end-to-end → repair**. It stops at
"execute."

---

## 4. The plan you preview is not the plan that runs (nondeterminism) 🟡

Three different things all call themselves "the plan," and they disagree:
- The REPL's **"Plan" panel** uses `split_tasks()` → the *regex* splitter, which returns a **single
  item** for natural-language prose like Daraz — so the user sees no real plan, then watches 8 steps
  happen anyway.
- **`/plan`** calls `get_plan()` → `Planner.plan()` with `_PLAN_PROMPT`.
- **Execution** calls `Planner.decompose()` with a *different* prompt (`_DECOMPOSE_PROMPT`).

On top of that, `decompose` runs at **temperature 0.3**, so the *same* prompt yields *different*
file sets on different runs (my two Daraz runs produced a 4-step and an 8-step plan). There's no
plan caching within a turn and no "approve this exact plan, then run it." For a tool that edits your
files, preview-≠-execution is a real trust problem.

---

## 5. Routing is a lattice of heuristics that's hard to reason about 🟡

Control flow is decided by a stack of regexes plus one `classify()` call: `_wants_file_op`,
`wants_multifile`, `_split_compound`, `_looks_multipart`, `_extract_filename`, `_infer_filename`,
`_EXT_GUARD`. Each has edge cases and they interact:
- `_extract_filename` turned "e.g." into a file (now patched — but that class of bug recurs).
- `_infer_filename` *guesses* a filename from keywords when none is given.
- `_split_compound` deliberately under-splits (safe) but that means it misses most real prompts,
  pushing the work onto the nondeterministic LLM planner.

Claude Code gets away with **one** clean tool-calling loop because its model is reliable enough to
drive it. Coder can't, so it has this branch pile instead. It works, but it's brittle, and every new
capability adds another regex and another interaction to worry about. It's a local optimum, not a
foundation.

---

## 6. Context threading doesn't scale and isn't selective 🟡

The new cross-file consistency (the good part) works by reading **every** already-written file
(capped at 2500 chars each) into **every** later step's prompt. For a 3–4 file build that's fine.
For a 10-file build it means: later steps carry a growing wall of context, the token budget balloons,
generation slows, and a 7B model loses coherence on large prompts. There's no notion of *which*
siblings are relevant to the current file (the RAG/symbol index that exists elsewhere isn't used
here) — it's all-or-nothing.

---

## 7. The eval harness doesn't measure what actually matters 🟡

`evals/checks.py` asserts `file_exists`, `file_contains(substring)`, `min_files_written`,
`answer_contains`. None of that catches the failures that actually hurt: a script referenced but not
created, links that don't resolve, JS that doesn't run, a homepage that's secretly a login form.
Daraz run 1 would **pass** a "files exist + contain substrings" eval while being broken. So the
measuring stick can't see the project's most important regressions (cross-file coherence, behavioral
correctness). Evals test the easy 20%.

---

## 8. Cost/latency of the multi-task path is high on the target hardware 🟡

A single multi-part prompt now fans out to many sequential local-model calls: `classify` +
`decompose` + per sub-task (`classify` or regex) + full-file generation + `verify` + any `repair`.
On the same machine that's running Ollama, that's slow, and it's all serial. There's no batching,
no parallelism across independent files, and no caching of the plan. "Offline and private" comes at
"and noticeably slow for anything non-trivial."

---

## 9. Output parsing is still fragile against mixed prose/code 🟢

`_parse_file_output` / `_strip_code_fences` handle fences and stray closers, but the Daraz run leaked
prose *after* `</html>` into `index.html` (an unmatched closing fence with trailing commentary that
the stripper doesn't cut). Any time the model wraps a file in explanation, there's a chance the
explanation lands in the file. Verification won't catch it (see #2) because it still parses.

---

## 10. Observability makes partial failure invisible 🟢

Best-effort `except … log.debug/warning` is everywhere (by design), and the multi-task summary
reports each step as `Created X — verified OK`. When a build is *semantically* wrong, the user gets a
green-looking report with no signal that anything's off. There's no per-run trace of "here's the
plan, here's what each step actually changed, here's what failed" beyond the tool trace the REPL
prints. Debugging a bad multi-file build is hard.

---

## If I could fix only three things

1. **Make verification mean something (#2, #3).** Add dead-reference/link checking and an optional
   "run the project's tests/lint" step, then feed failures back into the existing repair loop. This
   turns "verified OK" from a syntax claim into a correctness claim and closes the plan→verify→repair
   loop.
2. **Make the plan real and stable (#4).** Decompose **once** at temperature 0, cache the plan for
   the turn, show *that* plan (same one `/plan` and the REPL panel display), and run exactly it.
   Preview should equal execution.
3. **Teach the evals to see coherence (#7).** Add checks for "referenced file exists," "link
   resolves," and a couple of behavioral assertions on a generated multi-file app, so the harness can
   actually catch the failures that matter.

None of these fight the model ceiling (#1) — they make the harness *honest about* it, which is the
best you can do while staying offline.

---

*Grounded in the code as of this writing plus two live runs of the Daraz prompt on
`qwen2.5-coder:7b`. File/line references may drift.*
