"""Deterministic project scaffold (app/agent/scaffold.py) — Phase 1.

Fully offline. The one test that runs a subprocess starts the SCAFFOLDED app
(no LLM involved), which is the point: the plan's "done when" for this phase is
that `python app.py` works before a single line has been generated.
"""

import socket
from types import SimpleNamespace

import pytest

from app.agent.blueprint import (
    TIER_CORE,
    TIER_REQUESTED,
    ApiContract,
    Blueprint,
    Endpoint,
    Feature,
    PlannedFile,
)
from app.agent.buildspec import build_spec_from_data
from app.agent.core import AgentCore
from app.agent.references import (
    extract_local_references,
    find_broken_page_links,
    is_template_expression,
)
from app.agent.runtime_probe import NO_STACK, STDLIB_STACK, Stack
from app.agent.scaffold import (
    convert_to_child_template,
    frozen_files,
    is_frozen,
    is_web_app,
    project_name,
    restore_index_route,
    scaffold_context,
    scaffold_files,
    scaffold_flask,
    templates_without_inheritance,
)
from app.agent.verify import (
    find_external_assets,
    fix_form_enctype,
    forms_missing_enctype,
    strip_external_assets,
)
from config.settings import settings


class ScriptedLLM:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    def invoke(self, messages):
        out = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        return SimpleNamespace(content=out)


FLASK_STACK = Stack(language="python", backend="flask", note="Flask is installed")


# ---------------------------------------------------------------------------
# is_web_app — when does a scaffold apply at all
# ---------------------------------------------------------------------------


def test_is_web_app_true_for_pages_and_endpoints():
    pages = Blueprint(files=(PlannedFile("templates/index.html"),), stack=FLASK_STACK)
    api = Blueprint(
        contract=ApiContract(endpoints=(Endpoint("POST", "/api/x"),)),
        stack=FLASK_STACK,
    )
    assert is_web_app(pages) is True
    assert is_web_app(api) is True


def test_is_web_app_false_without_a_backend_or_a_web_surface():
    """A script with a backend stack is not a web app, and neither is a static
    page with no backend — scaffolding either would be wrong."""
    script = Blueprint(files=(PlannedFile("tool.py"),), stack=FLASK_STACK)
    static = Blueprint(files=(PlannedFile("index.html"),), stack=NO_STACK)
    assert is_web_app(script) is False
    assert is_web_app(static) is False


# ---------------------------------------------------------------------------
# scaffold_flask — what lands on disk
# ---------------------------------------------------------------------------


def test_scaffold_writes_the_whole_canonical_layout(tmp_path):
    written = scaffold_flask(tmp_path, "Bookshop")

    expected = {
        "app.py",
        "db.py",
        "models.py",
        "seed.py",
        "requirements.txt",
        "Procfile",
        "README.md",
        ".gitignore",
        "static/css/style.css",
        "static/js/app.js",
        "static/uploads/.gitkeep",
        "templates/base.html",
        "templates/index.html",
    }
    assert expected <= set(written)
    for rel in expected:
        assert (tmp_path / rel).is_file(), f"{rel} was not created"


def test_scaffold_files_matches_what_is_actually_written(tmp_path):
    """`scaffold_files()` is read by the build plan to know what already exists.
    If it drifts from the real tree, generation is told the wrong thing."""
    written = scaffold_flask(tmp_path, "Demo")
    assert scaffold_files() == set(written)


def test_dotfiles_are_written_with_their_dot(tmp_path):
    """They are STORED without a dot (setuptools' resources/**/* glob does not
    reliably ship hidden files) and must be restored on the way out."""
    scaffold_flask(tmp_path, "Demo")
    assert (tmp_path / ".gitignore").is_file()
    assert (tmp_path / "static" / "uploads" / ".gitkeep").is_file()
    assert not (tmp_path / "gitignore").exists()


def test_placeholders_are_fully_substituted(tmp_path):
    scaffold_flask(tmp_path, "Bookshop")
    for path in tmp_path.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        assert "{{PROJECT_NAME}}" not in text, path
        assert "{{SECRET_KEY}}" not in text, path
    assert "Bookshop" in (tmp_path / "templates" / "base.html").read_text(
        encoding="utf-8"
    )


