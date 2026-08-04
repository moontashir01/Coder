"""The JavaScript inside an EJS view, and the prose that is not a view at all.

Both of these were measured on one live Node build (OpenBazaar). Three of the
site's six nav destinations answered 500 — the home page among them — from views
that passed every check in the pipeline:

  * `views/users.ejs` used an OUTPUT tag around a statement,
    `<%- users.forEach(user => { %>`, so EJS emitted `__append(users.forEach(u
    => {)` and threw at render time. `strip_ejs` takes the JavaScript OUT before
    balancing the markup, so nothing ever looked at it.
  * `views/items.ejs` had a sentence of the model's own prose welded into a
    call's argument list.
  * a root-level `users.ejs` was written whose entire content was the model
    asking what was wrong — valid, balanced, tag-free "markup".
"""

from __future__ import annotations

import shutil

import pytest

from app.agent.verify import check_file, check_text, ejs_script, is_verifiable

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed"
)


# --- ejs_script: the translation itself ------------------------------------


def test_output_tag_becomes_an_append_call():
    script = ejs_script("<p><%= user.name %></p>")
    assert "__append(" in script and "user.name" in script


def test_scriptlet_is_copied_through_and_markup_is_not():
    script = ejs_script("<% if (a) { %><p>hello world</p><% } %>")
    assert "if (a) {" in script and "}" in script
    assert "hello" not in script, "markup is a string in the real compile"


def test_a_comment_tag_compiles_to_nothing():
    assert "never" not in ejs_script("<%# never mind %>")


def test_the_literal_escape_is_not_code():
    # `<%%` is how a view prints a literal `<%`; treating it as an open tag
    # would make every such view look unterminated.
    assert "%>" not in ejs_script("<%% not a tag %>")


def test_whitespace_slurp_markers_are_stripped():
    script = ejs_script("<%_ const x = 1; _%>")
    assert "const x = 1;" in script
    assert "_%" not in script


def test_the_script_is_wrapped_so_await_and_return_are_legal():
    script = ejs_script("<% return await thing(); %>")
    assert script.lstrip().startswith("async function")


# --- the check --------------------------------------------------------------


@needs_node
def test_forEach_in_an_output_tag_is_caught(tmp_path):
    """The measured defect, verbatim in shape."""
    view = tmp_path / "users.ejs"
    view.write_text(
        "<div>\n"
        "  <%- users.forEach(user => { %>\n"
        "    <p><%= user.email %></p>\n"
        "  <%- }) %>\n"
        "</div>\n",
        encoding="utf-8",
    )
    ok, error = check_file(view)
    assert not ok
    assert "JavaScript" in error


@needs_node
def test_the_same_loop_as_a_scriptlet_passes(tmp_path):
    """...and the correct spelling of it must not be reported."""
    view = tmp_path / "users.ejs"
    view.write_text(
        "<div>\n"
        "  <% users.forEach(user => { %>\n"
        "    <p><%= user.email %></p>\n"
        "  <% }) %>\n"
        "</div>\n",
        encoding="utf-8",
    )
    assert check_file(view) == (True, "")


@needs_node
def test_prose_welded_into_a_call_is_caught(tmp_path):
    view = tmp_path / "items.ejs"
    view.write_text(
        "<% items.forEach(item => { %>\n"
        "  <%- ui.card(\n"
        "    item.title,\n"
        "    because the helpers return HTML and already escape their values. %>\n",
        encoding="utf-8",
    )
    ok, _ = check_file(view)
    assert not ok


@needs_node
@pytest.mark.parametrize(
    "text",
    [
        "<%- ui.page_header('Users', '/users/new', 'Add User') %>\n",
        "<% if (items.length === 0) { %><p>None yet.</p><% } else { %>"
        "<ul><% items.forEach(i => { %><li><%= i.title %></li><% }) %></ul>"
        "<% } %>\n",
        "<%- ui.table(\n  ['Name', 'Email'],\n  rows.map(r => [r.name, r.email])\n) %>",
        "<p>A hero paragraph with no code in it at all.</p>",
        "<%# just a comment %>\n<section><%= title %></section>",
    ],
)
def test_real_views_are_not_false_failed(tmp_path, text):
    view = tmp_path / "view.ejs"
    view.write_text(text, encoding="utf-8")
    assert check_file(view) == (True, ""), text[:40]


def test_a_missing_node_is_a_skip_not_a_failure(tmp_path, monkeypatch):
    """`.js` behaves this way and `.ejs` must too: a check that could not run
    has verified nothing, and calling that "broken" blocks a correct write."""
    monkeypatch.setattr("app.agent.verify.shutil.which", lambda _name: None)
    view = tmp_path / "users.ejs"
    view.write_text("<%- users.forEach(u => { %><p>x</p><%- }) %>", encoding="utf-8")
    assert check_file(view) == (True, "")


# --- the prose guard --------------------------------------------------------


def test_an_assistant_reply_written_as_a_view_is_rejected():
    ok, error = check_text(
        "To address the request for fixing the `users.ejs` file, I need more "
        "information about the specific issues or errors you are encountering.",
        ".ejs",
        "users.ejs",
    )
    assert not ok
    assert "prose, not a view" in error


def test_a_fragment_of_plain_markup_is_still_a_view():
    """The guard is "no tags AND no EJS", not "looks like prose" — a hero
    paragraph is a legitimate view and must not be judged."""
    assert check_text("<p>Welcome to the shop.</p>", ".ejs", "index.ejs") == (True, "")


def test_a_view_that_is_only_code_is_still_a_view():
    assert check_text("<% /* set up */ %>", ".ejs", "x.ejs")[0]


def test_an_empty_view_is_not_prose():
    assert check_text("", ".ejs", "x.ejs") == (True, "")


def test_ejs_is_still_verifiable():
    assert is_verifiable("views/x.ejs")
