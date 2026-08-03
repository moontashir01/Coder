"""The layout audit and the dead-button probe (Phases W5/W6).

Fully offline: every decision in `pageaudit.py` is a pure function over a
`PageProbe`, so the whole default suite runs with no browser — exactly the split
`browser.py` was shaped for. The one driver test uses a fake `Session`, because
what is being tested here is the loop and the caps, not Chromium.

The rule these tests exist to hold: **a false failure is worse than no check.**
`functional_probe` learned it by reporting a failure for a row that had
persisted, which sent the repair loop off to rewrite working code. So most of
what follows asserts that something is NOT reported.
"""

import contextlib
import shutil
import subprocess

import pytest

from app.agent import pageaudit
from app.agent.browser import ConsoleMessage, NetworkFailure, PageProbe
from app.agent.core import AgentCore
from app.agent.pageaudit import (
    AuditCheck,
    Finding,
    SiteAudit,
    audit_site,
    click_findings,
    console_findings,
    empty_content,
    horizontal_overflow,
    is_destructive,
    layout_findings,
    low_contrast,
    network_findings,
    probe_urls,
    repair_instruction,
    repair_plan,
    triage_controls,
    unsized_images,
)
from app.agent.projectspec import Page, ProjectSpec
from app.agent.smoke import ProbeCheck, SmokeResult
from config.settings import settings


def make_probe(url="http://127.0.0.1:5000/products", width=390, **data) -> PageProbe:
    """A probe carrying only what a test cares about; the rest is a clean page."""
    layout = {
        "viewport_width": width,
        "scroll_width": width,
        "body_scroll_width": width,
        "overflowing": [],
        "main_text_length": 120,
        "images_without_size": [],
    }
    audit = {
        "contrast": [],
        "contrast_measured": 20,
        "contrast_skipped": 0,
        "main_text_length": 120,
        "main_child_count": 3,
        "main_media_count": 0,
    }
    layout.update(data.pop("layout", {}))
    audit.update(data.pop("audit", {}))
    return PageProbe(
        url=url,
        width=width,
        status=data.pop("status", 200),
        data={"layout": layout, "audit": audit, **data.pop("extra", {})},
        console=data.pop("console", ()),
        failed_requests=data.pop("failed_requests", ()),
    )


# --- W5: horizontal overflow ------------------------------------------------


def test_a_page_wider_than_the_phone_is_reported_with_its_culprit():
    probe = make_probe(
        layout={
            "scroll_width": 900,
            "body_scroll_width": 900,
            "overflowing": [{"selector": "table.table", "right": 900, "width": 880}],
        }
    )
    (finding,) = horizontal_overflow(probe)
    assert finding.kind == "overflow" and finding.severity == "error"
    assert finding.page == "/products" and finding.width == 390
    assert "900px" in finding.detail and "390px" in finding.detail
    assert (
        "table.table" in finding.detail
    )  # a measurement with no culprit is unactionable


def test_sub_pixel_rounding_is_not_an_overflow():
    assert horizontal_overflow(make_probe(layout={"scroll_width": 392})) == []


def test_a_wide_table_inside_a_scroll_container_is_not_a_defect():
    """`.table-wrap` scrolls its table sideways ON PURPOSE — it is the component
    W1 shipped to fix this exact problem. Judging on the element's box rather
    than the document's would fail every page that uses it correctly."""
    probe = make_probe(
        layout={
            "scroll_width": 390,
            "body_scroll_width": 390,
            "overflowing": [{"selector": "table.table", "right": 880, "width": 860}],
        }
    )
    assert horizontal_overflow(probe) == []


def test_a_script_that_never_ran_claims_nothing():
    probe = PageProbe(url="http://127.0.0.1:5000/", width=390, status=200, data={})
    assert horizontal_overflow(probe) == []
    assert layout_findings(probe) == []


def test_a_page_that_never_loaded_is_not_measured():
    probe = PageProbe(url="http://127.0.0.1:5000/x", width=390, error="timeout")
    assert layout_findings(probe) == []


# --- W5: empty page, contrast, images ---------------------------------------


def test_a_page_that_rendered_nothing_is_reported():
    probe = make_probe(
        layout={"main_text_length": 0},
        audit={"main_text_length": 0, "main_child_count": 0, "main_media_count": 0},
    )
    (finding,) = empty_content(probe)
    assert finding.kind == "empty"


