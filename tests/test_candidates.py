"""Best-of-N and the model roles (Phase W9, docs/web-quality-plan.md).

The phase's honest claim is narrow: offline on a 7B, sampling twice and letting
the DETERMINISTIC checks pick is the only way left to raise output quality, and
it is sound *only* because W2/W5/W6 made those checks objective. So these tests
are mostly about the scorer being a measurement rather than an opinion — and
about N=1, the default, changing nothing at all.
"""

from types import SimpleNamespace

import pytest

from app.agent.candidates import (
    Score,
    describe_choice,
    is_high_value,
    pick_best,
    score_candidate,
)
from app.agent.core import AgentCore
from app.models.llm import get_llm
from config.settings import settings

GOOD_PAGE = """{% extends "base.html" %}
{% block content %}
<h1>Products</h1>
<a href="{{ url_for('products') }}">All products</a>
{% endblock %}
"""

KNOWN = {"index", "products", "add_product"}


class ScriptedLLM:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    def invoke(self, messages):
        out = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        return SimpleNamespace(content=out)


# --- which files are worth paying twice for ---------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("templates/products.html", True),
        ("app.py", True),
        ("templates/base.html", True),
        ("static/css/style.css", False),
        ("README.md", False),
        ("requirements.txt", False),
    ],
)
def test_high_value_files(name, expected):
    assert is_high_value(name) is expected


# --- the score is a measurement ---------------------------------------------


def test_a_page_that_parses_and_extends_the_layout_scores_highest():
    good = score_candidate(GOOD_PAGE, "templates/products.html", KNOWN)
    assert good.points > 100 and good.reasons == ()


def test_a_page_that_does_not_parse_loses_to_one_that_does():
    broken = '{% extends "base.html" %}{% block content %}<div><p>oops{% endblock %}'
    assert (
        score_candidate(broken, "templates/x.html", KNOWN).points
        < score_candidate(GOOD_PAGE, "templates/x.html", KNOWN).points
    )


def test_a_dead_cdn_link_costs_points():
    """Offline, a CDN <link> is a dead DNS lookup and then an unstyled page."""
    with_cdn = GOOD_PAGE.replace(
        "<h1>", '<link rel="stylesheet" href="https://cdn.example.com/x.css"><h1>'
    )
    assert (
        score_candidate(with_cdn, "templates/x.html", KNOWN).points
        < score_candidate(GOOD_PAGE, "templates/x.html", KNOWN).points
    )


def test_an_endpoint_that_does_not_exist_costs_points():
    """W2's BuildError, scored: `url_for('product_list')` against a view named
    `products` is a 500 on that page."""
    bad = GOOD_PAGE.replace("url_for('products')", "url_for('product_list')")
    scored = score_candidate(bad, "templates/x.html", KNOWN)
    assert scored.points < score_candidate(GOOD_PAGE, "templates/x.html", KNOWN).points
    assert "product_list" in " ".join(scored.reasons)


def test_the_endpoint_check_is_skipped_when_the_routes_are_unknown():
    """An unknown endpoint set would make every candidate look broken, which
    turns the choice into noise. Skipped, not guessed."""
    bad = GOOD_PAGE.replace("url_for('products')", "url_for('whatever')")
    assert (
        score_candidate(bad, "templates/x.html", None).points
        == score_candidate(GOOD_PAGE, "templates/x.html", None).points
    )


def test_an_upload_form_without_enctype_costs_points():
    form = (
        '{% extends "base.html" %}{% block content %}'
        '<form method="post"><input type="file" name="cover"></form>{% endblock %}'
    )
    assert "enctype" in " ".join(score_candidate(form, "templates/x.html").reasons)


def test_a_page_that_ships_its_own_html_document_scores_lower():
    """The invariant `convert_to_child_template` otherwise has to repair."""
    doc = "<!doctype html><html><body><h1>Products</h1></body></html>"
    assert (
        score_candidate(doc, "templates/products.html").points
        < score_candidate(GOOD_PAGE, "templates/products.html").points
    )


