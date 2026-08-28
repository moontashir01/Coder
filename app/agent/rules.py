"""Behaviour rules: classifying them, and stating them to the model.

`ProjectSpec.Rule` is where a requirement that is not a table finally has
somewhere to live. This module is the half that decides what can be *done* with
one:

* `classify_rule` labels a rule with the shape of a live probe that could
  exercise it, or `""` for the many rules that are prose and nothing more;
* `rules_from_data` validates the schema call's `rules` array;
* `to_context_block` states them for a generation prompt.

Everything here is pure — no LLM, no database, no filesystem — so the whole of
it is unit-testable, and `smoke.py` can ask "which rules can I check?" without
importing anything that talks to a server.

**The classifier admits, it does not interpret.** A label is only assigned when
the wording is unambiguous *and* the entity carries the columns the probe would
need. Everything else is `""`, which costs nothing: the rule is still printed
into every prompt, still stored, still shown by `/spec` — it simply has no
automatic check behind it. Guessing a label would be worse than having none,
because a probe aimed at the wrong rule reports a failure on correct code, and
`_smoke_repair_instruction` would send the model to rewrite it.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.agent.projectspec import Entity, Rule

# `KIND_MIN_INCREMENT` — a new value must clear the stored one by a step.
#   "a bid must be at least the current highest bid plus the increment"
KIND_MIN_INCREMENT = "min_increment"
# `KIND_EXTEND_DEADLINE` — an action close to a deadline pushes the deadline out.
#   "a bid in the final 3 minutes extends the auction by 3 minutes"
KIND_EXTEND_DEADLINE = "extend_deadline"

# Both patterns need TWO signals, not one. "increment" alone appears in any
# sentence about bidding; "extend" alone appears in "extend the description".
_INCREMENT_RE = re.compile(r"\bincrement\b|\bstep\b", re.I)
_MINIMUM_RE = re.compile(
    r"\b(at least|minimum|min|higher than|greater than|exceed|above|reject|refuse|"
    r"must beat|not accept)\b",
    re.I,
)
_EXTEND_RE = re.compile(r"\b(extend|extends|extended|push|prolong|postpone)\b", re.I)
_DEADLINE_RE = re.compile(
    r"\b(end|ends|ending|close|closes|closing|expiry|expires|deadline|final)\b", re.I
)

# The columns each probe reads. A rule whose entity does not have them cannot be
# checked, whatever it says — so it is not labelled, and the label is therefore
# always a promise the probe can keep.
_INCREMENT_COLUMNS = ("increment", "step")
_AMOUNT_COLUMNS = ("amount", "bid", "price", "value")
_DEADLINE_COLUMNS = ("end_time", "ends_at", "end_at", "expires_at", "closes_at")


def _columns(entity: "Entity | None") -> list[str]:
    return [f.name.lower() for f in (entity.fields if entity else ())]


def _has(columns: Iterable[str], parts: Iterable[str]) -> str:
    """The first column whose name contains any of ``parts``, or ""."""
    for column in columns:
        if any(part in column for part in parts):
            return column
    return ""


def classify_rule(rule: "Rule", entity: "Entity | None" = None) -> str:
    """The probe shape this rule can be checked with, or "".

    ``entity`` is the table the rule names, when the caller has it. Without one
    the wording alone decides — which is right for `_load_rules`, where a spec is
    being read back and the entity may be loaded after the rule.
    """
    text = f"{rule.trigger} {rule.effect}"
    columns = _columns(entity)

    if _INCREMENT_RE.search(text) and _MINIMUM_RE.search(text):
        if not entity or (
            _has(columns, _INCREMENT_COLUMNS) and _has(columns, _AMOUNT_COLUMNS)
        ):
            return KIND_MIN_INCREMENT

    if _EXTEND_RE.search(text) and _DEADLINE_RE.search(text):
        if not entity or _has(columns, _DEADLINE_COLUMNS):
            return KIND_EXTEND_DEADLINE

    return ""


def deadline_column(entity: "Entity | None") -> str:
    """The column a deadline rule moves, or ""."""
    return _has(_columns(entity), _DEADLINE_COLUMNS)


def increment_column(entity: "Entity | None") -> str:
    """The column holding the step a new value must clear, or ""."""
    return _has(_columns(entity), _INCREMENT_COLUMNS)


def amount_column(entity: "Entity | None") -> str:
    """The column holding the current value, or ""."""
    columns = _columns(entity)
    # Prefer one that says it is the CURRENT/HIGHEST value over any money column
    # on the row: `fixed_price` is not what a bid has to beat.
    for column in columns:
        if ("current" in column or "highest" in column) and _has(
            [column], _AMOUNT_COLUMNS
        ):
            return column
    return _has(columns, _AMOUNT_COLUMNS)


def rules_from_data(data: dict | None, entities: tuple = ()) -> tuple:
    """The schema call's `rules` array, validated (see `_load_rules`).

    Lives here rather than in `projectspec` because classification needs the
    entity, and this is the module that knows how to classify.
    """
    from app.agent.projectspec import MAX_RULES, Rule, _ident

    by_name = {}
    for entity in entities:
        by_name[entity.name.lower()] = entity
        by_name[entity.table.lower()] = entity

    out: list[Rule] = []
    for item in (data or {}).get("rules") or []:
        if not isinstance(item, dict):
            continue
        trigger = " ".join(str(item.get("trigger") or "").split())[:200]
        effect = " ".join(str(item.get("effect") or "").split())[:200]
        if not trigger or not effect:
            continue
        name = _ident(item.get("entity") or item.get("table") or "")
        rule = Rule(entity=name, trigger=trigger, effect=effect)
        out.append(
            Rule(
                entity=name,
                trigger=trigger,
                effect=effect,
                kind=classify_rule(rule, by_name.get(name.lower())),
            )
        )
        if len(out) >= MAX_RULES:
            break
    return tuple(out)


def to_context_block(rules: tuple) -> str:
    """State the rules for a generation prompt, or "" when there are none.

    Deliberately imperative and deliberately short. The failure this exists to
    prevent is not the model misunderstanding a rule — it is the model never
    being told one, which is what happened to every behaviour in the OpenBazaar
    PRD.
    """
    if not rules:
        return ""
    lines = "\n".join(f"- {r.summary()}" for r in rules)
    return (
        "## Rules this app must ENFORCE\n"
        "These are behaviours, not tables. A route that stores the row without "
        "applying the rule is not this feature — it is the feature missing, and "
        "it will be reported as such.\n" + lines
    )