def test_an_empty_table_is_not_an_empty_page():
    """The listing rendered; it has no rows. Calling that a failure sends the
    repair loop after the seed data, which is not this check's business."""
    probe = make_probe(
        layout={"main_text_length": 0},
        audit={"main_text_length": 0, "main_child_count": 1, "main_media_count": 0},
    )
    assert empty_content(probe) == []


def test_an_image_only_page_is_not_empty():
    probe = make_probe(
        layout={"main_text_length": 0},
        audit={"main_text_length": 0, "main_child_count": 0, "main_media_count": 4},
    )
    assert empty_content(probe) == []


def test_body_text_below_aa_is_reported():
    probe = make_probe(
        audit={
            "contrast": [
                {
                    "selector": "p.lede",
                    "ratio": 3.5,
                    "font_size": 16,
                    "bold": False,
                    "text": "Shop our books",
                }
            ]
        }
    )
    (finding,) = low_contrast(probe)
    assert finding.kind == "contrast" and "3.5:1" in finding.detail
    assert "p.lede" in finding.detail


def test_large_text_gets_the_large_text_allowance():
    """3:1 is AA for large text. Applying 4.5 to every heading would report a
    compliant page — the textbook false failure."""
    sample = {"selector": "h1", "ratio": 3.5, "font_size": 32, "bold": False}
    assert low_contrast(make_probe(audit={"contrast": [sample]})) == []
    small = dict(sample, selector="p", font_size=16)
    assert len(low_contrast(make_probe(audit={"contrast": [small]}))) == 1


def test_bold_18px_counts_as_large():
    sample = {"selector": "strong", "ratio": 3.2, "font_size": 19, "bold": True}
    assert low_contrast(make_probe(audit={"contrast": [sample]})) == []


def test_a_passing_ratio_is_silent():
    sample = {"selector": "p", "ratio": 12.4, "font_size": 16, "bold": False}
    assert low_contrast(make_probe(audit={"contrast": [sample]})) == []


def test_an_image_with_no_intrinsic_size_is_a_warning_not_a_failure():
    """An image that has not decoded yet measures the same as one that never
    will. That is not evidence enough to rewrite a template over."""
    probe = make_probe(layout={"images_without_size": ["/static/uploads/a.png"]})
    (finding,) = unsized_images(probe)
    assert finding.severity == "warning"
    assert not SiteAudit(ran=True, findings=(finding,)).errors()


# --- W6: console and network ------------------------------------------------


def test_an_uncaught_exception_is_reported_verbatim():
    """`ReferenceError: addToCart is not defined` in the prompt is what makes
    the repair land — the same reason `server_error()` lifts a 5xx's title."""
    probe = make_probe(
        console=(
            ConsoleMessage("pageerror", "ReferenceError: addToCart is not defined"),
        )
    )
    (finding,) = console_findings(probe)
    assert "addToCart is not defined" in finding.detail


def test_console_logs_and_warnings_are_not_errors():
    probe = make_probe(
        console=(
            ConsoleMessage("log", "hello"),
            ConsoleMessage("warning", "deprecated"),
        )
    )
    assert console_findings(probe) == []


def test_a_resource_load_message_is_left_to_the_network_check():
    """Both fire for one 404. The network one names the URL and the status, so
    reporting the console echo as well says the same thing twice."""
    probe = make_probe(
        console=(
            ConsoleMessage(
                "error", "Failed to load resource: the server responded with 404"
            ),
        ),
        failed_requests=(
            NetworkFailure(url="http://127.0.0.1:5000/static/js/x.js", status=404),
        ),
    )
    assert console_findings(probe) == []
    (finding,) = network_findings(probe)
    assert "404" in finding.detail and "/static/js/x.js" in finding.detail


def test_a_missing_favicon_is_not_a_defect():
    probe = make_probe(
        failed_requests=(
            NetworkFailure(url="http://127.0.0.1:5000/favicon.ico", status=404),
        )
    )
    assert network_findings(probe) == []


# --- W6: which controls are safe to click -----------------------------------


BUTTON = {
    "kind": "button",
    "selector": "button.button",
    "path": "body:nth-child(2) > button:nth-child(1)",
    "name": "Add to cart",
    "type": "button",
    "disabled": False,
    "in_form": False,
}


