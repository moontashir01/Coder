"""The design system: component sheet, macro library, generated theme (Phase W1).

`docs/web-quality-plan.md`. All of it is deterministic — a file copy and some
colour arithmetic — so all of it tests offline with no LLM and no browser.

The load-bearing tests here are the *drift* ones. Shipping components is only
half the phase; the other half is `ui_context()` telling the model they exist,
and a context block that names a macro the templates do not define is worse than
no block at all — the model calls it and the page 500s on render. That is the
`crud.api_context()` failure mode one layer up, and these tests are what stop it
recurring silently.
"""

import re

import pytest

from app.agent import buildspec
from app.agent.buildspec import (
    contrast_ratio,
    relative_luminance,
    resolve_theme,
    theme_css,
    theme_tokens,
)
from app.agent.scaffold import (
    flask_scaffold_dir,
    is_frozen,
    scaffold_files,
    scaffold_flask,
    ui_context,
    write_theme,
)

jinja2 = pytest.importorskip("jinja2")


def _scaffold_text(rel: str) -> str:
    return (flask_scaffold_dir() / rel).read_text(encoding="utf-8")


# --- W1a: the component sheet ----------------------------------------------


def test_scaffold_ships_the_component_sheet_and_macros():
    files = scaffold_files()
    assert "static/css/style.css" in files
    assert "static/css/theme.css" in files
    assert "templates/_macros.html" in files


@pytest.mark.parametrize(
    "selector",
    [
        ".table-wrap",
        ".table",
        ".empty",
        ".alert-success",
        ".alert-error",
        ".badge",
        ".breadcrumb",
        ".pagination",
        ".sidebar-layout",
        ".card-media",
        ".field",
        ".button-danger",
        ".visually-hidden",
    ],
)
def test_component_sheet_defines(selector):
    assert selector in _scaffold_text("static/css/style.css")


def test_component_sheet_is_written_in_variables_not_literals():
    """No hardcoded colour outside the token block.

    The whole claim of theme.css is that a restyle is a one-file change. A rule
    that names `#2563eb` directly is a rule the theme cannot reach, and it only
    shows up as one component keeping the old palette after a restyle.
    """
    css = _scaffold_text("static/css/style.css")
    body = css.split("*,\n*::before", 1)[1]  # everything after the :root block
    hardcoded = [
        hex_value
        for hex_value in re.findall(r"#[0-9a-fA-F]{3,8}\b", body)
        # White/black are legitimate for a fixed-contrast surface (a danger
        # button's label), and rgba() shadows carry no hue.
        if hex_value.lower() not in ("#ffffff", "#fff", "#000000", "#000")
    ]
    assert hardcoded == [], f"colour literals outside :root: {hardcoded}"


def test_dark_scheme_lives_in_theme_not_in_the_component_sheet():
    """theme.css is linked last, so a `@media` block in style.css would lose to
    its plain `:root` and the site would be stuck light with no visible cause."""
    assert "prefers-color-scheme" not in _scaffold_text("static/css/style.css")
    assert "prefers-color-scheme" in _scaffold_text("static/css/theme.css")


def test_base_links_theme_after_style(tmp_path):
    """Order is the mechanism, not a detail — theme.css must win."""
    scaffold_flask(tmp_path, "Shop")
    base = (tmp_path / "templates" / "base.html").read_text(encoding="utf-8")
    assert base.index("css/style.css") < base.index("css/theme.css")


def test_macros_and_theme_are_frozen():
    """Both are shipped whole; handing either to the model rewrites the classes
    every page depends on. A restyle edits theme.css's variables instead."""
    assert is_frozen("templates/_macros.html")
    assert is_frozen("static/css/theme.css")
    # The component sheet is NOT frozen: a build may legitimately extend it.
    assert not is_frozen("static/css/style.css")


# --- W1c: the macro library actually renders --------------------------------


def _render(tmp_path, template: str, **context) -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(tmp_path / "templates")),
        autoescape=True,
    )
    env.globals["url_for"] = lambda endpoint, **kw: "/" + kw.get("filename", endpoint)
    env.globals["get_flashed_messages"] = lambda **kw: []
    return env.get_template(template).render(**context)


def test_scaffolded_pages_render(tmp_path):
    """The scaffold's own pages must render before the model touches them —
    a Jinja syntax error in base.html or _macros.html breaks every page at once."""
    scaffold_flask(tmp_path, "Shop")
    html = _render(tmp_path, "index.html")
    assert "Shop" in html
    assert "css/theme.css" in html


