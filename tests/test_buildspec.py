"""Tests for the shared build spec — the cross-file requirements distilled once.

All offline: the extraction call uses a scripted LLM, everything else is pure.
"""

import json
from types import SimpleNamespace

from app.agent.buildspec import (
    _CONCRETE_CSS_RE,
    BuildSpec,
    build_spec_from_data,
    chroma_lightness,
    find_style_keywords,
    mentions_shared_spec,
    palette_matches_style,
)
from app.agent.core import AgentCore


class ScriptedLLM:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    def invoke(self, messages):
        out = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        return SimpleNamespace(content=out)


class RecordingLLM(ScriptedLLM):
    def __init__(self, outputs):
        super().__init__(outputs)
        self.prompts: list[str] = []

    def invoke(self, messages):
        self.prompts.append("\n".join(str(getattr(m, "content", m)) for m in messages))
        return super().invoke(messages)


NAV_PROMPT = (
    "Build a multi-page site. The navigation should be: Our Story | Event Details "
    "| RSVP | Gallery. Use soft pastel colors and script headings."
)


# ---------------------------------------------------------------------------
# The gate — an extraction call only when the request says something shared
# ---------------------------------------------------------------------------


def test_gate_fires_on_navigation_and_design_language():
    assert mentions_shared_spec(NAV_PROMPT) is True
    assert mentions_shared_spec("give every page the same navbar") is True
    assert mentions_shared_spec("use a warm color palette and a serif font") is True


def test_gate_ignores_plain_split_requests():
    """Structural words ('stylesheet', 'css', 'styles') say nothing about the
    look — tripping on them would spend an LLM call on every ordinary split."""
    for msg in (
        "separate index.html into html, css and js files",
        "Create three files: index.html, styles.css and script.js for a small webpage.",
        "Create a webpage as separate files with an external stylesheet",
        "split the site into css and html files",
    ):
        assert mentions_shared_spec(msg) is False, msg


# ---------------------------------------------------------------------------
# Filtering — add what the user said, invent nothing
# ---------------------------------------------------------------------------


def test_navigation_labels_are_kept_verbatim_with_targets():
    spec = build_spec_from_data(
        {
            "navigation": [
                {"label": "Our Story", "file": "our-story.html"},
                {"label": "Event Details", "file": "details.html"},
                {"label": "RSVP", "file": "rsvp.html"},
                {"label": "Gallery", "file": "gallery.html"},
            ]
        },
        NAV_PROMPT,
    )
    assert spec.nav_labels() == ("Our Story", "Event Details", "RSVP", "Gallery")
    assert spec.nav_files() == (
        "our-story.html",
        "details.html",
        "rsvp.html",
        "gallery.html",
    )


def test_navigation_labels_the_user_never_wrote_are_dropped():
    spec = build_spec_from_data(
        {
            "navigation": [
                {"label": "Our Story", "file": "our-story.html"},
                {"label": "Blog", "file": "blog.html"},  # hallucinated
                {"label": "Careers", "file": "careers.html"},  # hallucinated
            ]
        },
        NAV_PROMPT,
    )
    assert spec.nav_labels() == ("Our Story",)


def test_navigation_target_is_derived_when_missing_or_not_a_page():
    spec = build_spec_from_data(
        {
            "navigation": [
                {"label": "Event Details"},
                {"label": "RSVP", "file": "/rsvp"},
            ]
        },
        NAV_PROMPT,
    )
    assert spec.nav_files() == ("event-details.html", "rsvp.html")


def test_empty_spec_when_the_prompt_specifies_nothing():
    spec = build_spec_from_data(None, "separate index.html into html, css and js files")
    assert spec.is_empty()
    assert spec.to_context_block() == ""


def test_no_design_values_without_style_words():
    """A model that volunteers fonts/colours for a prompt with no style language
    is guessing — those are dropped rather than imposed on the build."""
    spec = build_spec_from_data(
        {"fonts": ["Comic Sans MS"], "palette": ["#ff0000"], "decorative": "confetti"},
        "split index.html into separate files",
    )
    assert spec.fonts == ()
    assert spec.palette == ()
    assert spec.decorative == ""


def test_behaviors_must_reference_a_known_page():
    spec = build_spec_from_data(
        {
            "navigation": [{"label": "RSVP", "file": "rsvp.html"}],
            "behaviors": [
                "every page has a call-to-action linking to rsvp.html",
                "add a newsletter signup to the checkout flow",  # invented
            ],
        },
        NAV_PROMPT,
    )
    assert spec.behaviors == ("every page has a call-to-action linking to rsvp.html",)


