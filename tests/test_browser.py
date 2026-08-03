"""The browser layer (Phase W4, docs/web-quality-plan.md).

Two halves, deliberately:

* Everything that does NOT need a browser — the localhost jail, the
  availability/skip reporting, the `PageProbe` accessors — is tested offline and
  runs in the default suite. This is most of the module's decision-making.
* The driver itself is gated on `importorskip("playwright")` **and** on Chromium
  actually being installed, like the git tool tests. Playwright's browser
  download needs the network once, so the default suite must not depend on it.

The gated test is still worth having: a probe that has only ever been run
against a fake has not been tested, which is the rule
`tests/test_functional_probe.py` follows by starting a real Flask subprocess.
"""

import contextlib
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.agent import browser
from app.agent.browser import (
    LAYOUT_SCRIPT,
    ConsoleMessage,
    NetworkFailure,
    PageProbe,
    available,
    browser_session,
    install_hint,
    probe_pages,
)
from config.settings import settings

# --- the localhost jail -----------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:5000/",
        "http://localhost:5000/products",
        "http://[::1]:8000/",
    ],
)
def test_local_urls_are_allowed(url):
    assert browser._is_local(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/",
        "http://evil.test/",
        "file:///etc/passwd",
        "data:text/html,<h1>x</h1>",
        "",
        "not a url",
    ],
)
def test_everything_else_is_refused(url):
    """This code renders pages AND executes their JavaScript. Pointed at an
    arbitrary URL it would be a general-purpose web client running untrusted
    script, which is not what a smoke test is."""
    assert not browser._is_local(url)


def test_a_refused_url_never_launches_a_browser(monkeypatch):
    """The jail must come before the launch, not after it."""
    launched = []
    monkeypatch.setattr(browser, "browser_session", lambda *a, **k: launched.append(1))
    session = browser.Session(browser=None, timeout=1.0)
    probe = session.probe("https://example.com/")
    assert not probe.ok
    assert "localhost" in probe.error
    assert launched == []


# --- reporting a skip, rather than reporting a pass -------------------------


def test_install_hint_is_empty_exactly_when_the_browser_works():
    assert bool(install_hint()) is not available()


def test_install_hint_names_the_command(monkeypatch):
    monkeypatch.setattr(browser, "available", lambda: False)
    hint = install_hint()
    assert "playwright install chromium" in hint
    assert "BROWSER_CHECKS" in hint


def test_session_is_none_when_unavailable(monkeypatch):
    """Every caller degrades with one `if session is None` — the shape
    `vision.py` uses for a missing model."""
    monkeypatch.setattr(browser, "available", lambda: False)
    with browser_session() as session:
        assert session is None


def test_probe_pages_returns_nothing_when_unavailable(monkeypatch):
    monkeypatch.setattr(browser, "available", lambda: False)
    assert probe_pages(["http://127.0.0.1:5000/"]) == []


def test_probe_pages_respects_the_page_cap(monkeypatch):
    monkeypatch.setattr(settings, "browser_max_pages", 2)
    seen = []

    class _FakeSession:
        def probe(self, url, width=1280, scripts=None):
            seen.append((url, width))
            return PageProbe(url=url, width=width, status=200)

    @contextlib.contextmanager
    def _fake_session(timeout=None):
        yield _FakeSession()

    monkeypatch.setattr(browser, "browser_session", _fake_session)
    urls = [f"http://127.0.0.1:5000/p{i}" for i in range(6)]
    out = probe_pages(urls, widths=[1280])
    assert len(out) == 2 and len(seen) == 2


def test_probe_pages_visits_every_width(monkeypatch):
    seen = []

    class _FakeSession:
        def probe(self, url, width=1280, scripts=None):
            seen.append(width)
            return PageProbe(url=url, width=width, status=200)

    @contextlib.contextmanager
    def _fake_session(timeout=None):
        yield _FakeSession()

    monkeypatch.setattr(browser, "browser_session", _fake_session)
    probe_pages(["http://127.0.0.1:5000/"], widths=[1280, 390])
    assert seen == [1280, 390]


# --- PageProbe carries facts, not verdicts ----------------------------------


def test_probe_ok_requires_a_real_response():
    assert PageProbe(url="u", width=1, status=200).ok
    assert not PageProbe(url="u", width=1, status=500).ok
    assert not PageProbe(url="u", width=1, status=None).ok
    assert not PageProbe(url="u", width=1, status=200, error="boom").ok


def test_probe_errors_filters_console_noise():
    probe = PageProbe(
        url="u",
        width=1,
        console=(
            ConsoleMessage("log", "hello"),
            ConsoleMessage("warning", "deprecated"),
            ConsoleMessage("error", "boom"),
            ConsoleMessage("pageerror", "ReferenceError: x is not defined"),
        ),
    )
    assert [m.text for m in probe.errors()] == [
        "boom",
        "ReferenceError: x is not defined",
    ]


def test_network_failure_zero_status_means_the_request_never_landed():
    assert NetworkFailure(url="u", status=0, reason="refused").status == 0


def test_layout_script_reports_the_measurements_w5_needs():
    for key in (
        "viewport_width",
        "scroll_width",
        "overflowing",
        "main_text_length",
        "images_without_size",
    ):
        assert key in LAYOUT_SCRIPT


def test_browser_checks_ship_off():
    """The offline rule's one exception is opt-in. A machine that never runs
    `playwright install` must behave exactly as it did before Phase W4."""
    assert settings.browser_checks is False
    assert 390 in settings.browser_widths


# --- the real driver (needs a browser) --------------------------------------


@pytest.fixture
def local_site(tmp_path):
    """A real HTTP server on localhost serving one deliberately broken page."""
    (tmp_path / "index.html").write_text(
        "<!doctype html><html><head>"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Broken</title></head><body>"
        "<main><div id='wide' style='width:2000px'>too wide</div></main>"
        '<script src="missing.js"></script>'
        "<script>notDefinedAnywhere();</script>"
        "</body></html>",
        encoding="utf-8",
    )
    handler = partial(SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/index.html"
    finally:
        server.shutdown()
        server.server_close()


def test_probe_sees_what_bytes_cannot(local_site):
    """One navigation, and all three of W4's observations at once.

    Every one of these is invisible to `verify.py` and to `smoke.py`: the file
    parses, the tags balance, and `urllib` fetches it with a 200.
    """
    pytest.importorskip("playwright")
    with browser_session(timeout=15.0) as session:
        if session is None:
            pytest.skip("chromium not installed (`python -m playwright install`)")
        probe = session.probe(local_site, width=390)

    assert probe.ok and probe.status == 200
    assert probe.title == "Broken"

    # 1. Layout: the page is wider than the phone viewport, and we can say what
    #    made it wide.
    layout = probe.data["layout"]
    assert layout["viewport_width"] == 390
    assert layout["scroll_width"] > 390
    assert any("wide" in item["selector"] for item in layout["overflowing"])

    # 2. JavaScript actually ran, and threw.
    assert any("notDefinedAnywhere" in m.text for m in probe.errors())

    # 3. A referenced asset 404'd at runtime — the runtime complement to
    #    references.py's static scan.
    assert any(
        f.url.endswith("missing.js") and f.status == 404 for f in probe.failed_requests
    )


def test_screenshot_returns_png_bytes(local_site):
    pytest.importorskip("playwright")
    with browser_session(timeout=15.0) as session:
        if session is None:
            pytest.skip("chromium not installed (`python -m playwright install`)")
        png = session.screenshot(local_site, width=390)
    assert png.startswith(b"\x89PNG")