def test_table_macro_renders_rows_and_wraps_them(tmp_path):
    scaffold_flask(tmp_path, "Shop")
    (tmp_path / "templates" / "t.html").write_text(
        '{% import "_macros.html" as ui %}' "{{ ui.table(rows, ['name', 'price']) }}",
        encoding="utf-8",
    )
    html = _render(tmp_path, "t.html", rows=[{"name": "Book", "price": "9.00"}])
    # .table-wrap is what stops a wide table scrolling the whole page sideways.
    assert "table-wrap" in html
    assert "Book" in html and "9.00" in html
    assert '<th scope="col">Name</th>' in html


def test_table_macro_falls_back_to_an_empty_state(tmp_path):
    """An empty list is a finished page, not a blank one."""
    scaffold_flask(tmp_path, "Shop")
    (tmp_path / "templates" / "t.html").write_text(
        "{% import \"_macros.html\" as ui %}{{ ui.table([], ['name']) }}",
        encoding="utf-8",
    )
    html = _render(tmp_path, "t.html")
    assert 'class="empty"' in html


def test_field_macro_ties_label_to_input(tmp_path):
    """Without for/id the label does nothing on click and screen readers
    announce the field unnamed."""
    scaffold_flask(tmp_path, "Shop")
    (tmp_path / "templates" / "t.html").write_text(
        '{% import "_macros.html" as ui %}{{ ui.field("cover_path", type="file") }}',
        encoding="utf-8",
    )
    html = _render(tmp_path, "t.html")
    assert 'for="cover_path"' in html and 'id="cover_path"' in html
    assert 'type="file"' in html
    assert "Cover Path" in html  # label derived from the column name


def test_base_renders_flashes_once(tmp_path):
    scaffold_flask(tmp_path, "Shop")
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(tmp_path / "templates")), autoescape=True
    )
    env.globals["url_for"] = lambda endpoint, **kw: "/" + kw.get("filename", endpoint)
    env.globals["get_flashed_messages"] = lambda **kw: [("success", "Saved.")]
    html = env.get_template("index.html").render()
    assert html.count("Saved.") == 1
    assert "alert-success" in html


# --- W1d: the context block cannot drift from the files ---------------------


def test_ui_context_names_every_macro():
    """Every macro on disk is advertised, and nothing else is.

    Both directions matter: an unadvertised macro is dead weight the model never
    calls, and an advertised one that does not exist is a render-time 500.
    """
    macros = set(
        re.findall(
            r"{%\s*macro\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
            _scaffold_text("templates/_macros.html"),
        )
    )
    assert macros, "no macros found — did _macros.html move?"
    block = ui_context()
    missing = sorted(name for name in macros if f"`{name}(" not in block)
    assert not missing, f"ui_context() does not mention: {missing}"

    # Only the macro bullets (two-space indent) — the prose also mentions
    # `url_for(...)` and `var(--color-accent)`, which are not macros.
    advertised = set(re.findall(r"\n  - `([a-z_]+)\(", block))
    invented = sorted(advertised - macros)
    assert not invented, f"ui_context() advertises macros that don't exist: {invented}"


def test_ui_context_only_names_classes_the_sheet_defines():
    css = _scaffold_text("static/css/style.css")
    named = set(re.findall(r"`(\.[a-z][a-z0-9-]*)`", ui_context()))
    assert named, "ui_context() names no classes — did the block change shape?"
    missing = sorted(cls for cls in named if cls not in css)
    assert not missing, f"ui_context() names undefined classes: {missing}"


# --- W1b: themes as data ----------------------------------------------------


def test_no_style_words_means_no_theme():
    """Inventing a look nobody asked for is the failure `_clean_nav` prevents;
    the request keeps the scaffold's default theme instead."""
    assert resolve_theme("build me a site to track my books") == {}
    assert theme_css({}) == ""


def test_a_style_word_resolves_to_real_tokens():
    theme = resolve_theme("build me a soft pastel wedding site")
    tokens = theme["tokens"]
    assert tokens["--color-bg"].startswith("#")
    assert "--font-heading" in tokens
    css = theme_css(theme)
    assert ":root {" in css
    assert "--color-accent:" in css
    # No dark block: a chosen palette has no correct mechanical inverse.
    assert "prefers-color-scheme" not in css


def test_theme_roles_follow_lightness_not_listing_order():
    """The same palette in any order produces the same theme."""
    palette = ("#4a3f45", "#f6e7ef", "#b28fa8", "#fceade", "#e8dff5")
    first = theme_tokens(palette)
    second = theme_tokens(tuple(reversed(palette)))
    assert first == second
    assert relative_luminance(first["--color-bg"]) > relative_luminance(
        first["--color-text"]
    )


def test_a_dark_palette_declares_a_dark_color_scheme():
    css = theme_css(resolve_theme("build me a dark mode dashboard"))
    assert "color-scheme: dark" in css