def test_substitution_leaves_jinja_expressions_intact(tmp_path):
    """The placeholders share Jinja's `{{ }}` delimiters, so substitution must be
    exact-literal — a template engine here would eat `{{ url_for(...) }}` and the
    generated site would lose its stylesheet."""
    scaffold_flask(tmp_path, "Bookshop")
    base = (tmp_path / "templates" / "base.html").read_text(encoding="utf-8")
    assert "{{ url_for('static', filename='css/style.css') }}" in base
    assert "{% block content %}" in base


def test_secret_key_is_random_per_project(tmp_path):
    a, b = tmp_path / "one", tmp_path / "two"
    a.mkdir()
    b.mkdir()
    scaffold_flask(a, "One")
    scaffold_flask(b, "Two")
    key_a = (a / "app.py").read_text(encoding="utf-8")
    key_b = (b / "app.py").read_text(encoding="utf-8")
    assert key_a != key_b


def test_rerunning_never_overwrites_an_edited_file(tmp_path):
    """An amendment turn calls this again. It must be a no-op — otherwise turn 2
    silently reverts the work of turn 1, which is the exact failure the whole
    plan exists to fix."""
    scaffold_flask(tmp_path, "Bookshop")
    app_py = tmp_path / "app.py"
    app_py.write_text("# my own routes\n", encoding="utf-8")

    second = scaffold_flask(tmp_path, "Bookshop")

    assert second == []  # nothing rewritten
    assert app_py.read_text(encoding="utf-8") == "# my own routes\n"


def test_scaffold_is_best_effort_when_the_template_tree_is_missing(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "scaffolds_dir", tmp_path / "nope")
    assert scaffold_flask(tmp_path / "proj", "X") == []
    assert scaffold_files() == set()


def test_project_name_is_derived_from_the_directory(tmp_path):
    d = tmp_path / "my-book_shop"
    d.mkdir()
    assert project_name(d) == "My Book Shop"


# ---------------------------------------------------------------------------
# frozen files — what generation must not rewrite
# ---------------------------------------------------------------------------


def test_frozen_covers_only_pure_boilerplate():
    """app.py / templates / models.py are NOT frozen: they carry the domain
    layer and are edited on top of the skeleton. Freezing them would ship the
    placeholder home page as the finished site."""
    frozen = frozen_files()
    assert "requirements.txt" in frozen
    assert "Procfile" in frozen
    assert ".gitignore" in frozen
    assert "app.py" not in frozen
    assert "templates/index.html" not in frozen
    assert "models.py" not in frozen


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Procfile", True),
        ("./Procfile", True),
        (".\\requirements.txt", True),
        ("static/uploads/.gitkeep", True),
        ("app.py", False),
        ("templates/index.html", False),
    ],
)
def test_is_frozen_normalizes_separators(name, expected):
    assert is_frozen(name) is expected


def test_scaffold_context_names_the_layout_or_is_empty():
    assert scaffold_context([]) == ""
    block = scaffold_context(["app.py", "db.py"])
    assert "db.py" in block and "models.py" in block
    assert 'extends "base.html"' in block


# ---------------------------------------------------------------------------
# The scaffolded app actually RUNS — the phase's "done when"
# ---------------------------------------------------------------------------


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def test_scaffolded_app_starts_and_serves_the_home_page(tmp_path):
    pytest.importorskip("flask")
    from app.agent.smoke import run_smoke_test

    # The scaffold binds :5000. If something else already holds it, the probe
    # would talk to THAT server and report a pass this test never earned —
    # worse than failing. Skip loudly instead.
    if not _port_is_free(5000):
        pytest.skip("port 5000 is already in use; cannot verify the scaffold runs")

    project = tmp_path / "bookshop"
    project.mkdir()
    scaffold_flask(project, "Bookshop")

    result = run_smoke_test(project / "app.py", project, timeout=20.0)

    assert result.started, f"scaffolded app crashed: {result.stderr[:500]}"
    assert result.responded, result.detail
    # 200, not the 404 a hand-written server gives when it defines no "/" route.
    assert result.status == 200, result.detail


# ---------------------------------------------------------------------------
# _run_blueprint integration
# ---------------------------------------------------------------------------