@pytest.mark.parametrize("name", ["Delete", "Remove item", "Log out", "Clear cart"])
def test_a_destructive_control_is_skipped(name):
    """A Delete button that WORKS empties the seeded data the later checks
    assert against — the one case where success is the problem."""
    assert is_destructive(dict(BUTTON, name=name))


def test_a_reset_input_is_destructive_whatever_it_is_called():
    assert is_destructive(dict(BUTTON, name="Start over", type="reset"))


def test_an_ordinary_button_is_clicked_and_a_skip_is_reported():
    click, skipped = triage_controls([BUTTON, dict(BUTTON, name="Delete")])
    assert [c["name"] for c in click] == ["Add to cart"]
    assert len(skipped) == 1 and "destructive" in skipped[0]


def test_a_post_form_is_never_submitted_by_the_browser():
    """`functional_probe` already posts to every write endpoint with real values
    and requires the value to come back. Submitting here would insert a SECOND
    row behind the checks that assert against the seeded data."""
    control = dict(BUTTON, in_form=True, form_method="post", required_empty=0)
    click, skipped = triage_controls([control])
    assert click == [] and "functional probe" in skipped[0]


def test_a_form_with_an_empty_required_field_is_skipped():
    """The browser blocks the submit itself, so "nothing changed" would be a
    false failure on a form that is perfectly correct."""
    control = dict(BUTTON, in_form=True, form_method="get", required_empty=1)
    click, skipped = triage_controls([control])
    assert click == [] and "required" in skipped[0]


def test_a_get_search_form_is_submitted():
    control = dict(
        BUTTON, name="Search", in_form=True, form_method="get", required_empty=0
    )
    click, _ = triage_controls([control])
    assert len(click) == 1


def test_a_disabled_control_is_skipped():
    click, skipped = triage_controls([dict(BUTTON, disabled=True)])
    assert click == [] and "disabled" in skipped[0]


# --- W6: what a click proves ------------------------------------------------


def _clicked(**kw):
    base = {
        "clicked": True,
        "url_changed": False,
        "text_length_before": 100,
        "text_length_after": 100,
        "html_length_before": 500,
        "html_length_after": 500,
    }
    base.update(kw)
    return base


def test_a_button_wired_to_nothing_is_reported():
    (finding,) = click_findings(BUTTON, "/products", _clicked(), make_probe())
    assert finding.kind == "dead-control" and "wired to nothing" in finding.detail
    assert "Add to cart" in finding.detail and "button.button" in finding.detail


def test_navigation_counts_as_working():
    assert click_findings(BUTTON, "/", _clicked(url_changed=True), make_probe()) == []


def test_new_content_counts_as_working():
    changed = _clicked(text_length_after=140, html_length_after=700)
    assert click_findings(BUTTON, "/", changed, make_probe()) == []


def test_a_class_toggle_counts_as_working():
    """innerText does not move when a handler only flips a class. Judging on the
    text alone would call a working tab switch dead."""
    changed = _clicked(html_length_after=505)
    assert click_findings(BUTTON, "/", changed, make_probe()) == []


def test_a_handler_that_throws_is_reported_with_the_exception():
    probe = make_probe(
        console=(
            ConsoleMessage("pageerror", "ReferenceError: addToCart is not defined"),
        )
    )
    (finding,) = click_findings(BUTTON, "/products", _clicked(), probe)
    assert finding.severity == "error"
    assert "addToCart is not defined" in finding.detail


def test_a_control_that_could_not_be_clicked_is_only_a_warning():
    changed = _clicked(clicked=False)
    (finding,) = click_findings(BUTTON, "/", changed, make_probe())
    assert finding.severity == "warning"


# --- the driver -------------------------------------------------------------


class FakeSession:
    """A Session that answers from a script. Records what it was asked."""

    def __init__(self, probes=None, controls=(), clicks=None):
        self.probes = probes or {}
        self.controls = controls
        self.clicks = clicks or {}
        self.visited = []
        self.clicked = []

    def probe(self, url, width=1280, scripts=None):
        self.visited.append((url, width))
        probe = self.probes.get(url) or make_probe(url=url, width=width)
        data = dict(probe.data)
        data["controls"] = list(self.controls)
        return PageProbe(
            url=url,
            width=width,
            status=probe.status,
            data=data,
            console=probe.console,
            failed_requests=probe.failed_requests,
            error=probe.error,
        )

    def click(self, url, selector, width=1280):
        self.clicked.append((url, selector))
        return make_probe(url=url), self.clicks.get(selector, _clicked())


