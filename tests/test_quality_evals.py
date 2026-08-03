"""The "does it LOOK right" eval checks (Phase W10, docs/web-quality-plan.md).

Fully offline: the browser pass is stubbed, so what these test is the CHECK —
what it asserts, what it reports, and above all what it does when it could not
look at anything. The suite measured *works* well and *looks* not at all; a
check that silently passes on a machine with no browser would have made that
worse rather than better, because the score would then claim something nobody
verified.
"""

from pathlib import Path

import pytest

from app.agent.pageaudit import Finding, SiteAudit
from evals import checks as ev
from evals.checks import (
    contrast_ok,
    every_control_does_something,
    nav_on_every_page,
    no_console_errors,
    no_horizontal_overflow,
    style_stable_across_turns,
)
from evals.harness import CheckContext
from evals.tasks import WEBAPP_TASKS

BROWSER_CHECKS = (
    no_horizontal_overflow,
    no_console_errors,
    every_control_does_something,
    contrast_ok,
)


def _ctx(tmp_path) -> CheckContext:
    return CheckContext(answer="", trace=[], workdir=Path(tmp_path))


def _report(audit=None, styles=None, error=""):
    return ev._BrowserReport(audit=audit, styles=styles, error=error)


def _clean_audit(**kw):
    base = dict(ran=True, pages=("/", "/products"), widths=(1280, 390))
    base.update(kw)
    return SiteAudit(**base)


def _style(**kw):
    base = {
        "font_family": "system-ui",
        "background": "rgb(255, 255, 255)",
        "color": "rgb(17, 17, 17)",
        "local_styles": 0,
        "has_nav": True,
        "nav_links": ["Home", "Products"],
        "classes": ["card", "grid", "site-header"],
    }
    base.update(kw)
    return base


# --- a check that could not run is never a pass -----------------------------


@pytest.mark.parametrize("factory", BROWSER_CHECKS + (nav_on_every_page,))
def test_no_browser_fails_loudly_with_the_install_command(tmp_path, factory):
    """A suite that scores 100% without having rendered anything is worse than
    one that scores honestly. The detail has to say what to install."""
    ctx = _ctx(tmp_path)
    ctx.browser = _report(error="browser checks skipped — playwright install chromium")
    ok, detail = factory()(ctx)
    assert ok is False and "playwright install chromium" in detail


@pytest.mark.parametrize("factory", BROWSER_CHECKS)
def test_a_browser_that_opened_nothing_is_not_a_pass(tmp_path, factory):
    ctx = _ctx(tmp_path)
    ctx.browser = _report(
        audit=SiteAudit(
            ran=True, pages=("/",), widths=(390,), unobserved=("/ at 390px",)
        )
    )
    ok, detail = factory()(ctx)
    assert ok is False and "no page loaded" in detail


def test_the_app_is_started_once_for_every_check(tmp_path, monkeypatch):
    """Five checks, one server launch and one browser — the memo on the
    CheckContext is what makes the task affordable."""
    calls = []

    def fake_report(ctx):
        calls.append(1)
        return ev._BrowserReport(audit=_clean_audit(), styles={"/": _style()})

    monkeypatch.setattr(ev, "_browser_report", fake_report)
    ctx = _ctx(tmp_path)
    for factory in BROWSER_CHECKS:
        factory()(ctx)
    assert len(calls) == len(BROWSER_CHECKS)  # each check asks…
    # …and the real one memoizes, which is what the next test pins.


def test_the_report_is_memoized_on_the_context(tmp_path):
    ctx = _ctx(tmp_path)
    report = _report(audit=_clean_audit())
    ctx.browser = report
    assert ev._browser_report(ctx) is report


# --- what each check asserts ------------------------------------------------


