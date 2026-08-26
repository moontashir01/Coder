"""A turn on an existing project must not be re-read as a greenfield build.

Measured on a live static build: turn 1 wrote a six-file browser game; turn 2
said "Fix js/audio.js. It loads sounds/shoot.wav with XMLHttpRequest ... replace
it with Web Audio synthesis", the web-intent classifier read that as a request
for a web app, and the turn scaffolded a whole Express project — `server.js`,
`db.js`, `models.js`, `seed.js`, `views/`, `package.json` — into a folder that
is a static site. It then reported that its smoke test could not run because
`node_modules` was missing.

`should_amend` is the guard that should have caught it, and it is gated on a
saved ProjectSpec — which a static build never writes. So nothing protected one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.core import AgentCore


@pytest.fixture
def agent():
    return AgentCore(session_id="pytest_static_guard")


@pytest.fixture
def project(tmp_path):
    (tmp_path / "js").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><html></html>", encoding="utf-8")
    (tmp_path / "js" / "audio.js").write_text("// sounds\n", encoding="utf-8")
    (tmp_path / "views").mkdir()
    (tmp_path / "views" / "users.ejs").write_text("<p>x</p>", encoding="utf-8")
    return tmp_path


def test_a_message_naming_an_existing_file_is_an_edit(agent, project):
    agent._project_path = str(project)
    assert agent._names_an_existing_file(
        "Fix js/audio.js. It loads sounds/shoot.wav with XMLHttpRequest"
    )


def test_a_bare_name_that_exists_deeper_in_the_project_counts(agent, project):
    """`_locate_named_file`'s rule: the name people type is rarely the path."""
    agent._project_path = str(project)
    assert agent._names_an_existing_file("fix the files inside users.ejs")


def test_a_greenfield_build_names_nothing(agent, project):
    agent._project_path = str(project)
    assert not agent._names_an_existing_file("build me a marketplace for used bikes")


def test_a_file_the_project_does_not_have_is_not_a_veto(agent, project):
    """Creating something is supposed to name a file that does not exist yet."""
    agent._project_path = str(project)
    assert not agent._names_an_existing_file("create a theme.css for the styling")


def test_with_no_project_loaded_nothing_is_vetoed(agent):
    assert not agent._names_an_existing_file("fix js/audio.js")


def test_an_unreadable_project_path_is_not_a_veto(agent, tmp_path):
    agent._project_path = str(tmp_path / "gone")
    assert agent._names_an_existing_file("fix js/audio.js") is False
