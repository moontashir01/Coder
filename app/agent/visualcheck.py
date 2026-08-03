"""Ask a vision model what the rendered page LOOKS like (Phase W7).

W5 measures the page and W6 exercises it; both are deterministic, and between
them they catch every defect that can be stated as a number. This one is about
what is left: the page fits in the viewport, the console is clean, every button
works — and it still looks wrong. Text overlapping a card. A heading sitting on
top of an image. A form squeezed into a corner with 800px of white beside it.

**This is the least reliable stage in the pipeline, and it is built to be
reverted.** It reuses `intent.py`'s shape exactly, because it is the same
problem one layer up — a 7B judging a 7B — and the four rules that made intent
repair safe are the same four here:

  * **A checklist, never "critique this".** An open prompt on a 7B VL returns
    "the page has a clean and modern feel", which is not actionable, and then
    "consider adding testimonials", which is a feature request. The prompt names
    five things to look at and forbids everything else.
  * **Unparseable = PASS.** `intent.parse_verdict` is reused verbatim (which is
    why the prompt asks for the literal `MISSING:` marker rather than nicer
    wording — a second parser is a second thing that can read noise as a defect).
  * **Complaints are filtered deterministically.** A complaint must name a
    *rendering* symptom and must not ask for new content. `buildspec._clean_nav`
    resolves the same tension: "add a testimonials section" is a feature
    request, not a defect, and the model was not asked for one.
  * **A rewrite that regresses a measurement is reverted** — by the caller,
    which is the only place that can re-measure. Without that clause this stage
    is a net negative: a visual "fix" that introduces horizontal overflow trades
    a defect nobody measured for one that W5 will report forever.

Pure and offline: prompt construction, parsing and filtering only. The LLM call
lives in `AgentCore`, like every other model call in this codebase.
"""

from __future__ import annotations

import re

from app.agent.intent import parse_verdict

VISUAL_SYSTEM = (
    "You are a UI defect checker. You are shown a screenshot of one page of a "
    "website. You report only visible rendering defects. You never suggest new "
    "content, new features or new pages, and you never give design advice."
)

# The literal `MISSING:` marker is what `intent.parse_verdict` reads. Keeping
# one parser for both stages is worth the slightly odd word: every ambiguity
# already resolves toward PASS there, and that behaviour is what protects a page
# that is fine.
VISUAL_CHECKLIST = """
Answer in EXACTLY one of these two forms, and nothing else:

PASS

or:

MISSING:
- <one visible defect>
- <another one>

Check ONLY these five things, in this order:
1. Is any text cut off, clipped, or running outside the box it sits in?
2. Is any element overlapping or covering another one?
3. Is any text too low-contrast to read against what is behind it?
4. Is the layout obviously broken — content pushed off the right edge, or one
   element stranded alone in a large empty area?
5. Is any image stretched, squashed, or showing as broken?

Rules:
- Report ONLY what you can see in this image. If it looks acceptable, answer PASS.
- Never suggest adding content, sections, features, pages or images.
- Never comment on wording, branding, colour taste, or what the page "should"
  contain. Those are not defects.
- Each line names one visible defect in a few words, and says where it is.
- If you are unsure, answer PASS."""

# A complaint has to name a SYMPTOM the eye can see. Without this gate the stage
# passes through "the layout could be more modern", which no rewrite can satisfy
# and every rewrite will try to.
_RENDER_SYMPTOM_RE = re.compile(
    r"\b(overlap\w*|overlay\w*|cut\s*off|cropp?ed|clip(?:ped|ping)?|truncat\w*|"
    r"unreadable|illegible|invisible|low[-\s]?contrast|contrast|blend\w*|"
    r"off[-\s]?(?:screen|the\s+screen|the\s+page|centre|center)|outside|overflow\w*|"
    r"misalign\w*|unaligned|align\w*|squash\w*|stretch\w*|distort\w*|skew\w*|"
    r"cramped|crowded|squeez\w*|overlapping|hidden|obscur\w*|covered|"
    r"broken\s+image|missing\s+image|blank|empty\s+(?:area|space|gap|page)|"
    r"huge\s+gap|large\s+gap|white\s+space|whitespace|too\s+(?:small|large|wide|"
    r"narrow|close|tall)|wraps?\s+(?:awkwardly|badly)|spills?|extends?\s+(?:past|"
    r"beyond)|touching|no\s+(?:padding|margin|spacing))\b",
    re.IGNORECASE,
)

