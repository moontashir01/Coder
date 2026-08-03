"""Block-aware template edits (Phase W3, docs/web-quality-plan.md).

The failure being closed, measured on live builds: `_surgical_edit` runs
SEARCH/REPLACE across the whole file, and a 7B asked to add something to a page
answers with *just that block* — so applying its edit deletes `{% extends %}`,
the title, and the page's membership of the layout. `_restore_scaffold_invariants`
and `convert_to_child_template` exist to repair that afterwards, which is
strictly worse than not causing it.

Two halves, like every other phase here: `scaffold.template_edit_region` is pure
and decides nothing about editing, and the core wiring proves the block really
is the only thing the model can reach.
"""

from types import SimpleNamespace

import pytest

from app.agent.core import AgentCore
from app.agent.scaffold import template_edit_region

CHILD = """{% extends "base.html" %}
{% import "_macros.html" as ui %}

{% block title %}Products{% endblock %}

{% block content %}
<h1>Products</h1>
<p>Nothing here yet.</p>
{% endblock %}
"""


class ScriptedLLM:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0
        self.prompts = []

    def invoke(self, messages):
        self.prompts.append("\n".join(str(m.content) for m in messages))
        out = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        return SimpleNamespace(content=out)


# --- finding the block ------------------------------------------------------


def test_the_content_block_is_the_edit_region():
    region = template_edit_region("templates/products.html", CHILD)
    assert region is not None
    assert region.name == "content"
    assert "<h1>Products</h1>" in region.body
    # The tags themselves are OUTSIDE the span, so a splice cannot lose them.
    assert "{% block" not in region.body and "{% endblock %}" not in region.body
    assert "title" in region.siblings


def test_splice_puts_the_body_back_and_nothing_else_moves():
    region = template_edit_region("templates/products.html", CHILD)
    out = region.splice(CHILD, "\n<h1>Products</h1>\n<table></table>\n")
    assert out.startswith('{% extends "base.html" %}')
    assert "{% block title %}Products{% endblock %}" in out
    assert "<table></table>" in out
    assert out.count("{% block content %}") == 1
    assert out.count("{% endblock %}") == 2


@pytest.mark.parametrize(
    "filename",
    ["app.py", "static/css/style.css", "index.html", "templates/base.txt"],
)
def test_only_html_under_templates_is_a_child_template(filename):
    assert template_edit_region(filename, CHILD) is None


def test_a_full_document_is_left_to_convert_to_child_template():
    """A template with no `{% extends %}` is not a child template — converting
    one is `convert_to_child_template`'s job, and this pass must not claim it."""
    doc = "<!doctype html><html><body>{% block content %}hi{% endblock %}</body></html>"
    assert template_edit_region("templates/about.html", doc) is None


def test_a_nested_block_declines():
    """Ambiguity falls back to the existing whole-file path, always."""
    nested = (
        '{% extends "base.html" %}\n'
        "{% block content %}\n<div>{% block inner %}x{% endblock %}</div>\n"
        "{% endblock %}\n"
    )
    assert template_edit_region("templates/x.html", nested) is None


def test_unbalanced_tags_decline():
    broken = '{% extends "base.html" %}\n{% block content %}\n<h1>Oops</h1>\n'
    assert template_edit_region("templates/x.html", broken) is None


def test_an_empty_block_declines():
    """SEARCH/REPLACE against an empty body has nothing to match — that request
    belongs on the whole-file path."""
    empty = '{% extends "base.html" %}\n{% block content %}{% endblock %}\n'
    assert template_edit_region("templates/x.html", empty) is None


def test_a_single_named_block_is_used_when_there_is_no_content_block():
    text = '{% extends "base.html" %}\n{% block body %}\n<p>Hi</p>\n{% endblock %}\n'
    region = template_edit_region("templates/x.html", text)
    assert region is not None and region.name == "body"


def test_two_candidate_blocks_decline():
    text = (
        '{% extends "base.html" %}\n'
        "{% block left %}\n<p>a</p>\n{% endblock %}\n"
        "{% block right %}\n<p>b</p>\n{% endblock %}\n"
    )
    assert template_edit_region("templates/x.html", text) is None


def test_the_title_block_is_never_edited_alone():
    """A request that really is about the title is exactly what the whole-file
    fallback is for; a one-line block gives SEARCH nothing to match."""
    text = '{% extends "base.html" %}\n{% block title %}Shop{% endblock %}\n'
    assert template_edit_region("templates/x.html", text) is None