def _web_blueprint(stack=FLASK_STACK, extra_files=()):
    files = (
        PlannedFile("app.py", "create", "the routes"),
        PlannedFile("templates/index.html", "create", "the storefront"),
        PlannedFile("requirements.txt", "create", "list flask"),
    ) + tuple(extra_files)
    return Blueprint(
        summary="A bookshop",
        features=(
            Feature("Storefront", TIER_REQUESTED, ("templates/index.html",)),
            Feature("Backend", TIER_CORE, ("app.py",)),
        ),
        files=files,
        contract=ApiContract(endpoints=(Endpoint("GET", "/books"),)),
        stack=stack,
    )


async def test_run_blueprint_scaffolds_before_generating(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_scaffold_run")

    captured = {}

    async def _fake_mff(user_message, refs, extra_context="", preplanned_ops=None):
        # The scaffold must already be on disk by the time generation starts.
        captured["app_py_exists"] = (tmp_path / "app.py").is_file()
        captured["ops"] = [op.filename for op in preplanned_ops]
        captured["extra"] = extra_context
        return "Handled files", []

    monkeypatch.setattr(a, "_multi_file_flow", _fake_mff)

    answer, _ = await a._run_blueprint("build a bookshop", _web_blueprint(), [])

    assert captured["app_py_exists"] is True
    assert (tmp_path / "templates" / "base.html").is_file()
    # requirements.txt is frozen → dropped from the plan; the domain files stay.
    assert "requirements.txt" not in captured["ops"]
    assert "app.py" in captured["ops"]
    assert "templates/index.html" in captured["ops"]
    assert "do not rewrite it" in captured["extra"]
    assert "Scaffolded a runnable Flask project" in answer


async def test_run_blueprint_does_not_scaffold_a_non_flask_stack(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_scaffold_stdlib")

    async def _fake_mff(user_message, refs, extra_context="", preplanned_ops=None):
        return "Handled files", []

    monkeypatch.setattr(a, "_multi_file_flow", _fake_mff)

    answer, _ = await a._run_blueprint(
        "build a bookshop", _web_blueprint(stack=STDLIB_STACK), []
    )

    assert not (tmp_path / "templates" / "base.html").exists()
    assert "Scaffolded" not in answer


async def test_run_blueprint_reports_files_dropped_by_the_budget(tmp_path, monkeypatch):
    """The cap used to truncate silently, and `_verify_blueprint_coverage`
    applied the SAME slice, so nothing could report the loss."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "blueprint_max_files", 2)
    a = AgentCore(session_id="pytest_scaffold_budget")

    async def _fake_mff(user_message, refs, extra_context="", preplanned_ops=None):
        return "Handled files", []

    monkeypatch.setattr(a, "_multi_file_flow", _fake_mff)

    extra = (PlannedFile("templates/cart.html", "create", "the cart"),)
    answer, _ = await a._run_blueprint(
        "build a bookshop", _web_blueprint(extra_files=extra), []
    )

    assert "may not meet" in answer
    assert "templates/cart.html" in answer


# ---------------------------------------------------------------------------
# Offline-safe generated sites
# ---------------------------------------------------------------------------


def test_build_spec_emits_system_stacks_offline_and_google_fonts_online():
    spec = build_spec_from_data({}, "build me a soft pastel wedding site")

    offline = spec.to_context_block(allow_network=False)
    assert "googleapis" not in offline.lower()
    assert "--font-heading" in offline
    # The preset's pairing intent survives: a serif display face for headings.
    assert "serif" in offline

    online = spec.to_context_block(allow_network=True)
    assert "Google Fonts" in online


def test_build_spec_defaults_to_the_offline_branch():
    """A caller that forgets the argument must not ship a dead CDN dependency."""
    spec = build_spec_from_data({}, "build me a minimalist site")
    assert "googleapis" not in spec.to_context_block().lower()


@pytest.mark.parametrize(
    "markup",
    [
        '<link href="https://fonts.googleapis.com/css2?family=Inter" rel="stylesheet">',
        '<link rel="preconnect" href="https://fonts.gstatic.com">',
        '<script src="https://cdn.tailwindcss.com"></script>',
        '<script src="//cdn.example.com/x.js"></script>',
    ],
)
def test_external_assets_are_found_and_stripped(markup):
    html = f"<!doctype html><html><head>{markup}</head><body></body></html>"
    assert find_external_assets(html, ".html")
    cleaned, removed = strip_external_assets(html, ".html")
    assert len(removed) == 1
    assert "http" not in cleaned


def test_stripping_keeps_local_assets_and_real_hyperlinks():
    html = (
        "<!doctype html><html><head>"
        '<link rel="stylesheet" href="css/style.css">'
        '<script src="https://cdn.example.com/x.js"></script>'
        "</head><body>"
        '<a href="https://example.com">docs</a>'
        '<script src="js/app.js"></script>'
        "</body></html>"
    )
    cleaned, removed = strip_external_assets(html, ".html")
    assert len(removed) == 1
    assert 'href="css/style.css"' in cleaned
    assert 'src="js/app.js"' in cleaned
    assert "https://example.com" in cleaned  # an <a> is not a render dependency


def test_css_external_import_is_stripped():
    css = '@import url("https://fonts.googleapis.com/css2?family=Inter");\nbody{color:red}'
    cleaned, removed = strip_external_assets(css, ".css")
    assert len(removed) == 1
    assert "googleapis" not in cleaned
    assert "color:red" in cleaned


def test_clean_files_are_left_alone():
    html = (
        '<!doctype html><html><head><link rel="stylesheet" href="a.css"></head></html>'
    )
    assert find_external_assets(html, ".html") == []
    cleaned, removed = strip_external_assets(html, ".html")
    assert removed == [] and cleaned == html


async def test_verify_and_repair_strips_dead_assets_when_offline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "allow_network", False)
    a = AgentCore(session_id="pytest_offline_assets")
    a._llm_edit = ScriptedLLM(["PASS"])

    page = tmp_path / "index.html"
    page.write_text(
        "<!doctype html><html><head>"
        '<script src="https://cdn.tailwindcss.com"></script>'
        "</head><body><h1>Hi</h1></body></html>",
        encoding="utf-8",
    )

    note, _ = await a._verify_and_repair(page, "index.html")

    assert "cannot load offline" in note
    assert "cdn.tailwindcss" not in page.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Protecting the scaffold's invariants after generation
# ---------------------------------------------------------------------------

_GENERATED_APP = '''"""Blog."""

from flask import Flask, render_template

app = Flask(__name__)


@app.route("/posts")
def list_posts():
    return render_template("posts.html")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
'''


def test_restore_index_route_when_generation_deleted_it():
    """Measured twice live: the surgical edit REPLACES the `/` route block with
    the new routes, so the finished site 404s on its own home page."""
    restored, changed = restore_index_route(_GENERATED_APP)

    assert changed is True
    assert '@app.route("/")' in restored
    assert "def index():" in restored
    # It goes before the __main__ guard, not after it.
    assert restored.index('@app.route("/")') < restored.index("if __name__")
    assert '@app.route("/posts")' in restored  # the generated route survives
    compile(restored, "app.py", "exec")  # and the result still parses


def test_restore_index_route_is_a_no_op_when_the_route_survives():
    source = _GENERATED_APP.replace(
        '@app.route("/posts")',
        '@app.route("/")\ndef index():\n    return "hi"\n\n\n@app.route("/posts")',
    )
    _, changed = restore_index_route(source)
    assert changed is False


@pytest.mark.parametrize(
    "source,reason",
    [
        ("print('hello')\n", "not a flask route file"),
        (
            '@app.route("/x")\ndef x():\n    return "y"\n',
            "render_template not imported",
        ),
    ],
)
def test_restore_index_route_declines_rather_than_guesses(source, reason):
    """Synthesizing a route that raises NameError would be worse than the 404."""
    _, changed = restore_index_route(source)
    assert changed is False, reason


def test_templates_without_inheritance_flags_full_documents(tmp_path):
    tpl = tmp_path / "templates"
    tpl.mkdir()
    (tpl / "base.html").write_text(
        "<html><body>{% block content %}{% endblock %}</body></html>", encoding="utf-8"
    )
    (tpl / "good.html").write_text(
        '{% extends "base.html" %}{% block content %}hi{% endblock %}', encoding="utf-8"
    )
    (tpl / "bad.html").write_text(
        "<!doctype html><html><body><nav>own nav</nav></body></html>", encoding="utf-8"
    )

    orphans = templates_without_inheritance(tmp_path)

    assert orphans == ["templates/bad.html"]  # base.html and the good one excluded


def test_templates_without_inheritance_is_empty_without_a_templates_dir(tmp_path):
    assert templates_without_inheritance(tmp_path) == []


# The shape a live build actually produced for templates/posts.html.
_ORPHAN_PAGE = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>All Posts</title>
    <link rel="stylesheet" href="/static/css/style.css" />
  </head>
  <body>
    <header><nav><a href="/">Home</a><a href="/posts">Posts</a></nav></header>
    <main>
      <h1>All Posts</h1>
      <ul>
        {% for post in posts %}
        <li>{{ post.title }}</li>
        {% endfor %}
      </ul>
    </main>
    <footer><p>My blog</p></footer>
    <script src="/static/js/app.js"></script>
  </body>
</html>
"""


def test_convert_lifts_the_body_into_a_content_block():
    converted, ok = convert_to_child_template(_ORPHAN_PAGE)

    assert ok is True
    assert converted.startswith('{% extends "base.html" %}')
    assert "{% block content %}" in converted and "{% endblock %}" in converted
    # The page's own content survives, Jinja loops included.
    assert "<h1>All Posts</h1>" in converted
    assert "{% for post in posts %}" in converted
    assert "{{ post.title }}" in converted
    # The title is carried over rather than lost.
    assert "{% block title %}All Posts{% endblock %}" in converted


def test_convert_drops_the_chrome_base_html_already_renders():
    """Left in place these render twice — two navbars on one page is worse than
    the inconsistent-navbar bug this layout exists to remove."""
    converted, _ = convert_to_child_template(_ORPHAN_PAGE)
    assert "<nav" not in converted
    assert "<header" not in converted
    assert "<footer" not in converted
    assert "app.js" not in converted
    assert "<html" not in converted and "<head" not in converted


def test_convert_is_a_no_op_on_a_page_that_already_extends():
    good = '{% extends "base.html" %}\n{% block content %}<p>hi</p>{% endblock %}\n'
    assert convert_to_child_template(good) == (good, False)


def test_convert_is_a_no_op_on_a_fragment():
    fragment = "<section><h1>Not a document</h1></section>"
    assert convert_to_child_template(fragment) == (fragment, False)


def test_convert_declines_when_nothing_would_survive():
    """A page that is ONLY chrome would convert to an empty block. Better to
    leave a wrong-shaped page than to replace it with a blank one."""
    chrome_only = (
        "<html><body><header><nav><a href='/'>Home</a></nav></header></body></html>"
    )
    assert convert_to_child_template(chrome_only) == (chrome_only, False)


def test_converted_page_renders_through_the_real_scaffold(tmp_path):
    """End to end against Jinja itself: the rewritten child must actually render
    inside the scaffold's base.html."""
    jinja = pytest.importorskip("jinja2")

    scaffold_flask(tmp_path, "Blog")
    converted, ok = convert_to_child_template(_ORPHAN_PAGE)
    assert ok
    (tmp_path / "templates" / "posts.html").write_text(converted, encoding="utf-8")

    env = jinja.Environment(
        loader=jinja.FileSystemLoader(str(tmp_path / "templates")),
        autoescape=True,
    )
    env.globals["url_for"] = lambda endpoint, **kw: "/" + kw.get("filename", "")
    html = env.get_template("posts.html").render(posts=[{"title": "First"}])

    assert "All Posts" in html
    assert "First" in html
    assert 'class="site-nav"' in html  # the nav now comes from base.html
    assert html.count("<nav") == 1  # and exactly once


# ---------------------------------------------------------------------------
# Template expressions are not file paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "{{ url_for('static', filename='css/style.css') }}",
        "{{ url_for('index') }}",
        "{% static 'css/style.css' %}",
        "<%= stylesheet_path %>",
        "${assetUrl}",
    ],
)
def test_template_expressions_are_not_treated_as_references(value):
    """The Flask scaffold put Jinja into every page. Read literally, these look
    like relative paths: the reference check reported a stylesheet that exists as
    missing, and the link repair would rewrite an extensionless one to
    `{{ url_for('posts') }}.html`, corrupting a working template."""
    assert is_template_expression(value) is True
    html = f'<!doctype html><html><head><link rel="stylesheet" href="{value}"></head></html>'
    assert extract_local_references(html, ".html") == []


