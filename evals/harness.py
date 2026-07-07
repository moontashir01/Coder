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
    id: str
    prompt: str
    checks: list[Check]


@dataclass
class CheckContext:
    answer: str
    trace: list[dict]
    workdir: Path


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

    Any exception from the agent is caught and recorded as a failure so one bad
    task never aborts the suite.
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    prev_cwd = os.getcwd()
    answer, trace = "", []
    error: str | None = None
    try:
        os.chdir(workdir)
        answer, trace = await agent.chat(task.prompt)
    except Exception as e:  # noqa: BLE001 — evals must be robust to any failure
        error = f"agent raised {type(e).__name__}: {e}"
    finally:
        os.chdir(prev_cwd)

    if error is not None:
        return TaskResult(task_id=task.id, passed=False, details=[error])

    ctx = CheckContext(answer=answer, trace=trace, workdir=workdir)
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