def test_whitespace_control_tags_are_recognised():
    text = '{%- extends "base.html" -%}\n{%- block content -%}\n<p>Hi</p>\n{%- endblock -%}\n'
    region = template_edit_region("templates/x.html", text)
    assert region is not None and "<p>Hi</p>" in region.body


def test_a_named_endblock_is_recognised():
    text = (
        '{% extends "base.html" %}\n'
        "{% block content %}\n<p>Hi</p>\n{% endblock content %}\n"
    )
    region = template_edit_region("templates/x.html", text)
    assert region is not None and region.name == "content"


# --- the edit itself --------------------------------------------------------


async def test_editing_a_page_cannot_delete_the_layout(tmp_path, monkeypatch):
    """THE regression this phase exists for.

    The scripted reply is what a 7B really sends: the new block body, with the
    `{% extends %}` line nowhere in it. On the whole-file path that SEARCH
    matches the top of the file and the page stops being a child template. Here
    it is matched against the block body only, so it cannot.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "templates").mkdir()
    page = tmp_path / "templates" / "products.html"
    page.write_text(CHILD, encoding="utf-8")

    a = AgentCore(session_id="pytest_w3_edit")
    a._llm_edit = ScriptedLLM(
        [
            "<<<<<<< SEARCH\n<p>Nothing here yet.</p>\n=======\n"
            "<p>Nothing here yet.</p>\n<form><input name='q'></form>\n"
            ">>>>>>> REPLACE"
        ]
    )

    answer, _ = await a._file_op_flow(
        "add a search box", target="templates/products.html"
    )

    out = page.read_text(encoding="utf-8")
    assert out.startswith('{% extends "base.html" %}')
    assert "{% block title %}Products{% endblock %}" in out
    assert "<form><input name='q'></form>" in out
    assert "block content" in answer


async def test_the_model_is_shown_the_block_not_the_file(tmp_path, monkeypatch):
    """It cannot delete what it was never given."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "products.html").write_text(CHILD, encoding="utf-8")

    a = AgentCore(session_id="pytest_w3_prompt")
    a._llm_edit = ScriptedLLM(
        ["<<<<<<< SEARCH\n<h1>Products</h1>\n=======\n<h1>Books</h1>\n>>>>>>> REPLACE"]
    )
    await a._file_op_flow("rename the heading", target="templates/products.html")

    prompt = a._llm_edit.prompts[0]
    assert "<h1>Products</h1>" in prompt
    assert '{% extends "base.html" %}' not in prompt
    assert "block content" in prompt


async def test_a_block_edit_that_does_not_match_falls_back_to_the_whole_file(
    tmp_path, monkeypatch
):
    """The fallback rule: the existing path is never taken away, only tried
    second. Here the request is about the TITLE, which is not in the block."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "templates").mkdir()
    page = tmp_path / "templates" / "products.html"
    page.write_text(CHILD, encoding="utf-8")

    a = AgentCore(session_id="pytest_w3_fallback")
    a._llm_edit = ScriptedLLM(
        [
            # 1st + 2nd call: the block-confined attempt (and its one retry) —
            # a SEARCH for text that is not in the block body.
            "<<<<<<< SEARCH\n{% block title %}Products{% endblock %}\n=======\n"
            "{% block title %}Books{% endblock %}\n>>>>>>> REPLACE",
        ]
    )

    answer, _ = await a._file_op_flow(
        "change the page title to Books", target="templates/products.html"
    )

    out = page.read_text(encoding="utf-8")
    assert "{% block title %}Books{% endblock %}" in out
    assert out.startswith('{% extends "base.html" %}')
    assert "block content" not in answer  # it took the whole-file path


async def test_a_plain_html_file_still_takes_the_old_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    page = tmp_path / "index.html"
    page.write_text("<html><body><h1>Hi</h1></body></html>", encoding="utf-8")

    a = AgentCore(session_id="pytest_w3_plain")
    a._llm_edit = ScriptedLLM(
        ["<<<<<<< SEARCH\n<h1>Hi</h1>\n=======\n<h1>Hello</h1>\n>>>>>>> REPLACE"]
    )
    answer, _ = await a._file_op_flow("say hello", target="index.html")

    assert "<h1>Hello</h1>" in page.read_text(encoding="utf-8")
    assert "block" not in answer
    assert a._llm_edit.calls == 1  # no extra attempt was spent
