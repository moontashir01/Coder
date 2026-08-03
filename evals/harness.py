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


async def run_task(agent, task: EvalTask, workdir: Path) -> TaskResult:
    """Run one task in ``workdir`` (cwd is switched for the call, then restored).

    A multi-turn task runs every prompt against **one** workdir with **one**
    agent, and the checks run only after the last turn. The shared agent is not
    an optimisation: a fresh one per turn would reload the spec from disk and
    mask exactly the in-memory staleness bugs this suite exists to catch.

    Any exception from the agent is caught and recorded as a failure so one bad
    task never aborts the suite — and a turn that raises stops the conversation,
    since every later turn would be measuring the wrong thing.
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
    passed = True
    for check in task.checks:
        ok, detail = check(ctx)
        details.append(("ok: " if ok else "FAIL: ") + detail)
        passed = passed and ok
    return TaskResult(task_id=task.id, passed=passed, details=details)


async def run_suite(agent, tasks: list[EvalTask], base_dir: Path) -> SuiteReport:
    """Run every task in its own subdir of ``base_dir`` and score the suite."""
    base_dir = Path(base_dir)
    results: list[TaskResult] = []
    for task in tasks:
        results.append(await run_task(agent, task, workdir=base_dir / task.id))
    return SuiteReport(results=results)
