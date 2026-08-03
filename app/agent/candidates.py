"""Generate a file more than once, and keep the best one (Phase W9).

The honest position this phase starts from: offline, on a 7B, output quality has
a ceiling that no prompt reaches past. The one lever left is sampling — generate
the same file twice at a temperature that makes the samples differ, then let the
**checks** pick the winner.

That is only sound because W2/W5/W6 made the checks objective. Before them, a
"best of N" would have been a coin flip dressed up as a measurement; every
signal scored here is one an existing deterministic pass already trusts:

  * it parses, and it is the right KIND of content (`verify.check_text`);
  * it references no off-machine asset (`strip_external_assets` — offline, a CDN
    link is a dead DNS lookup and then an unstyled page);
  * every `url_for` it uses names a real view (`unresolved_endpoints` — W2's
    BuildError, which is a 500 on that page);
  * an upload form declares its enctype (`forms_missing_enctype`);
  * a page template extends the layout instead of shipping its own `<html>`
    (the invariant `convert_to_child_template` otherwise has to repair);
  * Python: no top-level definition written twice (`pyimports` — the later one
    silently wins, which `compile()` cannot see).

Deliberately NOT scored: `unresolved_local_calls`, which needs every sibling
module's source and is right only at the end of the turn — `app.py` legitimately
calls a `models.py` helper that this build writes two files later, so scoring it
per candidate would punish the correct answer. `_check_cross_module_calls` still
owns that question, once, where it can be answered.

**Ties go to the first candidate.** No coin flips: with nothing to separate two
samples, the one the model produced first is the one a single-sample run would
have shipped, so N>1 can never make a build *different* without making it
measurably better.

Pure and offline: scoring takes strings. The generation loop lives in
`AgentCore`, like every other model call.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.agent.pyimports import duplicate_definitions
from app.agent.verify import (
    check_text,
    find_external_assets,
    forms_missing_enctype,
    unresolved_endpoints,
)

logger = logging.getLogger(__name__)

# A file worth paying for twice. The rule is "does a defect here cost the whole
# page or the whole app" — a README or a .gitignore does not, and doubling the
# latency of every write for one would be a bad trade the user did not ask for.
_HIGH_VALUE_SUFFIXES = (".html", ".htm", ".py")

# Points for the things that must be true. `parses` dwarfs the rest on purpose:
# a candidate that does not parse is not a candidate, whatever else it gets
# right, and this ordering is what makes the total comparable at a glance.
_PARSES = 100
_EXTERNAL_ASSET = -12
_UNRESOLVED_ENDPOINT = -12
_MISSING_ENCTYPE = -6
_EXTENDS_LAYOUT = 8
_UNRESOLVED_CALL = -8
_DUPLICATE_DEF = -8
_EMPTY = -50

_EXTENDS_RE = re.compile(r"{%-?\s*extends\s")


@dataclass(frozen=True)
class Score:
    """What one candidate is worth, and why. The reasons are for the answer."""

    points: int = 0
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def line(self) -> str:
        return f"{self.points:+d}" + (
            f" ({'; '.join(self.reasons)})" if self.reasons else ""
        )


def is_high_value(filename: str) -> bool:
    """Is this a file worth generating more than once?

    Page templates and Python modules: one bad line in either takes down a page
    or the whole app. `templates/base.html` is included — it is the file every
    other page inherits from.
    """
    name = (filename or "").replace("\\", "/").lower()
    return name.endswith(_HIGH_VALUE_SUFFIXES)


def score_candidate(
    text: str, filename: str, endpoints: set[str] | frozenset[str] | None = None
) -> Score:
    """Score one candidate file. Higher is better; nothing here calls an LLM.

    ``endpoints`` are the view names defined in `app.py` (from
    `projectspec.routes_from_source`). Omitted, the `url_for` check is skipped
    entirely rather than guessed at — an unknown endpoint set would make every
    candidate look broken and turn the choice into noise.
    """
    name = (filename or "").replace("\\", "/")
    suffix = Path(name).suffix.lower()
    body = text or ""
    points = 0
    reasons: list[str] = []

    if not body.strip():
        return Score(_EMPTY, ("empty",))

    ok, error = check_text(body, suffix, Path(name).name or "candidate")
    if ok:
        points += _PARSES
    else:
        reasons.append(error.split(":", 1)[-1].strip()[:60] or "does not parse")

    external = find_external_assets(body, suffix)
    if external:
        points += _EXTERNAL_ASSET * len(external)
        reasons.append(f"{len(external)} off-machine asset(s)")

    if suffix in (".html", ".htm"):
        if endpoints is not None:
            missing = unresolved_endpoints(body, endpoints)
            if missing:
                points += _UNRESOLVED_ENDPOINT * len(missing)
                reasons.append("url_for -> " + ", ".join(missing[:3]))
        broken_forms = forms_missing_enctype(body)
        if broken_forms:
            points += _MISSING_ENCTYPE * len(broken_forms)
            reasons.append(f"{len(broken_forms)} upload form(s) without enctype")
        if _is_page_template(name):
            if _EXTENDS_RE.search(body):
                points += _EXTENDS_LAYOUT
            else:
                reasons.append("does not extend the layout")

    if suffix == ".py" and ok:
        try:
            duplicates = duplicate_definitions(body)
            if duplicates:
                points += _DUPLICATE_DEF * len(duplicates)
                reasons.append(f"{len(duplicates)} duplicate definition(s)")
        except Exception:
            logger.debug("duplicate scan failed for %s", name, exc_info=True)

    return Score(points, tuple(reasons))


def _is_page_template(name: str) -> bool:
    lower = name.lower()
    if not lower.endswith((".html", ".htm")):
        return False
    if Path(lower).name in ("base.html", "layout.html", "_macros.html"):
        return False
    return lower.startswith("templates/") or "/templates/" in lower


def pick_best(
    candidates: list[tuple[str, str]],
    filename: str,
    endpoints: set[str] | frozenset[str] | None = None,
) -> tuple[int, list[Score]]:
    """`(winning index, every score)`. Index 0 wins every tie.

    Returns 0 for an empty or single-element list, so the caller needs no
    special case for the default N=1.
    """
    scores = [score_candidate(text, filename, endpoints) for _name, text in candidates]
    if len(scores) < 2:
        return 0, scores
    best = 0
    for index in range(1, len(scores)):
        if scores[index].points > scores[best].points:
            best = index
    return best, scores


def describe_choice(best: int, scores: list[Score]) -> str:
    """The one line the answer gets when more than one candidate was generated.

    Says what it cost and what it bought, because a silent doubling of latency
    is exactly the kind of thing a user should be able to see and turn off.
    """
    if len(scores) < 2:
        return ""
    detail = ", ".join(
        f"#{i + 1} {s.points:+d}" + ("*" if i == best else "")
        for i, s in enumerate(scores)
    )
    if scores[best].points == scores[0].points and best == 0:
        return f"generated {len(scores)} candidates, kept the first ({detail})"
    return f"generated {len(scores)} candidates, kept #{best + 1} ({detail})"