# "add a testimonials section" is a feature request. `_clean_nav` refuses to let
# the model invent nav items for the same reason: the user asked for a page, not
# for whatever the model would have built.
_INVENTION_RE = re.compile(
    r"\b(add|adding|include|including|introduce|create|provide|insert|need\w*|"
    r"missing\s+(?:a|an|the)\s+\w+\s+(?:section|page|feature|button|link)|"
    r"should\s+(?:have|include|contain)|would\s+benefit|lacks?)\b",
    re.IGNORECASE,
)

# Hedged commentary. Same list `intent._SUGGESTION_RE` uses, for the same reason.
_SUGGESTION_RE = re.compile(
    r"\b(consider|could|might|maybe|perhaps|suggest\w*|recommend\w*|improve\w*|"
    r"enhance\w*|better|nicer|cleaner|modern\w*|professional|optional\w*|"
    r"ideally|appealing|aesthetic\w*|overall)\b",
    re.IGNORECASE,
)

# One page, one screenshot, one answer — a complaint list longer than this is a
# model reviewing rather than checking, and none of it is worth a rewrite.
MAX_VISUAL_COMPLAINTS = 3


def build_visual_prompt(page: str, width: int) -> str:
    """The text half of the vision message. The image is the other half."""
    device = "a phone" if width and width <= 500 else "a desktop browser"
    return (
        f"This is a screenshot of the page `{page or '/'}` as it renders in "
        f"{device} at {width or 1280}px wide.\n"
        f"{VISUAL_CHECKLIST}"
    )


def parse_visual_verdict(raw: str) -> list[str]:
    """The vision model's answer as a complaint list; [] means it looks fine.

    Deliberately `intent.parse_verdict` — the tolerance for unreadable output
    is the feature, not an implementation detail.
    """
    return parse_verdict(raw)


def filter_visual_complaints(complaints: list[str]) -> list[str]:
    """Keep only complaints a rewrite of this page could actually fix.

    Three gates, all biased toward doing nothing:

      * **Names a visible symptom** — otherwise it is taste, and taste is what a
        7B VL produces without limit.
      * **Does not ask for new content** — "add a hero image" is a feature
        request; the page not having one is not a defect.
      * **Is not hedged** — "could be cleaner" is not a defect either.

    The gates will drop real defects phrased unusually. That trade is the same
    one `filter_complaints` makes and for the same reason: a missed complaint
    leaves a page a human can still look at, while a false one rewrites a page
    that was fine.
    """
    kept: list[str] = []
    for complaint in complaints or ():
        text = (complaint or "").strip()
        if not text:
            continue
        if _SUGGESTION_RE.search(text) or _INVENTION_RE.search(text):
            continue
        if not _RENDER_SYMPTOM_RE.search(text):
            continue
        if text.lower() not in {k.lower() for k in kept}:
            kept.append(text)
        if len(kept) >= MAX_VISUAL_COMPLAINTS:
            break
    return kept


def build_visual_repair_prompt(target: str, page: str, complaints: list[str]) -> str:
    """Ask for the page again, with the visible defects named.

    Says nothing about improving, modernising or polishing: "regenerate this
    page, better" is how a 7B loses the half it got right. Same shape as
    `intent.build_repair_prompt`, and the same prohibition on removing content.
    """
    items = "\n".join(f"- {c}" for c in complaints)
    return (
        f"The page `{page or '/'}` was opened in a browser and looked at. These "
        f"visible problems were found in how it renders:\n{items}\n\n"
        f"Fix `{target}` so they are gone. Keep every heading, field, link, "
        f"route name and piece of content exactly as it is — this is a layout "
        f"fix, not a rewrite. Use the components that already exist "
        f"(`.card`, `.grid`, `.stack`, `.table-wrap`, `{{{{ ui.field(...) }}}}`) "
        f"and the variables in `static/css/theme.css`. Do not add a new "
        f"stylesheet, new sections, or new content of any kind."
    )