@contextlib.contextmanager
def _session(session):
    yield session


def _install(monkeypatch, session):
    monkeypatch.setattr(
        pageaudit, "browser_session", lambda timeout=None: _session(session)
    )


def test_no_browser_means_a_skip_not_a_pass(monkeypatch):
    monkeypatch.setattr(
        pageaudit, "browser_session", lambda timeout=None: _session(None)
    )
    audit = audit_site("http://127.0.0.1:5000", ["/products"])
    assert audit.ran is False
    assert audit.checks() == []  # nothing is claimed, in either direction


def test_every_page_is_visited_at_every_width(monkeypatch):
    session = FakeSession()
    _install(monkeypatch, session)
    audit = audit_site("http://127.0.0.1:5000", ["/", "/products"], widths=[1280, 390])
    assert audit.ran and len(session.visited) == 4
    assert audit.pages == ("/", "/products")


def test_a_parameterised_route_has_no_address_to_visit():
    urls = probe_urls("http://127.0.0.1:5000", ["/products/<int:id>", "/products"])
    assert urls == ["http://127.0.0.1:5000/", "http://127.0.0.1:5000/products"]


def test_the_page_cap_reports_what_it_dropped(monkeypatch):
    session = FakeSession()
    _install(monkeypatch, session)
    routes = [f"/p{i}" for i in range(8)]
    audit = audit_site("http://127.0.0.1:5000", routes, widths=[390], max_pages=3)
    assert len(audit.pages) == 3 and len(audit.dropped_pages) == 6
    assert "page budget" in audit.note() and "6 page(s)" in audit.note()


def test_the_control_cap_reports_what_it_dropped(monkeypatch):
    controls = [
        dict(BUTTON, name=f"Btn {i}", path=f"button:nth-child({i})") for i in range(6)
    ]
    session = FakeSession(controls=controls)
    _install(monkeypatch, session)
    audit = audit_site("http://127.0.0.1:5000", ["/"], widths=[390], max_controls=2)
    assert audit.controls_clicked == 2 and audit.controls_dropped == 4
    assert "not clicked" in audit.checks()[-1].detail


def test_controls_are_enumerated_once_per_page_not_once_per_width(monkeypatch):
    session = FakeSession(controls=[BUTTON])
    _install(monkeypatch, session)
    audit = audit_site("http://127.0.0.1:5000", ["/"], widths=[1280, 390])
    assert audit.controls_clicked == 1


def test_the_same_console_error_at_two_widths_is_one_finding(monkeypatch):
    probe = make_probe(
        url="http://127.0.0.1:5000/",
        console=(ConsoleMessage("pageerror", "TypeError: x"),),
    )
    session = FakeSession(probes={"http://127.0.0.1:5000/": probe})
    _install(monkeypatch, session)
    audit = audit_site("http://127.0.0.1:5000", ["/"], widths=[1280, 390])
    assert len(audit.of_kind("console")) == 1


def test_overflow_keeps_its_width_because_the_width_is_the_point(monkeypatch):
    """A page that scrolls only on a phone is a different fact from one that
    scrolls everywhere, so this is the one finding NOT collapsed across widths."""
    wide = make_probe(
        url="http://127.0.0.1:5000/",
        layout={"scroll_width": 900, "body_scroll_width": 900},
    )
    session = FakeSession(probes={"http://127.0.0.1:5000/": wide})
    _install(monkeypatch, session)
    audit = audit_site("http://127.0.0.1:5000", ["/"], widths=[1280, 390])
    assert len(audit.of_kind("overflow")) == 2


def test_a_page_that_never_loaded_qualifies_the_pass_it_did_not_earn(monkeypatch):
    dead = PageProbe(url="http://127.0.0.1:5000/cart", width=390, error="timeout")
    session = FakeSession(probes={"http://127.0.0.1:5000/cart": dead})
    _install(monkeypatch, session)
    audit = audit_site("http://127.0.0.1:5000", ["/", "/cart"], widths=[390])
    assert audit.unobserved and audit.observations == 1
    assert "never loaded" in audit.checks()[0].detail