def test_overflow_is_reported_with_the_page_and_the_measurement(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.browser = _report(
        audit=_clean_audit(
            findings=(
                Finding(
                    kind="overflow",
                    page="/products",
                    detail="the page is 900px wide in a 390px viewport",
                    width=390,
                ),
            )
        )
    )
    ok, detail = no_horizontal_overflow()(ctx)
    assert ok is False
    assert "/products at 390px" in detail and "900px" in detail


def test_a_clean_site_passes_every_browser_check(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.browser = _report(audit=_clean_audit(controls_clicked=3))
    for factory in BROWSER_CHECKS:
        ok, detail = factory()(ctx)
        assert ok, detail
    assert "3 control(s) clicked" in every_control_does_something()(ctx)[1]


def test_console_and_network_failures_both_count(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.browser = _report(
        audit=_clean_audit(
            findings=(Finding(kind="network", page="/", detail="an asset 404s"),)
        )
    )
    assert no_console_errors()(ctx)[0] is False


def test_a_skipped_control_is_reported_not_counted_as_a_pass(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.browser = _report(
        audit=_clean_audit(
            controls_clicked=1, controls_skipped=("/: Delete: destructive",)
        )
    )
    ok, detail = every_control_does_something()(ctx)
    assert ok and "1 skipped" in detail


def test_a_dead_button_fails(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.browser = _report(
        audit=_clean_audit(
            controls_clicked=2,
            findings=(
                Finding(
                    kind="dead-control",
                    page="/",
                    detail="clicking 'Add to cart' changes nothing",
                ),
            ),
        )
    )
    assert every_control_does_something()(ctx)[0] is False


def test_a_warning_never_fails_a_check(tmp_path):
    """`image-size` is a warning, and W5 keeps it out of the repair loop for the
    same reason it must stay out of the score."""
    ctx = _ctx(tmp_path)
    ctx.browser = _report(
        audit=_clean_audit(
            findings=(
                Finding(
                    kind="dead-control",
                    page="/",
                    detail="could not be clicked",
                    severity="warning",
                ),
            )
        )
    )
    assert every_control_does_something()(ctx)[0] is True


# --- the nav, on every page -------------------------------------------------


def test_the_same_nav_everywhere_passes(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.browser = _report(styles={"/": _style(), "/products": _style()})
    ok, detail = nav_on_every_page()(ctx)
    assert ok and "all 2 page(s)" in detail


def test_a_page_with_no_nav_fails(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.browser = _report(
        styles={"/": _style(), "/cart": _style(has_nav=False, nav_links=[])}
    )
    ok, detail = nav_on_every_page()(ctx)
    assert ok is False and "/cart" in detail


def test_a_drifting_nav_fails(tmp_path):
    """The 'every page has a different navbar' bug, as a number."""
    ctx = _ctx(tmp_path)
    ctx.browser = _report(
        styles={
            "/": _style(nav_links=["Home", "Products"]),
            "/cart": _style(nav_links=["Home", "Shop", "Basket"]),
        }
    )
    ok, detail = nav_on_every_page()(ctx)
    assert ok is False and "differs" in detail


# --- the headline: turn 3 must not restyle turn 1 ---------------------------


def test_one_theme_across_pages_passes(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.browser = _report(
        styles={"/": _style(), "/products": _style(), "/cart": _style()}
    )
    ok, detail = style_stable_across_turns()(ctx)
    assert ok and "3 page(s)" in detail


def test_a_page_that_restyled_itself_fails(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.browser = _report(
        styles={
            "/": _style(),
            "/cart": _style(font_family="Comic Sans MS", background="rgb(0, 0, 0)"),
        }
    )
    ok, detail = style_stable_across_turns()(ctx)
    assert ok is False and "font" in detail


def test_a_page_local_style_block_is_opting_out(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.browser = _report(styles={"/": _style(), "/cart": _style(local_styles=1)})
    ok, detail = style_stable_across_turns()(ctx)
    assert ok is False and "/cart" in detail


def test_a_page_built_from_no_shipped_component_fails(tmp_path):
    """W1 shipped the components so every page would use the same table. A page
    with none of them has its own design system."""
    ctx = _ctx(tmp_path)
    ctx.browser = _report(
        styles={"/": _style(), "/cart": _style(classes=["my-cart", "row-thing"])}
    )
    ok, detail = style_stable_across_turns()(ctx)
    assert ok is False and "component class" in detail


def test_one_page_is_not_enough_to_claim_stability(tmp_path):
    """Comparing a page against itself proves nothing, and saying so beats
    reporting a pass nobody earned."""
    ctx = _ctx(tmp_path)
    ctx.browser = _report(styles={"/": _style()})
    ok, detail = style_stable_across_turns()(ctx)
    assert ok is False and "fewer than two pages" in detail


# --- the tasks --------------------------------------------------------------


def test_the_quality_tasks_are_in_the_webapp_suite():
    ids = {t.id for t in WEBAPP_TASKS}
    assert {"web_quality_build", "web_quality_stable"} <= ids


def test_the_stability_task_is_multi_turn():
    """`style_stable_across_turns` needs turns to be stable across."""
    task = next(t for t in WEBAPP_TASKS if t.id == "web_quality_stable")
    assert len(task.turns()) >= 3


def test_the_quality_checks_name_no_route_or_selector():
    """They read the project's own spec, so a request whose schema the task
    author could not have guessed is measured just as strictly as this one.

    Pinned by construction: these checks close over NOTHING, while the older
    `app_serves(["/"])` closes over a route the author had to know in advance.
    """
    task = next(t for t in WEBAPP_TASKS if t.id == "web_quality_build")
    assert all(check.__closure__ is None for check in task.checks)
    assert ev.app_serves(["/"]).__closure__ is not None  # the contrast


def test_spec_routes_always_include_the_home_page(tmp_path):
    assert ev._spec_routes(_ctx(tmp_path)) == ["/"]