@pytest.mark.parametrize("pattern,preset", buildspec._STYLE_PRESETS)
def test_every_preset_is_legible(pattern, preset):
    """A preset that ships an unreadable pairing would now ship it site-wide.

    This is the check that makes `_ensure_contrast` worth having: text on the
    page and the accent used for link text both have to clear WCAG AA, or the
    theme is a downgrade from the default no matter how well it matches the
    mood.
    """
    tokens = theme_tokens(tuple(preset["palette"]), tuple(preset.get("stacks") or ()))
    assert tokens, f"{pattern.pattern} produced no tokens"
    bg = tokens["--color-bg"]
    assert contrast_ratio(tokens["--color-text"], bg) >= 4.5
    assert contrast_ratio(tokens["--color-accent"], bg) >= 4.5
    assert (
        contrast_ratio(tokens["--color-accent-text"], tokens["--color-accent"]) >= 4.5
    )


def test_contrast_ratio_matches_known_values():
    assert contrast_ratio("#ffffff", "#000000") == pytest.approx(21.0, abs=0.01)
    assert contrast_ratio("#ffffff", "#ffffff") == pytest.approx(1.0, abs=0.01)


def test_write_theme_replaces_the_default(tmp_path):
    scaffold_flask(tmp_path, "Shop")
    target = tmp_path / "static" / "css" / "theme.css"
    default = target.read_text(encoding="utf-8")

    assert write_theme(tmp_path, theme_css(resolve_theme("a soft pastel shop")))
    written = target.read_text(encoding="utf-8")
    assert written != default
    assert "--color-accent:" in written


def test_write_theme_declines_empty_css(tmp_path):
    """A request with no style words must not blank the default theme."""
    scaffold_flask(tmp_path, "Shop")
    target = tmp_path / "static" / "css" / "theme.css"
    before = target.read_text(encoding="utf-8")
    assert write_theme(tmp_path, theme_css(resolve_theme("build me a blog"))) is False
    assert target.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# The macro import is per-template — Jinja does not inherit it
# ---------------------------------------------------------------------------


async def test_macro_import_added_to_a_page_that_calls_ui(tmp_path, monkeypatch):
    """`base.html` importing the macros does nothing for a child, so a page that
    calls `ui.field(...)` without its own import is `UndefinedError: 'ui' is
    undefined` — a 500 on a file that parses, balances and passes the intent
    judge. Measured on a live build, on one page of fourteen."""
    monkeypatch.chdir(tmp_path)
    from app.agent.core import AgentCore

    templates = tmp_path / "templates"
    templates.mkdir()
    page = templates / "new_item.html"
    page.write_text(
        '{% extends "base.html" %}\n'
        "{% block content %}{{ ui.field('title') }}{% endblock %}\n",
        encoding="utf-8",
    )
    a = AgentCore(session_id="pytest_macro_import")

    note = await a._fix_macro_import(page, "templates/new_item.html")

    text = page.read_text(encoding="utf-8")
    assert '{% import "_macros.html" as ui %}' in text
    # After {% extends %}: a statement above it is outside every block, so the
    # import would parse and still not bind.
    assert text.index("extends") < text.index("_macros.html")
    assert note


async def test_macro_import_not_added_twice(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from app.agent.core import AgentCore

    templates = tmp_path / "templates"
    templates.mkdir()
    page = templates / "items.html"
    original = (
        '{% extends "base.html" %}\n'
        '{% import "_macros.html" as ui %}\n'
        "{% block content %}{{ ui.table([], []) }}{% endblock %}\n"
    )
    page.write_text(original, encoding="utf-8")
    a = AgentCore(session_id="pytest_macro_import_twice")

    note = await a._fix_macro_import(page, "templates/items.html")

    assert page.read_text(encoding="utf-8") == original
    assert note == ""


async def test_macro_import_not_added_to_a_page_that_never_calls_ui(
    tmp_path, monkeypatch
):
    """Narrow like `_repair_missing_imports`: it fires only when the file USES
    the name. Injecting an import nobody needs is markup nobody asked for."""
    monkeypatch.chdir(tmp_path)
    from app.agent.core import AgentCore

    templates = tmp_path / "templates"
    templates.mkdir()
    page = templates / "about.html"
    original = '{% extends "base.html" %}\n{% block content %}<p>hi</p>{% endblock %}\n'
    page.write_text(original, encoding="utf-8")
    a = AgentCore(session_id="pytest_macro_import_unused")

    note = await a._fix_macro_import(page, "templates/about.html")

    assert page.read_text(encoding="utf-8") == original
    assert note == ""