def test_a_browser_that_opened_nothing_claims_nothing(monkeypatch):
    """Four passes over zero observations is the exact shape of an unearned
    green. The checks disappear and the skip is stated instead."""
    dead = PageProbe(url="http://127.0.0.1:5000/", width=390, error="timeout")
    session = FakeSession(probes={"http://127.0.0.1:5000/": dead})
    _install(monkeypatch, session)
    audit = audit_site("http://127.0.0.1:5000", ["/"], widths=[390])
    assert audit.ran and audit.observations == 0
    assert audit.checks() == []
    assert "opened nothing" in audit.note()


# --- the aggregate report ---------------------------------------------------


def test_checks_are_one_line_per_question():
    audit = SiteAudit(ran=True, pages=("/",), widths=(390,))
    labels = [c.label for c in audit.checks()]
    assert labels == [
        "browser: no page scrolls sideways",
        "browser: the console is clean",
        "browser: every page renders content",
        "browser: text contrast is at least 4.5:1",
    ]
    assert all(c.ok for c in audit.checks())


def test_a_failing_check_names_the_page_and_the_measurement():
    finding = Finding(
        kind="overflow", page="/products", detail="the page is 900px wide", width=390
    )
    audit = SiteAudit(
        ran=True, pages=("/products",), widths=(390,), findings=(finding,)
    )
    check = audit.checks()[0]
    assert check.ok is False
    assert "/products at 390px" in check.detail and "900px" in check.detail


def test_a_warning_never_reads_as_a_failed_check():
    finding = Finding(
        kind="image-size",
        page="/",
        detail="2 image(s) have no size",
        severity="warning",
    )
    audit = SiteAudit(ran=True, pages=("/",), widths=(390,), findings=(finding,))
    assert all(c.ok for c in audit.checks())
    assert "note" in audit.note()


def test_a_skipped_control_is_reported_as_skipped():
    audit = SiteAudit(
        ran=True,
        pages=("/",),
        widths=(390,),
        controls_skipped=("/: Delete: destructive, would empty the seeded data",),
    )
    assert "skip" in audit.note() and "Delete" in audit.note()


# --- what feeds the repair loop, and what only gets reported ----------------


def test_contrast_and_network_findings_are_reported_never_repaired():
    """The palette lives in the frozen, deterministically written theme.css, and
    a missing file is `_repair_dead_references`' job — it CREATES the file
    rather than rewriting the page that asked for it."""
    audit = SiteAudit(
        ran=True,
        findings=(
            Finding(kind="contrast", page="/", detail="2.1:1"),
            Finding(kind="network", page="/", detail="404"),
        ),
    )
    assert repair_plan(audit, lambda f: "templates/index.html") == []


def test_repairable_findings_are_grouped_by_the_file_that_owns_them():
    audit = SiteAudit(
        ran=True,
        findings=(
            Finding(kind="overflow", page="/products", detail="900px"),
            Finding(kind="dead-control", page="/products", detail="dead"),
            Finding(kind="empty", page="/cart", detail="empty"),
        ),
    )
    plan = repair_plan(audit, lambda f: f"templates{f.page}.html")
    assert [path for path, _ in plan] == [
        "templates/products.html",
        "templates/cart.html",
    ]
    assert len(plan[0][1]) == 2


def test_a_finding_whose_file_cannot_be_named_is_not_repaired():
    audit = SiteAudit(
        ran=True, findings=(Finding(kind="overflow", page="/x", detail="900px"),)
    )
    assert repair_plan(audit, lambda f: None) == []


def test_the_repair_instruction_names_the_page_and_the_measurement():
    finding = Finding(
        kind="overflow",
        page="/products",
        detail="the page is 900px wide in a 390px viewport",
        width=390,
    )
    text = repair_instruction("templates/products.html", [finding])
    assert "templates/products.html" in text
    assert "/products at 390px" in text and "900px" in text
    assert "table-wrap" in text  # the fix, not just the complaint


# --- core wiring ------------------------------------------------------------


def test_a_browser_failure_never_sends_the_model_to_rewrite_the_server(tmp_path):
    """The reason `ProbeCheck.owner` exists: a table that scrolls sideways is
    not app.py's fault, and the smoke repair only edits app.py."""
    a = AgentCore(session_id="pytest_audit_owner")
    result = SmokeResult(
        True,
        True,
        200,
        5000,
        "started",
        checks=(
            ProbeCheck("browser: no page scrolls sideways", False, "900px", "browser"),
        ),
    )
    assert a._smoke_repair_instruction(tmp_path / "app.py", result) is None

    result = SmokeResult(
        True,
        True,
        200,
        5000,
        "started",
        checks=(
            ProbeCheck("POST /products", False, "HTTP 500"),
            ProbeCheck("browser: the console is clean", False, "TypeError", "browser"),
        ),
    )
    instruction = a._smoke_repair_instruction(tmp_path / "app.py", result)
    assert "POST /products" in instruction and "TypeError" not in instruction


