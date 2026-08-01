"""The golden task suite — observable-outcome prompts for the live eval run.

Intentionally small and concrete (roadmap Tier 2 #6: 10–20 prompts). Each task
asserts something you can *see* on disk or in the answer, not model prose
quality. Add tasks here as new behaviors ship.
"""

from __future__ import annotations

from evals.checks import (
    answer_contains,
    any_file_matches,
    app_serves,
    backend_reads_fields,
    db_has_column,
    earlier_pages_still_work,
    file_contains,
    file_excludes,
    file_exists,
    has_backend_server,
    min_files_written,
    spec_has_endpoint,
    spec_has_entity,
)
from evals.harness import EvalTask

GOLDEN_TASKS: list[EvalTask] = [
    # --- single-file creation --------------------------------------------
    EvalTask(
        id="create_html_page",
        prompt="Create an index.html file for a simple landing page with a heading.",
        checks=[file_exists("index.html"), file_contains("index.html", "<html")],
    ),
    EvalTask(
        id="create_python_add",
        prompt="Create a file calc.py with a function add(a, b) that returns a + b.",
        checks=[file_exists("calc.py"), file_contains("calc.py", "def add")],
    ),
    EvalTask(
        id="create_css_file",
        prompt="Create a styles.css file that sets the body background to navy.",
        checks=[file_exists("styles.css"), file_contains("styles.css", "background")],
    ),
    EvalTask(
        id="create_readme",
        prompt="Create a README.md describing a project called Coder.",
        checks=[file_exists("README.md"), file_contains("README.md", "Coder")],
    ),
    EvalTask(
        id="create_json_config",
        prompt="Create a config.json file with a key named version set to 1.",
        checks=[file_exists("config.json"), file_contains("config.json", "version")],
    ),
    # --- single-file edit -------------------------------------------------
    EvalTask(
        id="edit_add_function",
        prompt="Create greet.py with def hello(): return 'hi', then we will check it.",
        checks=[file_exists("greet.py"), file_contains("greet.py", "def hello")],
    ),
    # --- syntactic validity (verify-and-repair should guarantee this) -----
    EvalTask(
        id="python_is_valid",
        prompt="Create a Python file fib.py with a recursive fibonacci function.",
        checks=[file_exists("fib.py"), file_contains("fib.py", "def")],
    ),
    # --- multi-file split -------------------------------------------------
    EvalTask(
        id="multifile_three",
        prompt=(
            "Create three files: index.html, styles.css and script.js for a small "
            "webpage. The HTML must link the css and js as external files."
        ),
        checks=[
            file_exists("styles.css"),
            file_exists("script.js"),
            file_contains("index.html", "styles.css"),
            min_files_written(3),
        ],
    ),
    EvalTask(
        id="multifile_html_links_css",
        prompt=(
            "Create a webpage as separate files with an external stylesheet; "
            "index.html should reference the css via a <link> tag."
        ),
        checks=[
            file_contains("index.html", "<link"),
            file_excludes("index.html", "<style>"),
        ],
    ),
    # --- multi-task compliance (M1: several instructions in one prompt) ---
    EvalTask(
        id="multitask_two_files",
        prompt=(
            "Create a file alpha.py with a function a() that returns 1, and "
            "create a file beta.py with a function b() that returns 2."
        ),
        checks=[
            file_exists("alpha.py"),
            file_contains("alpha.py", "def a"),
            file_exists("beta.py"),
            file_contains("beta.py", "def b"),
            min_files_written(2),
        ],
    ),
    EvalTask(
        id="multitask_sequence",
        prompt=(
            "First create notes.md with a top-level heading, then create "
            "todo.md with a markdown bullet list of two items."
        ),
        checks=[
            file_exists("notes.md"),
            file_exists("todo.md"),
            min_files_written(2),
        ],
    ),
    # --- plain Q&A (no file, answer content) ------------------------------
    EvalTask(
        id="qa_decorator",
        prompt="In one sentence, what is a Python decorator?",
        checks=[answer_contains("function")],
    ),
    EvalTask(
        id="qa_list_vs_tuple",
        prompt="What is the key difference between a Python list and a tuple?",
        checks=[answer_contains("mutable")],
    ),
    EvalTask(
        id="qa_git",
        prompt="What git command shows the working-tree status?",
        checks=[answer_contains("status")],
    ),
]