# ---------------------------------------------------------------------------
# Design translation (Gap 2) — style words become concrete CSS decisions
# ---------------------------------------------------------------------------


def test_style_keywords_come_from_the_message():
    assert "pastel" in find_style_keywords(NAV_PROMPT)
    assert "script" in find_style_keywords(NAV_PROMPT)
    assert find_style_keywords("create two files") == ()


def test_llm_design_values_are_used_when_concrete():
    spec = build_spec_from_data(
        {
            "fonts": ["Great Vibes", "Lato"],
            "palette": ["#fdf6f0", "#f3e0d5", "not-a-color", "#e8dff5"],
            "decorative": "floral pseudo-element flourishes with 12px rounded cards",
        },
        NAV_PROMPT,
    )
    assert spec.fonts == ("Great Vibes", "Lato")
    assert spec.palette == ("#fdf6f0", "#f3e0d5", "#e8dff5")  # junk filtered out
    assert "floral" in spec.decorative


def test_a_palette_that_contradicts_the_style_words_is_replaced():
    """Seen live: asked for "soft pastel", the 7B model returned gold, orange
    and crimson. Those are not pastels — the preset is the better answer."""
    spec = build_spec_from_data(
        {"palette": ["#F0E68C", "#FFD700", "#FFA500", "#FF4500", "#DC143C"]},
        NAV_PROMPT,
    )
    assert "#FFD700" not in spec.palette
    assert palette_matches_style(spec.palette, NAV_PROMPT)  # the preset does
    assert (
        chroma_lightness(spec.palette[0])[1] >= 0.8
    )  # surfaces are light; text stays dark


def test_a_decorative_line_that_restates_the_request_is_replaced():
    """Also seen live: "features soft pastel colors ... to create a warm and
    inviting atmosphere" — true, and useless to a file generator."""
    spec = build_spec_from_data(
        {
            "decorative": "The site features soft pastel colors and a romantic atmosphere"
        },
        NAV_PROMPT,
    )
    assert "atmosphere" not in spec.decorative
    assert _CONCRETE_CSS_RE.search(spec.decorative)


def test_palette_check_only_judges_the_objective_cases():
    warm = ("#8b0000", "#b22222", "#cd5c5c")
    assert palette_matches_style(warm, "a bold retro poster site") is True
    assert palette_matches_style(warm, "soft pastel invitations") is False
    assert palette_matches_style(("#0f1115", "#1a1d24"), "dark mode dashboard") is True
    assert palette_matches_style(("#ffffff", "#fafafa"), "dark mode dashboard") is False


def test_rule_based_preset_fills_in_when_the_llm_gives_nothing():
    """The safety net: 'pastel'/'script' must still reach the CSS as real font
    names and real hex codes, not as adjectives the 7B model will ignore."""
    spec = build_spec_from_data({}, NAV_PROMPT)
    assert spec.fonts  # a concrete Google Font family
    assert all(c.startswith("#") for c in spec.palette)
    assert len(spec.palette) >= 3
    assert spec.decorative


def test_preset_is_chosen_by_the_style_words_used():
    dark = build_spec_from_data({}, "make a dark mode dashboard across several pages")
    minimal = build_spec_from_data({}, "a minimalist multi-page site")
    assert dark.palette != minimal.palette


# ---------------------------------------------------------------------------
# The context block that is injected into every per-file call
# ---------------------------------------------------------------------------


def test_context_block_states_nav_and_design_and_stays_compact():
    spec = build_spec_from_data(
        {
            "navigation": [
                {"label": "Our Story", "file": "our-story.html"},
                {"label": "RSVP", "file": "rsvp.html"},
            ]
        },
        NAV_PROMPT,
    )
    block = spec.to_context_block()
    assert "Our Story" in block and "our-story.html" in block
    assert "RSVP" in block
    assert "#" in block  # concrete palette
    assert len(block) < 1500  # rides alongside the manifest + sibling context


def test_context_block_empty_for_empty_spec():
    assert BuildSpec().to_context_block() == ""


# ---------------------------------------------------------------------------
# AgentCore._extract_build_spec — gating + threading into the build
# ---------------------------------------------------------------------------


