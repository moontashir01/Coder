"""The vision critique on the rendered page (Phase W7).

The least reliable stage in the pipeline, so most of these tests are about it
declining to act. It is a 7B VL judging a 7B's markup — the same problem
`intent.py` solves one layer down, and it reuses that module's parser precisely
so "unparseable = PASS" is not re-implemented (and re-broken) here.

Fully offline: the vision model is a stub, and the pure functions take strings.
"""

import contextlib

import pytest

from app.agent import pageaudit
from app.agent.browser import PageProbe
from app.agent.core import AgentCore
from app.agent.pageaudit import Finding, SiteAudit, audit_site
from app.agent.projectspec import Page, ProjectSpec
from app.agent.smoke import SmokeResult
from app.agent.visualcheck import (
    MAX_VISUAL_COMPLAINTS,
    VISUAL_CHECKLIST,
    build_visual_prompt,
    build_visual_repair_prompt,
    filter_visual_complaints,
    parse_visual_verdict,
)
from config.settings import settings

# --- the prompt is a checklist, not "critique this" -------------------------


def test_the_prompt_names_the_page_and_the_device():
    phone = build_visual_prompt("/products", 390)
    desktop = build_visual_prompt("/products", 1280)
    assert "/products" in phone and "phone" in phone and "390px" in phone
    assert "desktop" in desktop


def test_the_checklist_forbids_inventing_content():
    """An open prompt on a 7B VL returns 'consider adding testimonials', which
    is a feature request wearing a defect's clothes."""
    assert "Never suggest adding content" in VISUAL_CHECKLIST
    assert "PASS" in VISUAL_CHECKLIST and "MISSING:" in VISUAL_CHECKLIST


# --- reading the verdict ----------------------------------------------------


def test_pass_is_pass():
    assert parse_visual_verdict("PASS") == []


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "The page has a clean and modern feel.",
        "I am not able to view images.",
        "```\nnonsense\n```",
    ],
)
def test_anything_unreadable_is_a_pass(raw):
    """Every ambiguity resolves toward leaving the page alone — `intent.py`'s
    rule, inherited by reusing its parser rather than writing a second one."""
    assert parse_visual_verdict(raw) == []


def test_a_defect_list_is_read():
    raw = (
        "MISSING:\n- the price text overlaps the product image\n- the header is cut off"
    )
    assert parse_visual_verdict(raw) == [
        "the price text overlaps the product image",
        "the header is cut off",
    ]


# --- filtering: only visible symptoms, never feature requests ---------------


@pytest.mark.parametrize(
    "complaint",
    [
        "the price text overlaps the product image",
        "the heading is cut off on the right",
        "the body text is unreadable against the dark background",
        "the form is squashed into the corner",
        "the product image is stretched",
        "content extends past the right edge of the screen",
    ],
)
def test_a_visible_symptom_survives(complaint):
    assert filter_visual_complaints([complaint]) == [complaint]


@pytest.mark.parametrize(
    "complaint",
    [
        "add a testimonials section",
        "consider adding a hero image",
        "the page could be more modern",
        "a footer should be included",
        "the site would benefit from a search bar",
        "the colour scheme is not very professional",
        "there is no navigation menu",
    ],
)
def test_a_feature_request_or_an_opinion_is_dropped(complaint):
    """The tension `buildspec._clean_nav` resolves: the user asked for a page,
    not for whatever the model would have built instead."""
    assert filter_visual_complaints([complaint]) == []


def test_the_complaint_list_is_capped():
    many = [f"element {i} overlaps element {i + 1}" for i in range(10)]
    assert len(filter_visual_complaints(many)) == MAX_VISUAL_COMPLAINTS


def test_duplicates_collapse():
    assert filter_visual_complaints(["the text is cut off", "The text is cut off"]) == [
        "the text is cut off"
    ]


# --- the repair prompt ------------------------------------------------------


def test_the_repair_prompt_forbids_a_rewrite():
    """'regenerate this page, better' is how a 7B loses the half it got right."""
    text = build_visual_repair_prompt(
        "templates/products.html", "/products", ["the price overlaps the image"]
    )
    assert "templates/products.html" in text and "/products" in text
    assert "the price overlaps the image" in text
    assert "layout fix, not a rewrite" in text
    assert "Do not add a new" in text and "new content" in text


# --- screenshots are only taken when someone is going to look at them -------


class _FakeSession:
    def __init__(self):
        self.shots = []

    def probe(self, url, width=1280, scripts=None):
        return PageProbe(
            url=url,
            width=width,
            status=200,
            data={
                "layout": {"viewport_width": width, "scroll_width": width},
                "audit": {"main_text_length": 50, "main_child_count": 2},
                "controls": [],
            },
        )

    def screenshot(self, url, width=1280, full_page=True):
        self.shots.append((url, width, full_page))
        return b"\x89PNG-fake"

    def click(self, url, selector, width=1280):
        raise AssertionError("no controls to click")


