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
    contrast_ok,
    db_has_column,
    earlier_pages_still_work,
    entities_are_usable,
    every_control_does_something,
    every_entity_has_a_table,
    file_contains,
    file_excludes,
    file_exists,
    has_backend_server,
    is_full_stack_app,
    min_files_written,
    nav_on_every_page,
    no_console_errors,
    no_horizontal_overflow,
    spec_has_endpoint,
    spec_has_entity,
    style_stable_across_turns,
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
    # -----------------------------------------------------------------------
    # Phase E (docs/always-fullstack-plan.md): one task per REQUEST SHAPE, all
    # asserting the same three things. The point is that the checks name no
    # table and no route — they read the project's own spec — so a request whose
    # schema the eval author could not have guessed is measured just as
    # strictly as the e-commerce one above.
    #
    #   is_full_stack_app        A + B: a server exists at all
    #   every_entity_has_a_table C1: the declared schema really ran
    #   entities_are_usable      C3: every table is browsable and writable
    # -----------------------------------------------------------------------
    EvalTask(
        id="web_shape_blog",
        prompt="build me a blog where I can write posts and readers can comment",
        checks=[
            is_full_stack_app(),
            every_entity_has_a_table(),
            entities_are_usable(),
        ],
    ),
    EvalTask(
        id="web_shape_shop",
        prompt="build me a shop for selling handmade candles with pictures",
        checks=[
            is_full_stack_app(),
            every_entity_has_a_table(),
            entities_are_usable(),
        ],
    ),
    EvalTask(
        id="web_shape_booking",
        prompt="build a booking system for a barber shop",
        checks=[
            is_full_stack_app(),
            every_entity_has_a_table(),
            entities_are_usable(),
        ],
    ),
    # THE Phase B regression test. Not one word here is in `_BLUEPRINT_NOUN_RE`,
    # and there is no build verb either: before Phase B this shipped static HTML
    # with no server and no database, and every file-level check still passed.
    # If tier 2 ever regresses, `is_full_stack_app` is what says so.
    EvalTask(
        id="web_shape_offlist",
        prompt="something to organize my recipes and what goes in them",
        checks=[
            is_full_stack_app(),
            every_entity_has_a_table(),
            entities_are_usable(),
        ],
    ),
    # And the same off-list project amended twice — the headline question asked
    # of a request the gate was never written for: did turn 3 break turn 1?
    EvalTask(
        id="web_shape_offlist_amended",
        prompts=[
            "something to organize my recipes and what goes in them",
            "add a prep time in minutes to each recipe",
            "add a page listing recipes I have marked as favourites",
        ],
        checks=[
            is_full_stack_app(),
            every_entity_has_a_table(),  # incl. the column turn 2 added
            earlier_pages_still_work(["/"]),
            entities_are_usable(),
        ],
    ),
    # -----------------------------------------------------------------------
    # Phase W10 (docs/web-quality-plan.md): does it LOOK right, and does it
    # still look right three turns later. Kept as their own tasks rather than
    # bolted onto the ones above, for two reasons: they need a headless browser
    # that is a deliberate opt-in (`python -m playwright install chromium`), and
    # a machine without one must not quietly drag the whole suite's score down —
    # these tasks FAIL with the install command in the detail, which is the only
    # honest report of a check that could not run.
    #
    # The checks name no selector and no route: they read the project's own spec
    # and drive the same `browser.py` the agent uses.
    # -----------------------------------------------------------------------
    EvalTask(
        id="web_quality_build",
        prompt="build me an e-commerce site for selling books",
        checks=[
            no_horizontal_overflow(),
            no_console_errors(),
            every_control_does_something(),
            nav_on_every_page(),
            contrast_ok(),
        ],
    ),
    EvalTask(
        id="web_quality_stable",
        prompts=[
            "build me an e-commerce site for selling books",
            "add an admin page where I can add a product with a picture",
            "add a shopping cart",
        ],
        checks=[
            # THE headline number for the web-quality plan: turn 3 must not
            # restyle turn 1. The products table and the cart table have to be
            # visibly the same table.
            style_stable_across_turns(),
            nav_on_every_page(),
            no_horizontal_overflow(),
            no_console_errors(),
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
