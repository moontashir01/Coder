"""Live eval runner — the measuring stick.

Runs the golden suite through a real AgentCore against the local Ollama and
prints a scored report. NOT part of pytest (it needs Ollama running).

    python -m evals.run                 # run all golden tasks in a temp dir
    python -m evals.run --keep OUT_DIR  # keep the generated files for inspection
    python -m evals.run --min 0.7       # exit non-zero if score < 0.7

Use it before/after a model or prompt change to catch regressions.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

from evals.harness import run_suite
from evals.tasks import GOLDEN_TASKS


async def _main(base_dir: Path, min_score: float, only: str | None) -> int:
    from app.agent.core import AgentCore

    tasks = GOLDEN_TASKS
    if only:
        wanted = {s.strip() for s in only.split(",") if s.strip()}
        tasks = [t for t in tasks if t.id in wanted]
        if not tasks:
            print(f"No golden tasks match --only {only!r}", file=sys.stderr)
            return 2

    agent = AgentCore(session_id="evals")
    report = await run_suite(agent, tasks, base_dir=base_dir)
    print(report.format())
    print(f"\nArtifacts: {base_dir}")

    if report.score < min_score:
        print(
            f"\nFAIL: score {report.score:.0%} below threshold {min_score:.0%}",
            file=sys.stderr,
        )
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the offline Coder eval suite live.")
    ap.add_argument(
        "--keep",
        metavar="DIR",
        help="Directory to write task artifacts to (kept). Default: a temp dir.",
    )
    ap.add_argument(
        "--min",
        type=float,
        default=0.0,
        help="Minimum passing score in [0,1]; exit non-zero if below.",
    )
    ap.add_argument(
        "--only",
        help="Comma-separated task ids to run (default: all).",
    )
    args = ap.parse_args()

    if args.keep:
        base = Path(args.keep)
        base.mkdir(parents=True, exist_ok=True)
        return asyncio.run(_main(base, args.min, args.only))

    with tempfile.TemporaryDirectory(prefix="coder_evals_") as tmp:
        return asyncio.run(_main(Path(tmp), args.min, args.only))


if __name__ == "__main__":
    raise SystemExit(main())