def _install(monkeypatch, session):
    @contextlib.contextmanager
    def _fake(timeout=None):
        yield session

    monkeypatch.setattr(pageaudit, "browser_session", _fake)


def test_no_screenshots_unless_asked(monkeypatch):
    session = _FakeSession()
    _install(monkeypatch, session)
    audit = audit_site("http://127.0.0.1:5000", ["/"], widths=[390])
    assert audit.screenshots == () and session.shots == []


def test_screenshots_are_bounded_by_pages_not_by_widths(monkeypatch):
    session = _FakeSession()
    _install(monkeypatch, session)
    audit = audit_site(
        "http://127.0.0.1:5000",
        ["/", "/products", "/cart"],
        widths=[1280, 390],
        screenshot_pages=1,
    )
    # One page, both widths — the width IS the point of the second shot.
    assert [(p, w) for p, w, _png in audit.screenshots] == [("/", 1280), ("/", 390)]
    assert all(full_page is False for _u, _w, full_page in session.shots)


# --- the review itself ------------------------------------------------------


def _audit_with_shots():
    return SiteAudit(
        ran=True,
        pages=("/products",),
        widths=(1280,),
        screenshots=(("/products", 1280, b"\x89PNG"),),
    )


def _project(tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "products.html").write_text("GOOD", encoding="utf-8")
    return ProjectSpec(
        pages=(Page(route="/products", template="templates/products.html"),)
    )


async def test_the_critique_is_off_by_default(tmp_path, monkeypatch):
    spec = _project(tmp_path)
    a = AgentCore(session_id="pytest_w7_off")

    def boom(*args, **kwargs):
        raise AssertionError("the vision model must not be reached")

    monkeypatch.setattr("app.agent.core.ask_about_image", boom)
    assert settings.check_visual is False
    assert await a._visual_review(_audit_with_shots(), spec, tmp_path) == ("", [], {})


async def test_a_seen_defect_repairs_the_page(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "check_visual", True)
    spec = _project(tmp_path)
    a = AgentCore(session_id="pytest_w7_repair")
    monkeypatch.setattr(
        "app.agent.core.ask_about_image",
        lambda *args, **kwargs: "MISSING:\n- the price text overlaps the image",
    )
    calls = []

    async def fake_file_op(message, target=None, extra_context="", on_token=None):
        calls.append((target, message))
        return "ok", [{"tool": "write_file"}]

    monkeypatch.setattr(a, "_file_op_flow", fake_file_op)
    note, trace, snapshot = await a._visual_review(_audit_with_shots(), spec, tmp_path)

    assert calls and calls[0][0] == "templates/products.html"
    assert "overlaps" in calls[0][1]
    assert trace and snapshot == {"templates/products.html": "GOOD"}
    assert "seen, not measured" in note


async def test_no_vision_model_is_not_a_defect(tmp_path, monkeypatch):
    """A missing model must read as "this check did not run", never as a pass
    with complaints or a rewrite."""
    monkeypatch.setattr(settings, "check_visual", True)
    spec = _project(tmp_path)
    a = AgentCore(session_id="pytest_w7_nomodel")
    monkeypatch.setattr("app.agent.core.ask_about_image", lambda *a_, **k: None)

    async def boom(*args, **kwargs):
        raise AssertionError("must not rewrite")

    monkeypatch.setattr(a, "_file_op_flow", boom)
    assert await a._visual_review(_audit_with_shots(), spec, tmp_path) == ("", [], {})


async def test_a_feature_request_never_reaches_the_repair(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "check_visual", True)
    spec = _project(tmp_path)
    a = AgentCore(session_id="pytest_w7_invention")
    monkeypatch.setattr(
        "app.agent.core.ask_about_image",
        lambda *args, **kwargs: "MISSING:\n- add a testimonials section\n- consider a hero image",
    )

    async def boom(*args, **kwargs):
        raise AssertionError("must not rewrite")

    monkeypatch.setattr(a, "_file_op_flow", boom)
    assert await a._visual_review(_audit_with_shots(), spec, tmp_path) == ("", [], {})