def test_real_local_references_still_resolve():
    """The guard must not blind the check to genuine dead references."""
    html = (
        '<!doctype html><html><head><link rel="stylesheet" href="css/style.css">'
        '</head><body><script src="js/app.js"></script></body></html>'
    )
    assert extract_local_references(html, ".html") == ["css/style.css", "js/app.js"]


def test_page_link_repair_leaves_jinja_hrefs_alone(tmp_path):
    """`find_broken_page_links` rewrites an extensionless href to `<name>.html`.
    A Jinja href is extensionless too — and must never be rewritten."""
    (tmp_path / "posts.html").write_text("<html></html>", encoding="utf-8")
    page = tmp_path / "index.html"
    page.write_text(
        "<html><body>"
        "<a href=\"{{ url_for('posts') }}\">Posts</a>"
        '<a href="posts">Posts</a>'
        "</body></html>",
        encoding="utf-8",
    )

    fixes = find_broken_page_links(page, tmp_path)

    targets = [old for old, _ in fixes]
    assert "posts" in targets  # the real extensionless link is still fixed
    assert not any("url_for" in t for t in targets)


# ---------------------------------------------------------------------------
# Upload forms — the attribute a file input cannot work without
# ---------------------------------------------------------------------------


def test_a_file_input_without_enctype_is_detected_and_fixed():
    """Measured live on the admin form an amendment had just created: without
    enctype the browser posts only the filename, `request.files[...]` raises,
    and the upload silently never happens."""
    html = (
        "<form method='post' action='/admin/products'>"
        "<input type='text' name='title'>"
        "<input type='file' name='image'>"
        "</form>"
    )
    assert forms_missing_enctype(html)

    fixed, count = fix_form_enctype(html)
    assert count == 1
    assert 'enctype="multipart/form-data"' in fixed
    assert "name='title'" in fixed  # nothing else touched


