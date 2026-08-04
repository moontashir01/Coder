"""A `@`-referenced requirements document reaches the stages that build from it.

"Build the website described in @PRD.md" is the shape a real spec arrives in,
and before this the document was read into `_multi_file_flow`'s `context` and
then dropped: its only consumer is `_plan_file_ops`, which `_run_blueprint`
SKIPS because it preplans the ops. So the two calls that decide what the app IS
— `_extract_schema` and `_expand_requirements` — planned a whole build from the
one sentence that names the file.

All offline: the LLM-calling stages are scripted or monkeypatched, and the
document is a real file in `tmp_path`.
"""

from types import SimpleNamespace

import pytest

from app.agent.blueprint import (
    ApiContract,
    Blueprint,
    Endpoint,
    Feature,
    PlannedFile,
)
from app.agent.core import AgentCore
from config.settings import settings

pytestmark = pytest.mark.asyncio


class ScriptedLLM:
    """Records what it was asked, answers from a fixed script.

    `prompts` holds the HUMAN message only. The system prompts now *describe*
    a requirements document, so asserting against the whole conversation would
    pass on the rule rather than on the document being there.
    """

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.prompts: list[str] = []

    def invoke(self, messages):
        self.prompts.append(str(messages[-1].content))
        out = self._outputs[min(len(self.prompts) - 1, len(self._outputs) - 1)]
        return SimpleNamespace(content=out)


PRD = """# Auction marketplace

Sellers list items. Buyers place bids. Every order is cash on delivery and must
pass an SMS OTP check before the seller dispatches it. A buyer who refuses a
delivery at the door loses 25% of their reliability score.
"""


def _write_prd(tmp_path, text: str = PRD, name: str = "PRD.md"):
    (tmp_path / name).write_text(text, encoding="utf-8")
    return name


def _actionable_blueprint() -> Blueprint:
    return Blueprint(
        summary="an auction marketplace",
        features=(Feature(name="Bidding", tier="requested", files=("app.py",)),),
        files=(PlannedFile("app.py"), PlannedFile("templates/items.html")),
        contract=ApiContract(endpoints=(Endpoint("POST", "/bids", entity="bid"),)),
    )


# ---------------------------------------------------------------------------
# _requirements_doc_context — what counts as a document, and what it says
# ---------------------------------------------------------------------------


