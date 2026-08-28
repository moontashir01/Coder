"""Behaviour rules: the spec can hold one, the probe can check one.

The gap these close was measured end to end on the OpenBazaar build. The PRD's
five tables survived turn 1 perfectly, because a table is representable and
`_extract_schema` had somewhere to put it. "If a bid is registered within the
final 3 minutes, extend the auction by 3 minutes" had nowhere to go: it was
dropped at the first stage, no later prompt mentioned it, no check could fail on
it, and the finished site had every auction column and no auction.

Offline throughout — the classifier and the loaders are pure, and the probe is
driven with a fake `run_sql` and a fake HTTP poster.
"""

from __future__ import annotations

import pytest

from app.agent import rules as R
from app.agent import smoke
from app.agent.projectspec import Entity, Field, ProjectSpec, Rule, SpecEndpoint

ITEMS = Entity(
    name="item",
    table="items",
    fields=(
        Field(name="id", type="TEXT", pk=True),
        Field(name="title", type="TEXT"),
        Field(name="fixed_price", type="NUMERIC"),
        Field(name="current_highest_bid", type="NUMERIC"),
        Field(name="bid_increment_step", type="NUMERIC"),
        Field(name="auction_end_time", type="TIMESTAMP"),
    ),
)
BIDS = Entity(
    name="bid",
    table="bids",
    fields=(
        Field(name="id", type="TEXT", pk=True),
        Field(name="item_id", type="TEXT", references="items(id)"),
        Field(name="bid_amount", type="NUMERIC"),
    ),
)

MIN_RULE = Rule(
    entity="items",
    trigger="a bid is placed",
    effect="it must be at least the current highest bid plus the bid increment step",
)
LATE_RULE = Rule(
    entity="items",
    trigger="a bid arrives in the final 3 minutes",
    effect="the auction end time is extended by 3 minutes",
)


def _spec(rules=()) -> ProjectSpec:
    return ProjectSpec(
        name="Market",
        entities=(ITEMS, BIDS),
        rules=tuple(rules),
        endpoints=(SpecEndpoint(method="POST", path="/bids/new", entity="bid"),),
    )


# ---------------------------------------------------------------------------
# Classification — it admits, it does not interpret
# ---------------------------------------------------------------------------


def test_a_minimum_increment_rule_is_recognised():
    assert R.classify_rule(MIN_RULE, ITEMS) == R.KIND_MIN_INCREMENT


def test_a_deadline_extension_rule_is_recognised():
    assert R.classify_rule(LATE_RULE, ITEMS) == R.KIND_EXTEND_DEADLINE


def test_a_rule_the_probe_cannot_express_is_left_unlabelled():
    """Most rules are prose to the model and nothing more. That is a fine
    outcome — they are still stored and still printed into every prompt — and it
    is much better than a label whose probe would then fail on correct code."""
    score = Rule(
        entity="users",
        trigger="a delivery is refused at the door",
        effect="the buyer's reliability score drops by 25",
    )
    assert R.classify_rule(score, None) == ""


def test_wording_alone_is_not_enough_when_the_columns_are_absent():
    """A label is a promise the probe can keep. Against a table with no
    increment and no amount, it cannot be kept."""
    plain = Entity(
        name="post",
        table="posts",
        fields=(Field(name="id", type="TEXT", pk=True), Field(name="body")),
    )
    assert R.classify_rule(MIN_RULE, plain) == ""


def test_the_amount_column_prefers_the_current_value_over_any_price():
    """`fixed_price` is money on the row, but it is not what a bid must beat."""
    assert R.amount_column(ITEMS) == "current_highest_bid"
    assert R.increment_column(ITEMS) == "bid_increment_step"
    assert R.deadline_column(ITEMS) == "auction_end_time"


# ---------------------------------------------------------------------------
# The spec holds them, and states them
# ---------------------------------------------------------------------------


def test_rules_survive_a_save_and_load_round_trip():
    spec = _spec([MIN_RULE, LATE_RULE])
    back = ProjectSpec.from_dict(spec.to_dict())
    assert [r.trigger for r in back.rules] == [MIN_RULE.trigger, LATE_RULE.trigger]
    assert back.rules[0].kind == R.KIND_MIN_INCREMENT


def test_a_rule_missing_its_effect_is_dropped():
    """A half-remembered requirement is worse than an absent one: it reads as
    coverage."""
    data = {"rules": [{"entity": "items", "trigger": "a bid is placed"}]}
    assert R.rules_from_data(data, (ITEMS,)) == ()


def test_the_context_block_states_the_rules_above_the_routes():
    """The budget drops sections from the BOTTOM, and a rule is the one thing
    here nothing else remembers — a route can be re-read off the entry file, a
    page off the template directory."""
    spec = _spec([LATE_RULE])
    block = spec.to_context_block()
    assert "must ENFORCE" in block
    assert "extended by 3 minutes" in block
    assert block.index("ENFORCE") < len(block)


def test_the_schema_prompt_asks_for_rules():
    """A field nothing populates is a field that does not exist."""
    from config.settings import settings

    prompt = (settings.prompts_dir / "schema.md").read_text(encoding="utf-8")
    assert '"rules"' in prompt
    assert "trigger" in prompt and "effect" in prompt