async def test_a_page_with_no_file_is_reported_not_guessed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "check_visual", True)
    a = AgentCore(session_id="pytest_w7_notarget")
    monkeypatch.setattr(
        "app.agent.core.ask_about_image",
        lambda *args, **kwargs: "MISSING:\n- the heading is cut off",
    )

    async def boom(*args, **kwargs):
        raise AssertionError("must not rewrite")

    monkeypatch.setattr(a, "_file_op_flow", boom)
    note, trace, snapshot = await a._visual_review(
        _audit_with_shots(), ProjectSpec(), tmp_path
    )
    assert trace == [] and snapshot == {}
    assert "no page file owns it" in note


# --- the guard: a repair that makes things worse is undone ------------------


def _fake_smoke(audits, findings):
    """A run_smoke_test stand-in that reports `findings` on the next run."""

    def run(entry, workdir, paths, timeout, warmup, spec, hook):
        audits.append(
            SiteAudit(ran=True, pages=("/",), widths=(390,), findings=findings)
        )
        return SmokeResult(True, True, 200, 5000, "started")

    return run


async def test_a_repair_that_regresses_the_measurements_is_reverted(
    tmp_path, monkeypatch
):
    """THE rule that keeps this stage from being a net negative: a visual fix
    that introduces horizontal overflow must be undone automatically."""
    page = tmp_path / "templates" / "products.html"
    page.parent.mkdir()
    page.write_text("GOOD", encoding="utf-8")
    a = AgentCore(session_id="pytest_w7_revert")
    audits = [SiteAudit(ran=True, pages=("/",), widths=(390,))]  # before: no errors
    worse = (Finding(kind="overflow", page="/", detail="900px wide", width=390),)
    monkeypatch.setattr("app.agent.core.run_smoke_test", _fake_smoke(audits, worse))

    async def repair(audit):
        snapshot = {"templates/products.html": page.read_text(encoding="utf-8")}
        page.write_text("BAD", encoding="utf-8")
        return "  fix  rewrote it", [{"tool": "write_file"}], snapshot

    note, trace, result = await a._guarded_repair(
        "visual",
        repair,
        SmokeResult(True, True, 200, 5000, "before"),
        tmp_path / "app.py",
        tmp_path,
        [],
        None,
        None,
        audits,
    )

    assert page.read_text(encoding="utf-8") == "GOOD"
    assert "reverted" in note and "templates/products.html" in note
    assert trace  # the attempt is still reported, not hidden
    # The restored files are byte-identical to the ones that produced the
    # earlier audit, so its measurements are the truth again.
    assert audits[-1].errors() == ()
    assert result.detail == "before"


async def test_a_repair_that_helps_is_kept(tmp_path, monkeypatch):
    page = tmp_path / "templates" / "products.html"
    page.parent.mkdir()
    page.write_text("GOOD", encoding="utf-8")
    a = AgentCore(session_id="pytest_w7_keep")
    audits = [
        SiteAudit(
            ran=True,
            pages=("/",),
            widths=(390,),
            findings=(Finding(kind="overflow", page="/", detail="900px"),),
        )
    ]
    monkeypatch.setattr("app.agent.core.run_smoke_test", _fake_smoke(audits, ()))

    async def repair(audit):
        snapshot = {"templates/products.html": page.read_text(encoding="utf-8")}
        page.write_text("FIXED", encoding="utf-8")
        return "  fix  rewrote it", [{"tool": "write_file"}], snapshot

    note, trace, result = await a._guarded_repair(
        "visual",
        repair,
        SmokeResult(True, True, 200, 5000, "before"),
        tmp_path / "app.py",
        tmp_path,
        [],
        None,
        None,
        audits,
    )
    assert page.read_text(encoding="utf-8") == "FIXED"
    assert "reverted" not in note
    assert result.detail == "started"


async def test_a_repair_pass_that_raises_costs_only_itself(tmp_path, monkeypatch):
    a = AgentCore(session_id="pytest_w7_boom")

    async def repair(audit):
        raise RuntimeError("nope")

    before = SmokeResult(True, True, 200, 5000, "before")
    note, trace, result = await a._guarded_repair(
        "visual", repair, before, tmp_path / "app.py", tmp_path, [], None, None, []
    )
    assert (note, trace) == ("", [])
    assert result is before


async def test_nothing_rewritten_means_no_second_server_start(tmp_path, monkeypatch):
    a = AgentCore(session_id="pytest_w7_norerun")

    def boom(*args, **kwargs):
        raise AssertionError("must not re-run the smoke test")

    monkeypatch.setattr("app.agent.core.run_smoke_test", boom)

    async def repair(audit):
        return "", [], {}

    note, trace, result = await a._guarded_repair(
        "visual",
        repair,
        SmokeResult(True, True, 200, 5000, "before"),
        tmp_path / "app.py",
        tmp_path,
        [],
        None,
        None,
        [],
    )
    assert trace == [] and result.detail == "before"
