"""`AgentCore._locate_named_file` — the name a person types vs the path on disk.

The measured failure, on a live Node build: "fix the files inside users.ejs".
`_extract_filename` recognised `users.ejs` and returned it; nothing checked that
the project had a `users.ejs` at its root, and a Node build keeps its views in
`views/`. So `_file_op_flow` read an empty string, decided this was a NEW file,
and wrote the model's "I need more information about the specific issues" reply
to disk as a second, junk `users.ejs` beside the real `views/users.ejs`.

Every guard that should have caught it was gated on `filename is None`: the
spec lookup that would have matched `views/users.ejs` by its template stem, and
the tool-loop escalation whose own comment describes this exact outcome. A
WRONG filename is a different failure from a MISSING one, and only the second
was covered.
"""

from __future__ import annotations

import pytest

from app.agent.core import AgentCore


@pytest.fixture
def agent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    return AgentCore()


def _project(tmp_path):
    (tmp_path / "views").mkdir()
    (tmp_path / "views" / "users.ejs").write_text("<p>x</p>", encoding="utf-8")
    (tmp_path / "server.js").write_text("app.listen(3000);", encoding="utf-8")
    return tmp_path


def test_a_bare_name_resolves_to_where_the_file_really_is(agent, tmp_path):
    root = _project(tmp_path)
    assert agent._locate_named_file(
        "users.ejs", root, "fix the files inside users.ejs"
    ) == str(__import__("pathlib").Path("views/users.ejs"))


def test_a_name_at_the_root_is_returned_as_given(agent, tmp_path):
    root = _project(tmp_path)
    assert agent._locate_named_file("server.js", root, "fix server.js") == "server.js"


def test_an_unresolvable_repair_target_becomes_None(agent, tmp_path):
    """This is what lets the spec lookup and then the tool loop have their
    turn, instead of a blind new file at the project root."""
    root = _project(tmp_path)
    assert agent._locate_named_file("nowhere.ejs", root, "fix nowhere.ejs") is None


def test_an_ambiguous_name_becomes_None(agent, tmp_path):
    """Two `index.ejs` mean the message was ambiguous. Falling through beats a
    coin flip — `_resolve_target_from_spec`'s rule."""
    root = _project(tmp_path)
    (root / "views" / "index.ejs").write_text("<p>a</p>", encoding="utf-8")
    (root / "partials").mkdir()
    (root / "partials" / "index.ejs").write_text("<p>b</p>", encoding="utf-8")
    assert agent._locate_named_file("index.ejs", root, "fix index.ejs") is None


def test_a_CREATION_request_keeps_the_name_it_was_given(agent, tmp_path):
    """A file being created is *supposed* not to exist yet. Dropping the name
    here would send every creation down the repair path."""
    root = _project(tmp_path)
    assert (
        agent._locate_named_file("theme.css", root, "create a css file theme.css")
        == "theme.css"
    )


def test_a_backup_copy_is_never_the_resolution(agent, tmp_path):
    """`.coder_backups/` holds a snapshot of every file the agent has written,
    so an unfiltered walk resolves `server.js` to a copy of itself and the edit
    lands on the backup."""
    root = _project(tmp_path)
    backups = root / ".coder_backups"
    backups.mkdir()
    (backups / "01__old__notes.ejs").write_text("<p>old</p>", encoding="utf-8")
    (root / "views" / "notes.ejs").write_text("<p>new</p>", encoding="utf-8")
    resolved = agent._locate_named_file("notes.ejs", root, "fix notes.ejs")
    assert resolved is not None and ".coder_backups" not in resolved


def test_node_modules_is_skipped(agent, tmp_path):
    root = _project(tmp_path)
    vendored = root / "node_modules" / "pkg"
    vendored.mkdir(parents=True)
    (vendored / "helper.js").write_text("//", encoding="utf-8")
    assert agent._locate_named_file("helper.js", root, "fix helper.js") is None


def test_an_explicit_path_is_never_retargeted(agent, tmp_path):
    """ "views/users.ejs" was a real path and it was wrong; searching for a
    basename we were already given in full would silently move the edit
    somewhere else in the tree."""
    root = _project(tmp_path)
    assert (
        agent._locate_named_file("templates/users.ejs", root, "fix templates/users.ejs")
        is None
    )


def test_an_empty_name_is_None(agent, tmp_path):
    assert agent._locate_named_file("", tmp_path, "fix it") is None