def test_the_layout_itself_is_not_asked_to_extend_anything():
    doc = "<!doctype html><html><body>{% block content %}{% endblock %}</body></html>"
    assert "extend" not in " ".join(score_candidate(doc, "templates/base.html").reasons)


def test_a_duplicate_definition_costs_points():
    """The later one silently wins — measured live on a db.py with two init_db."""
    twice = "def init_db():\n    pass\n\n\ndef init_db():\n    pass\n"
    once = "def init_db():\n    pass\n"
    assert (
        score_candidate(twice, "db.py").points < score_candidate(once, "db.py").points
    )


def test_an_empty_candidate_is_worthless():
    assert score_candidate("", "app.py").points < 0
    assert score_candidate("   \n", "app.py").points < 0


# --- picking -----------------------------------------------------------------


def test_the_better_candidate_wins():
    broken = "{% block content %}<div>{% endblock %}"
    best, scores = pick_best(
        [("x.html", broken), ("x.html", GOOD_PAGE)], "templates/x.html", KNOWN
    )
    assert best == 1 and len(scores) == 2


def test_a_tie_goes_to_the_first_candidate():
    """No coin flips: N>1 must never make a build merely DIFFERENT."""
    best, _ = pick_best(
        [("x.html", GOOD_PAGE), ("x.html", GOOD_PAGE)], "templates/x.html", KNOWN
    )
    assert best == 0


def test_one_candidate_needs_no_special_case():
    assert pick_best([("x.html", GOOD_PAGE)], "templates/x.html")[0] == 0
    assert pick_best([], "templates/x.html")[0] == 0


def test_the_choice_is_reported_only_when_there_was_one():
    assert describe_choice(0, [Score(100)]) == ""
    line = describe_choice(1, [Score(88), Score(108)])
    assert "2 candidates" in line and "#2" in line


# --- the generation loop ----------------------------------------------------


async def test_best_of_one_is_exactly_todays_behaviour(tmp_path, monkeypatch):
    """The default. One call, one file, no note."""
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_w9_default")
    a._llm_direct = ScriptedLLM(["FILENAME: app.py\nprint('hi')\n"])
    a._llm_sample = ScriptedLLM(["FILENAME: app.py\nprint('other')\n"])

    assert settings.best_of_n == 1
    answer, _ = await a._file_op_flow("make me an app.py that prints hi")

    assert a._llm_sample.calls == 0
    assert "candidates" not in answer
    assert (tmp_path / "app.py").read_text(encoding="utf-8").strip() == "print('hi')"


async def test_a_second_sample_can_win(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "best_of_n", 2)
    a = AgentCore(session_id="pytest_w9_win")
    # #1 does not parse; #2 does.
    a._llm_direct = ScriptedLLM(["FILENAME: app.py\ndef broken(:\n"])
    a._llm_sample = ScriptedLLM(["FILENAME: app.py\ndef fine():\n    pass\n"])

    answer, _ = await a._file_op_flow("write app.py")

    assert a._llm_sample.calls == 1
    assert "def fine():" in (tmp_path / "app.py").read_text(encoding="utf-8")
    assert "kept #2" in answer


async def test_a_worse_second_sample_is_discarded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "best_of_n", 2)
    a = AgentCore(session_id="pytest_w9_lose")
    a._llm_direct = ScriptedLLM(["FILENAME: app.py\ndef fine():\n    pass\n"])
    a._llm_sample = ScriptedLLM(["FILENAME: app.py\ndef broken(:\n"])

    answer, _ = await a._file_op_flow("write app.py")

    assert "def fine():" in (tmp_path / "app.py").read_text(encoding="utf-8")
    assert "kept the first" in answer