def test_no_hook_when_browser_checks_are_off(monkeypatch):
    monkeypatch.setattr(settings, "browser_checks", False)
    a = AgentCore(session_id="pytest_audit_off")
    assert a._browser_hook(None, []) is None
    assert a._browser_skip_note() == ""


def test_asking_for_browser_checks_without_a_browser_says_so_loudly(monkeypatch):
    monkeypatch.setattr(settings, "browser_checks", True)
    monkeypatch.setattr("app.agent.core.browser_available", lambda: False)
    a = AgentCore(session_id="pytest_audit_hint")
    assert a._browser_hook(None, []) is None
    note = a._browser_skip_note()
    assert "playwright install chromium" in note and "skip" in note


def test_the_hook_returns_browser_owned_checks(monkeypatch):
    monkeypatch.setattr(settings, "browser_checks", True)
    monkeypatch.setattr("app.agent.core.browser_available", lambda: True)
    captured = SiteAudit(ran=True, pages=("/",), widths=(390,))
    monkeypatch.setattr("app.agent.core.audit_site", lambda *a, **k: captured)

    a = AgentCore(session_id="pytest_audit_hook")
    sink = []
    hook = a._browser_hook(ProjectSpec(pages=(Page(route="/"),)), sink)
    checks = hook(5000)

    assert sink == [captured]
    assert checks and all(c.owner == "browser" for c in checks)
    assert isinstance(checks[0], ProbeCheck)


def test_the_repair_target_is_the_page_s_own_template(tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "products.html").write_text("x", encoding="utf-8")
    spec = ProjectSpec(
        pages=(Page(route="/products", template="templates/products.html"),)
    )
    a = AgentCore(session_id="pytest_audit_target")
    finding = Finding(kind="overflow", page="/products", detail="900px")
    assert a._browser_target(finding, spec, tmp_path) == "templates/products.html"


def test_a_template_that_is_not_on_disk_is_never_a_target(tmp_path):
    """Same strictness as `_resolve_target_from_spec`: the caller reads a
    missing path as "create this", which would turn a measurement into a new
    empty file."""
    spec = ProjectSpec(
        pages=(Page(route="/products", template="templates/products.html"),)
    )
    a = AgentCore(session_id="pytest_audit_target_missing")
    finding = Finding(kind="overflow", page="/products", detail="900px")
    assert a._browser_target(finding, spec, tmp_path) is None


def test_a_javascript_error_targets_the_script_it_names(tmp_path):
    (tmp_path / "static" / "js").mkdir(parents=True)
    (tmp_path / "static" / "js" / "app.js").write_text("//", encoding="utf-8")
    a = AgentCore(session_id="pytest_audit_target_js")
    finding = Finding(
        kind="console",
        page="/",
        detail="JavaScript error: ReferenceError at /static/js/app.js:4",
    )
    assert a._browser_target(finding, None, tmp_path) == "static/js/app.js"


async def test_the_repair_is_bounded_and_targeted(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "max_browser_repairs", 1)
    (tmp_path / "templates").mkdir()
    for name in ("products.html", "cart.html"):
        (tmp_path / "templates" / name).write_text("x", encoding="utf-8")
    spec = ProjectSpec(
        pages=(
            Page(route="/products", template="templates/products.html"),
            Page(route="/cart", template="templates/cart.html"),
        )
    )
    audit = SiteAudit(
        ran=True,
        findings=(
            Finding(kind="overflow", page="/products", detail="900px", width=390),
            Finding(kind="dead-control", page="/products", detail="dead"),
            Finding(kind="overflow", page="/cart", detail="900px", width=390),
        ),
    )

    a = AgentCore(session_id="pytest_audit_repair")
    a._last_write_path = "templates/original.html"
    calls = []

    async def fake_file_op(message, target=None, extra_context="", on_token=None):
        calls.append((target, message))
        a._last_write_path = str(tmp_path / target)
        return "ok", [{"tool": "write_file"}]

    monkeypatch.setattr(a, "_file_op_flow", fake_file_op)
    note, trace, snapshot = await a._repair_browser_findings(audit, spec, tmp_path)
    # The pre-rewrite content is kept so `_guarded_repair` can undo the pass.
    assert snapshot == {"templates/products.html": "x"}

    # The file with the most findings is repaired; the second is REPORTED.
    assert [t for t, _ in calls] == ["templates/products.html"]
    assert "900px" in calls[0][1] and "dead" in calls[0][1]
    assert "rewrote templates/products.html" in note
    assert "1 more file(s)" in note and "max_browser_repairs" in note
    assert trace
    # An auto-repair must not hijack the follow-up edit target.
    assert a._last_write_path == "templates/original.html"


