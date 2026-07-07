"""The golden task suite — observable-outcome prompts for the live eval run.

Intentionally small and concrete (roadmap Tier 2 #6: 10–20 prompts). Each task
asserts something you can *see* on disk or in the answer, not model prose
quality. Add tasks here as new behaviors ship.
"""

from __future__ import annotations

from evals.checks import (answer_contains, file_contains, file_excludes,
                          file_exists, min_files_written)
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