# ---------------------------------------------------------------------------
# Blueprint suite — measures the Requirements Blueprint feature
# (docs/requirements-blueprint.md). These need `settings.expand_requirements`
# ON, so they run via `python -m evals.run --blueprint`, NOT the default suite.
#
# The checks assert the failures a file-exists/substring eval can't see
# (weaknesses.md #7): that the build is actually full-stack and the button
# reaches a server — not just a layout. Route paths and filenames vary run to
# run, so the checks probe for CONCEPTS (a form, a real backend, the server
# reading the form's fields), never an exact route string the model must guess.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Webapp suite — the DEMO, turn for turn (docs/fullstack-web-plan.md Phase 7).
#
# Multi-turn: every prompt runs against ONE workdir with ONE agent, and the
# checks run after the last turn. That shape is the point — a suite that builds
# each task from scratch cannot measure the only thing the faculty actually
# complained about, which is whether turn 3 breaks what turn 1 built.
#
# Run with `python -m evals.run --webapp`. Slow (four live builds per task) and
# it starts the generated app repeatedly, so it is deliberately its own suite.
# ---------------------------------------------------------------------------

WEBAPP_TASKS: list[EvalTask] = [
    EvalTask(
        id="web_turn1_build",
        prompt="build me an e-commerce site for selling books",
        checks=[
            file_exists("app.py"),
            file_exists("templates/base.html"),
            spec_has_entity("product"),
            app_serves(["/"], label="the site"),
        ],
    ),
    EvalTask(
        id="web_turn2_amend",
        prompts=[
            "build me an e-commerce site for selling books",
            "add an admin page where I can add a product with a picture",
        ],
        checks=[
            # The amendment landed…
            spec_has_endpoint("POST", "/"),
            db_has_column("products", "image_path"),
            # …and turn 1 still works. THE headline check.
            earlier_pages_still_work(["/"]),
        ],
    ),
    EvalTask(
        id="web_turn3_cart",
        prompts=[
            "build me an e-commerce site for selling books",
            "add an admin page where I can add a product with a picture",
            "add a shopping cart",
        ],
        checks=[
            spec_has_entity("cart"),
            earlier_pages_still_work(["/"]),
        ],
    ),
    EvalTask(
        id="web_turn4_search",
        prompts=[
            "build me an e-commerce site for selling books",
            "add an admin page where I can add a product with a picture",
            "add a shopping cart",
            "now let customers search products by title",
        ],
        checks=[
            spec_has_endpoint("GET", "search"),
            earlier_pages_still_work(["/"]),
        ],
    ),
]


_SUBMIT_MARKERS = (
    "fetch(",
    "XMLHttpRequest",
    "axios",
    "action=",
    "onsubmit",
    "addEventListener('submit'",
    'addEventListener("submit"',
)

BLUEPRINT_TASKS: list[EvalTask] = [
    EvalTask(
        id="bp_login_fullstack",
        prompt="Build me a login page with a working backend.",
        checks=[
            any_file_matches(["<form"], exts=(".html", ".htm"), label="login form"),
            any_file_matches(
                ["password"], exts=(".html", ".htm", ".js"), label="password field"
            ),
            has_backend_server(),
            backend_reads_fields(["password"]),
            min_files_written(2),
        ],
    ),
    EvalTask(
        id="bp_signup_reset",
        prompt=(
            "Create a signup page with email and password and a "
            "forgot-password flow."
        ),
        checks=[
            any_file_matches(["<form"], exts=(".html", ".htm"), label="signup form"),
            has_backend_server(),
            min_files_written(2),
        ],
    ),
    EvalTask(
        id="bp_todo_app",
        prompt="Build a todo app where I can add a todo and see my list of todos.",
        checks=[
            has_backend_server(),
            any_file_matches(
                _SUBMIT_MARKERS, exts=(".html", ".js"), label="frontend calls server"
            ),
            min_files_written(2),
        ],
    ),
    EvalTask(
        id="bp_contact_form",
        prompt="Make a contact form that submits messages to a backend.",
        checks=[
            any_file_matches(["<form"], exts=(".html", ".htm"), label="contact form"),
            has_backend_server(),
            any_file_matches(
                _SUBMIT_MARKERS, exts=(".html", ".js"), label="frontend submits"
            ),
            min_files_written(2),
        ],
    ),
]