async def test_a_cheap_file_is_never_sampled_twice(tmp_path, monkeypatch):
    """Doubling the latency of every write for a README is a bad trade nobody
    asked for."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "best_of_n", 3)
    a = AgentCore(session_id="pytest_w9_cheap")
    a._llm_direct = ScriptedLLM(["FILENAME: notes.md\n# Notes\n"])
    a._llm_sample = ScriptedLLM(["FILENAME: notes.md\n# Other\n"])

    await a._file_op_flow("write notes.md with some notes")
    assert a._llm_sample.calls == 0


async def test_streaming_never_races_candidates(tmp_path, monkeypatch):
    """The user is watching candidate #1's tokens; shipping #2 would be a lie."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "best_of_n", 2)
    a = AgentCore(session_id="pytest_w9_stream")
    a._llm_sample = ScriptedLLM(["FILENAME: app.py\nprint('other')\n"])

    class _Stream:
        async def astream(self, messages):
            for piece in ("FILENAME: app.py\n", "print('streamed')\n"):
                yield SimpleNamespace(content=piece)

    a._llm_stream = _Stream()
    await a._file_op_flow("write app.py", on_token=lambda t: None)

    assert a._llm_sample.calls == 0
    assert "streamed" in (tmp_path / "app.py").read_text(encoding="utf-8")


async def test_a_failed_extra_sample_is_not_fatal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "best_of_n", 2)
    a = AgentCore(session_id="pytest_w9_boom")
    a._llm_direct = ScriptedLLM(["FILENAME: app.py\nprint('hi')\n"])

    class Boom:
        def invoke(self, messages):
            raise RuntimeError("ollama died")

    a._llm_sample = Boom()
    answer, _ = await a._file_op_flow("write app.py")
    assert (tmp_path / "app.py").read_text(encoding="utf-8").strip() == "print('hi')"
    assert "candidates" not in answer


# --- roles per model ---------------------------------------------------------


def test_by_default_a_role_model_is_the_general_one(monkeypatch):
    """Empty setting = the SAME OBJECT, so `/model` and every test that patches
    `_llm_blueprint` or `_llm_edit` keep working."""
    a = AgentCore(session_id="pytest_w9_roles_off")
    assert a._llm_planner is a._llm_blueprint
    assert a._llm_judge is a._llm_edit


def test_patching_the_general_model_still_reaches_the_role(monkeypatch):
    """Why they are properties and not attributes."""
    a = AgentCore(session_id="pytest_w9_roles_patch")
    a._llm_edit = ScriptedLLM(["PASS"])
    assert a._llm_judge is a._llm_edit


def test_a_named_planner_model_is_used_for_planning(monkeypatch):
    monkeypatch.setattr(settings, "planner_model", "some-instruct-model")
    a = AgentCore(session_id="pytest_w9_roles_on")
    assert a._llm_planner is not a._llm_blueprint
    assert a._llm_planner.model == "some-instruct-model"
    # Codegen is untouched: the role is for reasoning calls only.
    assert a._llm_direct.model == settings.llm_model


def test_get_llm_defaults_to_the_configured_model():
    assert get_llm().model == settings.llm_model
    assert get_llm(model="other:7b").model == "other:7b"


# --- the schema cache --------------------------------------------------------


async def test_the_schema_call_is_cached_per_session(monkeypatch):
    """`/plan` previews an amendment with the same message the build then uses,
    and the call is temperature 0 — so the second one buys nothing."""
    a = AgentCore(session_id="pytest_w9_schema_cache")
    a._llm_blueprint = ScriptedLLM(
        [
            '{"entities": [{"name": "product", "table": "products", '
            '"fields": [{"name": "title", "type": "TEXT"}]}]}'
        ]
    )

    first = await a._extract_schema("build me a shop for books")
    second = await a._extract_schema("Build me a shop for books")  # same, recased

    assert first and first == second
    assert a._llm_blueprint.calls == 1


async def test_a_failed_schema_call_is_not_cached(monkeypatch):
    """A transient failure is not an answer."""
    a = AgentCore(session_id="pytest_w9_schema_nocache")
    a._llm_blueprint = ScriptedLLM(
        [
            "not json at all",
            '{"entities": [{"name": "p", "table": "ps", '
            '"fields": [{"name": "title", "type": "TEXT"}]}]}',
        ]
    )
    assert await a._extract_schema("build me a shop") == ()
    assert await a._extract_schema("build me a shop") != ()
    assert a._llm_blueprint.calls == 2