# ---------------------------------------------------------------------------
# The probe — a rule that is not enforced fails, and only then
# ---------------------------------------------------------------------------


class FakeAdapter:
    """A `run_sql` over an in-memory row set."""

    def __init__(self, rows: dict, bid_count: list[int]):
        self.rows = rows
        self.bid_count = bid_count
        self.statements: list[str] = []

    def run_sql(self, root, sql, params=None):
        self.statements.append(sql)
        low = sql.lower()
        if "count(*)" in low:
            return [{"n": self.bid_count[0]}]
        if low.startswith("update"):
            self.rows["left_s"] = 60
            return [{"id": "i1"}]
        if "left_s" in low:
            return [{"left_s": self.rows["left_s"]}]
        return [dict(self.rows)]


@pytest.fixture
def posted(monkeypatch):
    """Capture what the probe POSTs, and control what the app answers."""
    calls: list[tuple] = []
    plan = {"status": 302}

    def _fake_request(port, method, path, timeout=4.0, body=None, content_type=""):
        calls.append((method, path, body))
        return plan["status"], ""

    monkeypatch.setattr(smoke, "_request", _fake_request)
    return calls, plan


def test_a_bid_that_does_not_clear_the_increment_and_is_stored_FAILS(posted):
    """The exact defect: the handler answers 302 and writes the row. Every check
    that existed passed on this."""
    calls, plan = posted
    adapter = FakeAdapter(
        {"id": "i1", "amount": "100", "step": "5", "left_s": 3600}, [0]
    )
    inner = adapter.run_sql

    def run_sql(root, sql, params=None):
        out = inner(root, sql, params)
        if "count(*)" in sql.lower():
            adapter.bid_count[0] += 1  # the app stored the bid it should refuse
        return out

    adapter.run_sql = run_sql
    checks = smoke.behaviour_probe(_spec([MIN_RULE]), 3000, adapter, root=".")

    assert len(checks) == 1
    assert checks[0].ok is False
    assert "not enforced" in checks[0].detail
    assert calls and calls[0][1] == "/bids/new"


def test_a_bid_that_is_refused_PASSES(posted):
    calls, plan = posted
    plan["status"] = 400
    adapter = FakeAdapter({"id": "i1", "amount": "100", "step": "5"}, [0])
    checks = smoke.behaviour_probe(_spec([MIN_RULE]), 3000, adapter, root=".")
    assert checks[0].ok is True
    assert "refused" in checks[0].detail


def test_a_bid_silently_dropped_also_PASSES(posted):
    """A 302 with no row written is the rule being enforced, just not loudly."""
    adapter = FakeAdapter({"id": "i1", "amount": "100", "step": "5"}, [7])
    checks = smoke.behaviour_probe(_spec([MIN_RULE]), 3000, adapter, root=".")
    assert checks[0].ok is True


def test_a_deadline_that_does_not_move_FAILS(posted):
    adapter = FakeAdapter({"id": "i1", "amount": "100", "step": "5", "left_s": 60}, [0])
    checks = smoke.behaviour_probe(_spec([LATE_RULE]), 3000, adapter, root=".")
    assert checks[0].ok is False
    assert "not enforced" in checks[0].detail


def test_a_deadline_that_moves_PASSES(posted):
    adapter = FakeAdapter({"id": "i1", "amount": "100", "step": "5", "left_s": 60}, [0])
    real = adapter.run_sql

    def run_sql(root, sql, params=None):
        if "left_s" in sql.lower():
            return [{"left_s": 240}]  # the app extended it
        return real(root, sql, params)

    adapter.run_sql = run_sql
    checks = smoke.behaviour_probe(_spec([LATE_RULE]), 3000, adapter, root=".")
    assert checks[0].ok is True
    assert "extended" in checks[0].detail


def test_no_database_access_is_a_STATED_SKIP_never_a_failure(posted):
    """`functional_probe` step 3's lesson: a false failure sends the repair loop
    at code that works."""
    checks = smoke.behaviour_probe(_spec([MIN_RULE]), 3000, adapter=None, root=None)
    assert checks[0].ok is True
    assert "not probed" in checks[0].detail


def test_an_unreadable_table_is_a_stated_skip(posted):
    class Empty:
        def run_sql(self, root, sql, params=None):
            return None

    checks = smoke.behaviour_probe(_spec([MIN_RULE]), 3000, Empty(), root=".")
    assert checks[0].ok is True
    assert "not probed" in checks[0].detail


def test_a_spec_with_no_rules_probes_nothing(posted):
    assert smoke.behaviour_probe(_spec(), 3000, FakeAdapter({}, [0]), root=".") == []


def test_a_route_that_CRASHES_is_not_counted_as_enforcement(posted):
    """A 500 is not a refusal. `functional_probe` reports the crash already;
    counting it here would hide a broken handler behind a green check — the
    most expensive kind of false pass."""
    calls, plan = posted
    plan["status"] = 500
    adapter = FakeAdapter({"id": "i1", "amount": "100", "step": "5"}, [0])
    checks = smoke.behaviour_probe(_spec([MIN_RULE]), 3000, adapter, root=".")
    assert checks[0].ok is True
    assert "not probed" in checks[0].detail and "errored" in checks[0].detail