async def test_prose_ref_becomes_a_requirements_block(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_prd(tmp_path)
    a = AgentCore(session_id="pytest_doc_read")

    block = a._requirements_doc_context(["PRD.md"])

    assert "Requirements document" in block
    assert "reliability score" in block  # the document's own text is quoted


async def test_a_source_file_ref_is_not_a_requirements_document(tmp_path, monkeypatch):
    """`@app.py` on a build request is code to work from. Quoting it to the
    schema call as requirements would model the code instead of the product."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "app.py").write_text("print('hello')", encoding="utf-8")
    a = AgentCore(session_id="pytest_doc_code")

    assert a._requirements_doc_context(["app.py"]) == ""


async def test_missing_empty_and_unreadable_refs_yield_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "empty.md").write_text("   \n", encoding="utf-8")
    a = AgentCore(session_id="pytest_doc_absent")

    assert a._requirements_doc_context(["nope.md"]) == ""
    assert a._requirements_doc_context(["empty.md"]) == ""
    assert a._requirements_doc_context([]) == ""


async def test_truncation_is_stated_not_silent(tmp_path, monkeypatch):
    """A silently halved PRD is a requirement the build will not have and
    nobody will know is missing."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "max_spec_doc_chars", 200)
    (tmp_path / "PRD.md").write_text("x" * 5000, encoding="utf-8")
    a = AgentCore(session_id="pytest_doc_trunc")

    block = a._requirements_doc_context(["PRD.md"])

    assert "[TRUNCATED: 200 of 5000 characters shown." in block
    assert block.count("x" * 10) == 20  # 200 chars of body, and no more


async def test_zero_budget_disables_the_feature(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "max_spec_doc_chars", 0)
    _write_prd(tmp_path)
    a = AgentCore(session_id="pytest_doc_off")

    assert a._requirements_doc_context(["PRD.md"]) == ""


async def test_the_budget_is_total_across_documents(tmp_path, monkeypatch):
    """A per-file cap is how several referenced documents overflow the context
    window and evict the block they were meant to add (`_sibling_context`)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "max_spec_doc_chars", 300)
    (tmp_path / "one.md").write_text("a" * 400, encoding="utf-8")
    (tmp_path / "two.md").write_text("b" * 400, encoding="utf-8")
    a = AgentCore(session_id="pytest_doc_budget")

    block = a._requirements_doc_context(["one.md", "two.md"])

    assert "### one.md" in block
    assert "### two.md" not in block  # the first document spent the budget
    assert block.count("a" * 10) == 30  # 300 chars of it, and no more


# ---------------------------------------------------------------------------
# The stages that decide what the app IS must see it
# ---------------------------------------------------------------------------


async def test_schema_call_is_given_the_document(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_prd(tmp_path)
    a = AgentCore(session_id="pytest_doc_schema")
    llm = ScriptedLLM(['{"summary": "s", "entities": []}'])
    monkeypatch.setattr(AgentCore, "_llm_planner", property(lambda self: llm))

    a._spec_doc = a._requirements_doc_context(["PRD.md"])
    await a._extract_schema("build the website described in PRD.md")

    assert "reliability score" in llm.prompts[0]


async def test_blueprint_call_is_given_the_document(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_prd(tmp_path)
    a = AgentCore(session_id="pytest_doc_expand")
    llm = ScriptedLLM(["not json"])  # parse failure → None, which is fine here
    monkeypatch.setattr(AgentCore, "_llm_planner", property(lambda self: llm))

    a._spec_doc = a._requirements_doc_context(["PRD.md"])
    await a._expand_requirements("build the website described in PRD.md")

    assert "reliability score" in llm.prompts[0]


async def test_schema_cache_keys_on_the_document_too(tmp_path, monkeypatch):
    """Same sentence, different PRD, is a different request — returning the
    first one's tables for the second is the cache answering a question it was
    never asked."""
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_doc_cache")
    llm = ScriptedLLM(
        [
            '{"summary": "s", "entities": [{"name": "item", "table": "items",'
            ' "fields": [{"name": "id", "type": "INTEGER", "pk": true}]}]}',
            '{"summary": "s", "entities": [{"name": "bid", "table": "bids",'
            ' "fields": [{"name": "id", "type": "INTEGER", "pk": true}]}]}',
        ]
    )
    monkeypatch.setattr(AgentCore, "_llm_planner", property(lambda self: llm))

    a._spec_doc = "## Requirements document\nfirst spec"
    first = await a._extract_schema("build it")
    a._spec_doc = "## Requirements document\nsecond spec"
    second = await a._extract_schema("build it")

    assert [e.table for e in first] == ["items"]
    assert [e.table for e in second] == ["bids"]


async def test_no_document_leaves_the_stages_untouched(tmp_path, monkeypatch):
    """The whole feature is inert on a turn that references nothing."""
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_doc_inert")
    llm = ScriptedLLM(['{"summary": "s", "entities": []}'])
    monkeypatch.setattr(AgentCore, "_llm_planner", property(lambda self: llm))

    await a._extract_schema("build me a shop")

    assert a._spec_doc == ""
    # No "Requirements document" section at all — the prompt is the column-type
    # block (which every turn gets, document or not) and the request, nothing
    # more.
    assert "Requirements document" not in llm.prompts[0]
    assert llm.prompts[0].endswith("Request: build me a shop\n\nOutput the JSON now:")


# ---------------------------------------------------------------------------
# The seam: chat() reads it, and _run_blueprint threads it to every file
# ---------------------------------------------------------------------------


async def test_chat_reads_the_document_before_the_blueprint_gate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "expand_requirements", True)
    _write_prd(tmp_path)
    a = AgentCore(session_id="pytest_doc_seam")

    seen = {}

    async def _fake_expand(msg, entities=()):
        seen["doc"] = a._spec_doc
        return _actionable_blueprint()

    async def _fake_run(msg, blueprint, refs):
        return "built", []

    monkeypatch.setattr(a, "_expand_requirements", _fake_expand)
    monkeypatch.setattr(a, "_run_blueprint", _fake_run)

    await a.chat("build the marketplace website described in @PRD.md")

    assert "reliability score" in seen["doc"]


async def test_run_blueprint_threads_the_document_into_every_file(
    tmp_path, monkeypatch
):
    """`_multi_file_flow` reads the @refs itself — but only into `context`,
    which `_plan_file_ops` consumes, and that call is skipped for preplanned
    ops. Without this the document is read and then dropped on exactly the
    path that needs it most."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "expand_requirements", True)
    _write_prd(tmp_path)
    a = AgentCore(session_id="pytest_doc_thread")

    captured = {}

    async def _fake_mff(user_message, refs, extra_context="", preplanned_ops=None):
        captured["extra"] = extra_context
        return "Handled 2 file(s)", []

    async def _fake_expand(msg, entities=()):
        return _actionable_blueprint()

    monkeypatch.setattr(a, "_expand_requirements", _fake_expand)
    monkeypatch.setattr(a, "_multi_file_flow", _fake_mff)

    await a.chat("build the marketplace website described in @PRD.md")

    assert "reliability score" in captured["extra"]


async def test_per_file_threading_uses_the_tighter_budget(tmp_path, monkeypatch):
    """Here the document sits on top of the contract, the scaffold block, the UI
    block, the manifest AND the siblings — and an overflowing prompt evicts the
    siblings, which is `_sibling_context`'s bug arriving by a new road."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "expand_requirements", True)
    monkeypatch.setattr(settings, "max_spec_doc_chars", 5000)
    monkeypatch.setattr(settings, "max_spec_doc_context_chars", 100)
    (tmp_path / "PRD.md").write_text("z" * 5000, encoding="utf-8")
    a = AgentCore(session_id="pytest_doc_tight")

    captured = {}

    async def _fake_mff(user_message, refs, extra_context="", preplanned_ops=None):
        captured["extra"] = extra_context
        return "Handled 2 file(s)", []

    async def _fake_expand(msg, entities=()):
        captured["planning_doc"] = a._spec_doc
        return _actionable_blueprint()

    monkeypatch.setattr(a, "_expand_requirements", _fake_expand)
    monkeypatch.setattr(a, "_multi_file_flow", _fake_mff)

    await a.chat("build the marketplace website described in @PRD.md")

    assert captured["planning_doc"].count("z" * 10) == 500  # planning got 5000
    assert captured["extra"].count("z" * 10) == 10  # per-file got 100


# ---------------------------------------------------------------------------
# ...and it is never the file that gets overwritten
# ---------------------------------------------------------------------------


async def test_a_build_never_targets_the_requirements_document(tmp_path, monkeypatch):
    """The blueprint stage being off (or declining) must not turn the PRD into
    the edit target — `_file_op_flow` sends an existing file to
    `_surgical_edit`, so the one file the user could not regenerate would be
    overwritten with a web page."""
    monkeypatch.chdir(tmp_path)
    _write_prd(tmp_path)
    a = AgentCore(session_id="pytest_doc_target")

    captured = {}

    async def _fake_file_op(msg, target=None, extra_context="", on_token=None):
        captured["target"] = target
        return "wrote", []

    monkeypatch.setattr(a, "_file_op_flow", _fake_file_op)

    await a.chat("build the marketplace website described in @PRD.md")

    assert captured["target"] != "PRD.md"


async def test_an_edit_of_a_document_still_targets_it(tmp_path, monkeypatch):
    """The ordinary case is the opposite: "fix the typo in @README.md" must
    still write to the README."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "README.md").write_text("# Titel\n", encoding="utf-8")
    a = AgentCore(session_id="pytest_doc_edit")

    captured = {}

    async def _fake_file_op(msg, target=None, extra_context="", on_token=None):
        captured["target"] = target
        return "wrote", []

    monkeypatch.setattr(a, "_file_op_flow", _fake_file_op)

    await a.chat("fix the typo in @README.md")

    assert captured["target"] == "README.md"


# ---------------------------------------------------------------------------
# The planning calls are told about THIS stack, not about Flask
# ---------------------------------------------------------------------------


async def test_schema_call_states_the_stack_s_own_column_types(tmp_path, monkeypatch):
    """`prompts/schema.md` used to say "Storage is SQLite" to every stack, so a
    Node build was told to flatten a timestamp to TEXT — and an auction decided
    by `auction_end_time > NOW()` cannot be written against a string."""
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_types_node")
    llm = ScriptedLLM(['{"summary": "s", "entities": []}'])
    monkeypatch.setattr(AgentCore, "_llm_planner", property(lambda self: llm))
    monkeypatch.setattr(settings, "web_stack", "node")

    await a._extract_schema("build an auction site")

    assert "PostgreSQL" in llm.prompts[0]
    assert "TIMESTAMP" in llm.prompts[0]
    assert "Storage is SQLite" not in llm.prompts[0]


async def test_schema_call_still_says_sqlite_on_flask(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_types_flask")
    llm = ScriptedLLM(['{"summary": "s", "entities": []}'])
    monkeypatch.setattr(AgentCore, "_llm_planner", property(lambda self: llm))
    monkeypatch.setattr(settings, "web_stack", "flask")

    await a._extract_schema("build an auction site")

    assert "Storage is SQLite" in llm.prompts[0]


async def test_schema_cache_keys_on_the_stack_too(tmp_path, monkeypatch):
    """The same sentence on a different stack is a different question: the
    answer names types only one of the two databases has."""
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_types_cache")
    llm = ScriptedLLM(
        [
            '{"summary": "s", "entities": [{"name": "item", "table": "items",'
            ' "fields": [{"name": "id", "type": "INTEGER", "pk": true}]}]}',
            '{"summary": "s", "entities": [{"name": "bid", "table": "bids",'
            ' "fields": [{"name": "id", "type": "INTEGER", "pk": true}]}]}',
        ]
    )
    monkeypatch.setattr(AgentCore, "_llm_planner", property(lambda self: llm))

    monkeypatch.setattr(settings, "web_stack", "flask")
    first = await a._extract_schema("build it")
    monkeypatch.setattr(settings, "web_stack", "node")
    second = await a._extract_schema("build it")

    assert [e.table for e in first] == ["items"]
    assert [e.table for e in second] == ["bids"]


async def test_blueprint_call_states_the_stack_s_own_file_layout(tmp_path, monkeypatch):
    """The layout table was hard-coded to Flask in `prompts/blueprint.md`, so an
    Express build was planned as `app.py` and `templates/*.html` — paths this
    stack never renders."""
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_layout_node")
    llm = ScriptedLLM(["not json"])
    monkeypatch.setattr(AgentCore, "_llm_planner", property(lambda self: llm))
    monkeypatch.setattr(settings, "web_stack", "node")

    await a._expand_requirements("build an auction site")

    prompt = llm.prompts[0]
    assert "server.js" in prompt and "views/layout.ejs" in prompt
    assert "templates/base.html" not in prompt