async def test_nothing_to_repair_costs_nothing(tmp_path, monkeypatch):
    a = AgentCore(session_id="pytest_audit_repair_none")

    async def boom(*args, **kwargs):
        raise AssertionError("must not generate")

    monkeypatch.setattr(a, "_file_op_flow", boom)
    assert await a._repair_browser_findings(None, None, tmp_path) == ("", [], {})
    assert await a._repair_browser_findings(SiteAudit(ran=False), None, tmp_path) == (
        "",
        [],
        {},
    )


async def test_repairs_can_be_turned_off_entirely(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "max_browser_repairs", 0)
    audit = SiteAudit(
        ran=True, findings=(Finding(kind="overflow", page="/", detail="900px"),)
    )
    a = AgentCore(session_id="pytest_audit_repair_off")

    async def boom(*args, **kwargs):
        raise AssertionError("must not generate")

    monkeypatch.setattr(a, "_file_op_flow", boom)
    assert await a._repair_browser_findings(audit, None, tmp_path) == ("", [], {})


def test_the_smoke_hook_runs_inside_the_server_window(tmp_path):
    """W5/W6 probe the app the smoke test already started. A second server would
    fight this one for :5000 and for app.db — the reason `--webapp` turns the
    smoke test off in the first place."""
    from app.agent import smoke

    (tmp_path / "srv.py").write_text(
        "import http.server, socketserver\n"
        "socketserver.TCPServer(('127.0.0.1', 8123), "
        "http.server.SimpleHTTPRequestHandler).serve_forever()\n",
        encoding="utf-8",
    )
    seen = []

    def hook(port):
        seen.append(port)
        return [ProbeCheck("browser: the console is clean", True, "", "browser")]

    result = smoke.run_smoke_test(
        tmp_path / "srv.py", tmp_path, ["/"], timeout=8.0, on_serving=hook
    )
    assert result.started and seen == [8123]
    assert any(c.owner == "browser" for c in result.checks)


def test_a_hook_that_raises_costs_only_its_own_observations(tmp_path):
    from app.agent import smoke

    (tmp_path / "srv.py").write_text(
        "import http.server, socketserver\n"
        "socketserver.TCPServer(('127.0.0.1', 8124), "
        "http.server.SimpleHTTPRequestHandler).serve_forever()\n",
        encoding="utf-8",
    )

    def hook(port):
        raise RuntimeError("chromium died")

    result = smoke.run_smoke_test(
        tmp_path / "srv.py", tmp_path, ["/"], timeout=8.0, on_serving=hook
    )
    assert result.started and result.responded


@pytest.mark.parametrize("name", ["AUDIT_SCRIPT", "CONTROLS_SCRIPT"])
def test_the_page_scripts_are_valid_javascript(name, tmp_path):
    """A syntax error here fails SILENTLY and in the worst direction.

    `Session.probe` swallows an `evaluate` failure into a debug log, so the key
    is simply absent from `probe.data` — and every function above then reports
    nothing, which reads as a clean page. `node --check` is the cheapest guard
    against shipping a check that can never fail. Skipped where node is absent,
    the same rule `verify._check_with_command` follows.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    source = getattr(pageaudit, name)
    path = tmp_path / f"{name}.js"
    path.write_text(f"const fn = {source};\nfn;\n", encoding="utf-8")
    proc = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_the_audit_check_shape_matches_what_smoke_reports():
    """`AuditCheck` is converted to `ProbeCheck` one-for-one; a drift here is a
    TypeError inside a best-effort hook, i.e. an invisible skip."""
    check = AuditCheck("browser: x", True, "detail")
    probe_check = ProbeCheck(check.label, check.ok, check.detail, owner="browser")
    assert "ok" in probe_check.line() and "browser: x" in probe_check.line()
    assert "detail" in probe_check.line()