def test_a_form_that_already_declares_enctype_is_left_alone():
    html = (
        '<form method="post" enctype="multipart/form-data">'
        '<input type="file" name="image"></form>'
    )
    assert forms_missing_enctype(html) == []
    assert fix_form_enctype(html) == (html, 0)


def test_a_form_with_no_file_input_is_left_alone():
    html = '<form method="post"><input type="text" name="q"></form>'
    assert fix_form_enctype(html) == (html, 0)


def test_a_missing_post_method_is_added_alongside_enctype():
    """A file upload over GET cannot work either."""
    fixed, count = fix_form_enctype('<form action="/x"><input type="file"></form>')
    assert count == 1
    assert 'method="post"' in fixed and "multipart/form-data" in fixed


def test_only_the_form_with_the_file_input_is_changed():
    html = (
        '<form action="/search"><input type="text" name="q"></form>'
        '<form action="/upload"><input type="file" name="f"></form>'
    )
    fixed, count = fix_form_enctype(html)
    assert count == 1
    assert fixed.count("multipart/form-data") == 1
    assert '<form action="/search">' in fixed


async def test_verify_and_repair_fixes_the_upload_form(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "allow_network", True)  # keep stage 0 inert
    a = AgentCore(session_id="pytest_enctype")

    page = tmp_path / "admin.html"
    page.write_text(
        "<!doctype html><html><body>"
        "<form method='post' action='/admin/products'>"
        "<input type='file' name='image'></form>"
        "</body></html>",
        encoding="utf-8",
    )

    note, _ = await a._verify_and_repair(page, "admin.html")

    assert "multipart" in note
    assert 'enctype="multipart/form-data"' in page.read_text(encoding="utf-8")


async def test_verify_and_repair_keeps_cdn_assets_when_network_is_allowed(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "allow_network", True)
    a = AgentCore(session_id="pytest_online_assets")

    page = tmp_path / "index.html"
    original = (
        "<!doctype html><html><head>"
        '<script src="https://cdn.tailwindcss.com"></script>'
        "</head><body><h1>Hi</h1></body></html>"
    )
    page.write_text(original, encoding="utf-8")

    await a._verify_and_repair(page, "index.html")

    assert "cdn.tailwindcss" in page.read_text(encoding="utf-8")
