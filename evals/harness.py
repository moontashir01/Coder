"""Eval task model + runner.

``run_task`` executes one prompt through ``AgentCore.chat`` inside an isolated
working directory and evaluates its checks; ``run_suite`` runs many and scores
the result. All offline-friendly: pass a scripted-LLM agent in tests, a real
one in ``evals/run.py``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from evals.checks import Check


@dataclass(frozen=True)
class EvalTask:
    """One eval: a prompt (or an ordered conversation) plus its checks.

    ``prompts`` turns a task into a MULTI-TURN one — the shape the demo actually
    has, where turn 3 must not break what turn 1 built. ``prompt`` still works
    exactly as before, so every single-turn task is untouched.
    """

    id: str
    checks: list[Check]
    prompt: str = ""
    prompts: list[str] | None = None

    def turns(self) -> list[str]:
        return list(self.prompts) if self.prompts else [self.prompt]


@dataclass
class CheckContext:
    answer: str
    trace: list[dict]
    workdir: Path
    # Every turn's answer, in order — so a check can look at what turn 2 said
    # rather than only the last thing printed.
    answers: list[str] = field(default_factory=list)
    # Memo for the browser-driven checks (Phase W10). Every one of them needs
    # the app running and a browser open; done per check that is five server
    # launches and five Chromium starts for one task. Filled in by
    # `evals.checks._browser_report`, which is the only thing that reads it.
    browser: object | None = None
    _adapter: object | None = field(default=None, repr=False)

    @property
    def adapter(self):
        """The stack adapter for the project that was actually BUILT (Phase N6).

        Read off the project's own `.coder/project.json`, not off
        `settings.web_stack` — the same precedence `stacks.resolve_key` gives
        every other caller, and for the same reason. A check that trusted the
        session setting would look for `app.py` in a Node project the moment
        someone ran the suite with the default still on Flask, and report "this
        build is static" about an Express app that works.

        Resolved once per task and cached: every check that starts the app asks
        for it, and re-reading the spec per check would be the `_browser_report`
        memo problem again. Total, like `get_adapter` — no spec means Flask,
        which is exactly what every existing single-stack task expects.
        """
        if self._adapter is None:
            from app.agent.projectspec import ProjectSpec
            from app.agent.stacks import get_adapter, resolve_key

            try:
                spec = ProjectSpec.load(self.workdir)
            except Exception:  # noqa: BLE001 — a check must never die on this
                spec = None
            self._adapter = get_adapter(resolve_key(spec, ""))
        return self._adapter


@dataclass
class TaskResult:
    task_id: str
    passed: bool
    details: list[str] = field(default_factory=list)


@dataclass
class SuiteReport:
    results: list[TaskResult]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def score(self) -> float:
        return self.passed / self.total if self.results else 0.0

    def format(self) -> str:
        lines = [
            f"Eval: {self.passed}/{self.total} passed (score {self.score:.0%})",
            "",
        ]
        for r in self.results:
            mark = "PASS" if r.passed else "FAIL"
            lines.append(f"[{mark}] {r.task_id}")
            for d in r.details:
                lines.append(f"       - {d}")
        return "\n".join(lines)


async def run_task(agent, task: EvalTask, workdir: Path, prepare=None) -> TaskResult:
    """Run one task in ``workdir`` (cwd is switched for the call, then restored).

    A multi-turn task runs every prompt against **one** workdir with **one**
    agent, and the checks run only after the last turn. The shared agent is not
    an optimisation: a fresh one per turn would reload the spec from disk and
    mask exactly the in-memory staleness bugs this suite exists to catch.

    Any exception from the agent is caught and recorded as a failure so one bad
    task never aborts the suite — and a turn that raises stops the conversation,
    since every later turn would be measuring the wrong thing.

    ``prepare(workdir) -> str`` runs after the build and before the checks, for
    the one thing a generated project may need that Coder deliberately will not
    do for you: `npm install` (Phase N6). It is a hook rather than a step
    because it needs the network, so it must stay something the caller opts into
    — the same shape as Playwright's Chromium. Its note is recorded in the
    details either way, so a run that installed nothing says so.
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    prev_cwd = os.getcwd()
    answers: list[str] = []
    answer, trace = "", []
    error: str | None = None
    try:
        os.chdir(workdir)
        for index, prompt in enumerate(task.turns(), 1):
            try:
                answer, trace = await agent.chat(prompt)
            except Exception as e:  # noqa: BLE001 — evals survive any failure
                error = f"turn {index} raised {type(e).__name__}: {e}"
                break
            answers.append(answer or "")
    finally:
        os.chdir(prev_cwd)

    if error is not None:
        return TaskResult(task_id=task.id, passed=False, details=[error])

    ctx = CheckContext(answer=answer, trace=trace, workdir=workdir, answers=answers)
    details: list[str] = []
    if prepare is not None:
        try:
            note = prepare(workdir)
        except Exception as e:  # noqa: BLE001 — preparation never aborts a task
            note = f"{type(e).__name__}: {e}"
        if note:
            details.append(f"prepare: {note}")
    passed = True
    for check in task.checks:
        ok, detail = check(ctx)
        details.append(("ok: " if ok else "FAIL: ") + detail)
        passed = passed and ok
    return TaskResult(task_id=task.id, passed=passed, details=details)


async def run_suite(
    agent, tasks: list[EvalTask], base_dir: Path, prepare=None
) -> SuiteReport:
    """Run every task in its own subdir of ``base_dir`` and score the suite."""
    base_dir = Path(base_dir)
    results: list[TaskResult] = []
    for task in tasks:
        results.append(
            await run_task(agent, task, workdir=base_dir / task.id, prepare=prepare)
        )
    return SuiteReport(results=results)
