"""Live eval runner — the measuring stick.

Runs the golden suite through a real AgentCore against the local Ollama and
prints a scored report. NOT part of pytest (it needs Ollama running).

    python -m evals.run                 # run all golden tasks in a temp dir
    python -m evals.run --keep OUT_DIR  # keep the generated files for inspection
    python -m evals.run --min 0.7       # exit non-zero if score < 0.7
    python -m evals.run --blueprint     # run the Requirements Blueprint suite
                                        #   (forces settings.expand_requirements ON;
                                        #    the default suite forces it OFF)

Use it before/after a model or prompt change to catch regressions.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

from evals.harness import run_suite
from evals.tasks import BLUEPRINT_TASKS, GOLDEN_TASKS, WEBAPP_TASKS


def _ollama_reachable() -> bool:
    """Cheap preflight: is the local Ollama actually up? A down server makes every
    model call fail, which the agent swallows into empty results — so the suite
    would score 0/N and *look* like a code regression. Fail loud instead."""
    import urllib.request

    from config.settings import settings

    base = getattr(settings, "ollama_base_url", None) or "http://localhost:11434"
    try:
        with urllib.request.urlopen(f"{base.rstrip('/')}/api/tags", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


async def _main(
    base_dir: Path,
    min_score: float,
    only: str | None,
    blueprint: bool,
    webapp: bool = False,
) -> int:
    from app.agent.core import AgentCore
    from config.settings import settings

    if not _ollama_reachable():
        print(
            "ERROR: Ollama is not reachable at "
            f"{getattr(settings, 'ollama_base_url', 'http://localhost:11434')}.\n"
            "Start it first (`ollama serve`) and ensure the model is pulled "
            f"(`ollama pull {settings.llm_model}`). Without it every task writes "
            "nothing and the suite scores 0 — which is a connection problem, not "
            "a code one.",
            file=sys.stderr,
        )
        return 2

    # Pin the blueprint stage per suite rather than inheriting the process
    # default. It now ships ON (docs/fullstack-web-plan.md Phase 0), and
    # should_blueprint() matches several GOLDEN_TASKS prompts verbatim
    # ("Create an index.html file for a simple landing page") — so without this
    # the default suite would stop measuring plain routing and its 14/14
    # baseline would no longer be comparable to anything. --blueprint and
    # --webapp measure the full-stack path; the default suite measures routing.
    settings.expand_requirements = blueprint or webapp
    if webapp:
        # The webapp suite runs the app it built, so the smoke test would be
        # fighting it for the port. The checks do the running here.
        settings.blueprint_smoke_test = False
        tasks = WEBAPP_TASKS
    elif blueprint:
        tasks = BLUEPRINT_TASKS
    else:
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
    ap.add_argument(
        "--blueprint",
        action="store_true",
        help="Run the Requirements Blueprint suite with expand_requirements ON.",
    )
    ap.add_argument(
        "--webapp",
        action="store_true",
        help=(
            "Run the multi-turn webapp suite — the demo, turn for turn. Slow: "
            "each task is a whole conversation and the checks start the app."
        ),
    )
    args = ap.parse_args()

    if args.keep:
        base = Path(args.keep)
        base.mkdir(parents=True, exist_ok=True)
        return asyncio.run(
            _main(base, args.min, args.only, args.blueprint, args.webapp)
        )

    with tempfile.TemporaryDirectory(prefix="coder_evals_") as tmp:
        return asyncio.run(
            _main(Path(tmp), args.min, args.only, args.blueprint, args.webapp)
        )


if __name__ == "__main__":
    raise SystemExit(main())
