"""Live eval runner — the measuring stick.

Runs the golden suite through a real AgentCore against the local Ollama and
prints a scored report. NOT part of pytest (it needs Ollama running).

    python -m evals.run                 # run all golden tasks in a temp dir
    python -m evals.run --keep OUT_DIR  # keep the generated files for inspection
    python -m evals.run --min 0.7       # exit non-zero if score < 0.7
    python -m evals.run --blueprint     # run the Requirements Blueprint suite
                                        #   (forces settings.expand_requirements ON;
                                        #    the default suite forces it OFF)
    python -m evals.run --webapp --stack node        # the same suite on Node
    python -m evals.run --webapp --stack node --npm-install   # ...and install deps

Use it before/after a model or prompt change to catch regressions.

**On `--stack node` (Phase N6).** The suite's checks name no table and no route
— they read the project's own spec — so they measure a Node build as strictly as
a Flask one. What does NOT come free is the environment: a generated Node project
cannot run until `npm install` has fetched Express and `pg`, and that needs the
network, which Coder will not use on your behalf. So the app-running checks FAIL
naming the command rather than being skipped, exactly as a missing browser does
in W10 — a suite that scored well without starting anything would be worthless.
Pass `--npm-install` to let the RUNNER (not Coder) install into each task dir.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

from evals.harness import run_suite
from evals.tasks import BLUEPRINT_TASKS, GOLDEN_TASKS, WEBAPP_TASKS

_IS_WIN = sys.platform == "win32"


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


def _npm_installer(timeout: float = 300.0):
    """`npm install` in each task dir — the one step Coder will not do for you.

    A deliberate, opt-in exception to the offline rule, handled exactly like
    Playwright's Chromium in W4: the network is used ONCE, by the runner, only
    when asked, and never silently. Coder itself still never runs it.

    Never raises: the note comes back as text and the checks then fail on their
    own terms, naming what is missing.
    """
    import shutil
    import subprocess

    def prepare(workdir: Path) -> str:
        if not (Path(workdir) / "package.json").is_file():
            return "no package.json — nothing to install"
        if not shutil.which("npm"):
            return "npm is not on PATH"
        try:
            proc = subprocess.run(
                ["npm", "install", "--no-audit", "--no-fund"],
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=_IS_WIN,  # npm is npm.cmd on Windows
            )
        except Exception as e:  # noqa: BLE001
            return f"npm install did not run: {type(e).__name__}: {e}"
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()
            return "npm install failed: " + (tail[-1][:160] if tail else "no output")
        return "npm install completed"

    return prepare


def _node_preflight() -> str:
    """Why a Node suite cannot be measured here at all, or "".

    Only the machine-wide half — Node itself. `node_modules` and PostgreSQL are
    per-project and are reported per task by the readiness gate, because a run
    that legitimately installs (via `--npm-install`) must not be refused up
    front.
    """
    import shutil

    if not shutil.which("node"):
        return (
            "Node.js is not on PATH, so every task in this suite would fail for "
            "the same environment reason and the score would mean nothing. "
            "Install it (nodejs.org) and re-run."
        )
    return ""


async def _main(
    base_dir: Path,
    min_score: float,
    only: str | None,
    blueprint: bool,
    webapp: bool = False,
    stack: str = "flask",
    npm_install: bool = False,
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

    # Phase N6: which stack the suite BUILDS on. Set before any task runs, so
    # every turn's `_select_stack` sees it; each project then persists its own
    # stack into `.coder/project.json`, which is what the checks read back.
    settings.web_stack = stack
    if stack != "flask":
        blocked = _node_preflight() if stack == "node" else ""
        if blocked:
            print(f"ERROR: {blocked}", file=sys.stderr)
            return 2
        print(f"Stack: {stack}")
        if not npm_install:
            print(
                "NOTE: --npm-install was not passed, so the generated projects "
                "have no node_modules. Checks that start the app will FAIL "
                "naming `npm install` rather than being skipped — a score here "
                "measures the files, not the running app.",
                file=sys.stderr,
            )

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
    report = await run_suite(
        agent,
        tasks,
        base_dir=base_dir,
        prepare=_npm_installer() if npm_install else None,
    )
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
    ap.add_argument(
        "--stack",
        default="flask",
        choices=["flask", "node"],
        help=(
            "Which stack to BUILD on (default: flask). The checks read the "
            "project's own spec, so they generalise; the environment does not — "
            "see --npm-install."
        ),
    )
    ap.add_argument(
        "--npm-install",
        action="store_true",
        help=(
            "Run `npm install` in each task directory before its checks. Needs "
            "the network, so it is opt-in and the RUNNER does it, never Coder. "
            "Without it, a --stack node run measures the files but not the "
            "running app, and says so."
        ),
    )
    args = ap.parse_args()

    def _go(base: Path) -> int:
        return asyncio.run(
            _main(
                base,
                args.min,
                args.only,
                args.blueprint,
                args.webapp,
                args.stack,
                args.npm_install,
            )
        )

    if args.keep:
        base = Path(args.keep)
        base.mkdir(parents=True, exist_ok=True)
        return _go(base)

    with tempfile.TemporaryDirectory(prefix="coder_evals_") as tmp:
        return _go(Path(tmp))


if __name__ == "__main__":
    raise SystemExit(main())