async def test_extract_build_spec_skips_the_llm_when_nothing_is_shared(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_spec_gate")
    a._llm_direct = ScriptedLLM(["should not be called"])

    spec = await a._extract_build_spec(
        "separate index.html into html, css and js files"
    )

    assert spec.is_empty()
    assert a._llm_direct.calls == 0


async def test_extract_build_spec_parses_and_filters(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_spec_parse")
    a._llm_direct = ScriptedLLM(
        [
            json.dumps(
                {
                    "navigation": [
                        {"label": "Our Story", "file": "our-story.html"},
                        {"label": "Pricing", "file": "pricing.html"},
                    ],
                    "fonts": ["Great Vibes", "Lato"],
                    "palette": ["#f6e7ef", "#4a3f45"],
                    "decorative": "soft rounded cards",
                    "behaviors": [],
                }
            )
        ]
    )

    spec = await a._extract_build_spec(NAV_PROMPT)

    assert a._llm_direct.calls == 1
    assert spec.nav_labels() == ("Our Story",)  # "Pricing" was never requested
    assert spec.fonts == ("Great Vibes", "Lato")


async def test_extract_build_spec_survives_an_llm_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_spec_boom")

    class Boom:
        def invoke(self, messages):
            raise RuntimeError("offline")

    a._llm_direct = Boom()
    spec = await a._extract_build_spec(NAV_PROMPT)

    assert spec.nav == ()  # nothing invented
    assert spec.palette  # but the style words still became concrete CSS


async def test_extract_build_spec_disabled_by_setting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from config.settings import settings

    monkeypatch.setattr(settings, "extract_build_spec", False)
    a = AgentCore(session_id="pytest_spec_off")
    a._llm_direct = ScriptedLLM(["should not be called"])

    assert (await a._extract_build_spec(NAV_PROMPT)).is_empty()
    assert a._llm_direct.calls == 0


async def test_multi_file_flow_threads_the_spec_into_every_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_spec_thread")
    a._llm_direct = RecordingLLM(
        [
            # call 0: the build spec
            json.dumps(
                {
                    "navigation": [
                        {"label": "Our Story", "file": "our-story.html"},
                        {"label": "RSVP", "file": "rsvp.html"},
                    ],
                    "fonts": ["Great Vibes", "Lato"],
                    "palette": ["#f6e7ef", "#4a3f45"],
                    "decorative": "soft rounded cards",
                }
            ),
            # call 1: the file plan
            '{"files": ['
            '{"filename": "styles.css", "action": "create", "instruction": "site styles"},'
            '{"filename": "our-story.html", "action": "create", "instruction": "the story page"}'
            "]}",
            # call 2: styles.css
            "FILENAME: styles.css\nbody{margin:0}",
            # call 3: our-story.html
            "FILENAME: our-story.html\n<html><body><p>x</p></body></html>",
        ]
    )

    await a._multi_file_flow(NAV_PROMPT + " Split it into separate files.", refs=[])

    plan_prompt = a._llm_direct.prompts[1]
    css_prompt = a._llm_direct.prompts[2]
    page_prompt = a._llm_direct.prompts[3]
    # The planner sees the nav the user dictated…
    assert "Our Story" in plan_prompt and "rsvp.html" in plan_prompt
    # …the stylesheet gets the concrete fonts/colours, not the adjectives…
    assert "Great Vibes" in css_prompt and "#f6e7ef" in css_prompt
    # …and every page gets the same canonical nav list.
    assert "Our Story" in page_prompt and "RSVP" in page_prompt
    # The spec is retained for the post-generation nav check.
    assert a._build_spec is not None and a._build_spec.nav_labels() == (
        "Our Story",
        "RSVP",
    )


async def test_multi_file_flow_names_the_shared_assets_exactly(tmp_path, monkeypatch):
    """Gap 4: the pages must be told the ONE script/stylesheet name, so they
    don't link a variant spelling that then gets created as a duplicate."""
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_shared_assets")
    a._llm_direct = RecordingLLM(
        [
            '{"files": ['
            '{"filename": "script.js", "action": "create", "instruction": "behaviour"},'
            '{"filename": "index.html", "action": "create", "instruction": "the page"}'
            "]}",
            "FILENAME: script.js\nconsole.log(1)",
            "FILENAME: index.html\n<html><body><p>x</p></body></html>",
        ]
    )

    await a._multi_file_flow("split the site into separate files", refs=[])

    assert "The shared script is `script.js`" in a._llm_direct.prompts[2]
