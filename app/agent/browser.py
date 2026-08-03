"""Render a generated page in a real browser, and observe it (Phase W4).

`docs/web-quality-plan.md`. Every check that existed before this module reads
**bytes**: `verify.py` parses a file, `references.py` scans hrefs, `smoke.py`
fetches with `urllib`. None of them lays out CSS or executes JavaScript, so an
entire class of failure was structurally invisible —

  * a table 900px wide inside a 390px viewport, so the page scrolls sideways;
  * `<button onclick="addToCart(1)">` with no `addToCart` anywhere, which
    parses, renders, and does nothing;
  * a `<script src>` that 404s at runtime even though the file exists, because
    Flask never routed it.

This module is the plumbing for seeing them: one navigation per page, and a
`PageProbe` carrying what that navigation revealed. It deliberately makes **no
judgements** — no thresholds, no pass/fail. Phases W5/W6 own those as pure
functions over a `PageProbe`, which is what lets them unit test with no browser
in the loop.

**The offline exception, stated plainly.** Playwright's Chromium download needs
the network once. Nothing else in Coder does, and no pure-Python renderer
implements modern CSS layout, so there is no offline-native alternative. It is
handled the way `runtime_probe` handles a missing Flask: `settings.browser_checks`
is **off** by default and `install_hint()` is loud and separate, so a machine
without a browser gets exactly the old behaviour and is *told* which checks did
not run. A skipped check must never read as a passing one.

**Sync API on purpose.** Like `smoke.py`, everything here is blocking and is
called from `core.py` with `asyncio.to_thread(...)`. Playwright's sync API
refuses to run inside a thread that owns a running event loop, and the worker
thread `to_thread` provides does not — so this is the shape that works. Do not
"fix" it into the async API without moving every call site.

**Localhost only.** `_is_local` refuses any other host before a browser is
launched. This code renders pages *and executes their JavaScript*; pointing it
at an arbitrary URL would be a general-purpose web client running untrusted
script, which is not what a smoke test is.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from typing import Iterator, Sequence
from urllib.parse import urlparse

from config.settings import settings

logger = logging.getLogger(__name__)

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}

# How long after `load` to keep collecting console/network events. Deferred
# scripts and a fetch fired on DOMContentLoaded both land after the load event,
# and a probe that stops at `load` misses exactly the errors it exists to find.
_SETTLE_MS = 250


# The default observation: cheap, generic page facts that need the DOM to be
# real. Anything requiring a judgement (is 4.5:1 enough? is this overflow
# meaningful?) belongs in W5, not here.
#
# `scrollWidth > innerWidth` is the horizontal-overflow measurement, and the
# element list names what caused it — a measurement with no culprit produces a
# repair instruction nobody can act on.
LAYOUT_SCRIPT = """
() => {
  const vw = window.innerWidth;
  const overflowing = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    if (r.right > vw + 1 || r.left < -1) {
      const id = el.id ? '#' + el.id : '';
      const cls = (el.className && typeof el.className === 'string')
        ? '.' + el.className.trim().split(/\\s+/).slice(0, 2).join('.')
        : '';
      overflowing.push({
        selector: el.tagName.toLowerCase() + id + cls,
        right: Math.round(r.right),
        width: Math.round(r.width),
      });
      if (overflowing.length >= 10) break;
    }
  }
  const main = document.querySelector('main') || document.body;
  return {
    viewport_width: vw,
    scroll_width: Math.round(document.documentElement.scrollWidth),
    body_scroll_width: Math.round(document.body ? document.body.scrollWidth : 0),
    overflowing: overflowing,
    main_text_length: (main.innerText || '').trim().length,
    has_nav: !!document.querySelector('nav'),
    link_count: document.querySelectorAll('a[href]').length,
    button_count: document.querySelectorAll(
      'button, input[type=submit], form'
    ).length,
    images_without_size: Array.from(document.querySelectorAll('img'))
      .filter((i) => !i.getAttribute('width') && !i.naturalWidth)
      .map((i) => i.getAttribute('src') || '')
      .slice(0, 10),
  };
}
"""


@dataclass(frozen=True)
class ConsoleMessage:
    """One console entry or uncaught exception from the page."""

    level: str
    text: str

    def is_error(self) -> bool:
        return self.level in ("error", "pageerror")


@dataclass(frozen=True)
class NetworkFailure:
    """A request the page made that did not come back usable.

    ``status`` is 0 when the request failed outright (connection refused, bad
    scheme) rather than answering with an HTTP error.
    """

    url: str
    status: int
    reason: str = ""


@dataclass
class PageProbe:
    """Everything one navigation revealed. Facts only — see the module docstring."""

    url: str
    width: int
    status: int | None = None
    title: str = ""
    console: tuple[ConsoleMessage, ...] = ()
    failed_requests: tuple[NetworkFailure, ...] = ()
    data: dict = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        """Did the page load at all? Says nothing about whether it is correct."""
        return not self.error and self.status is not None and self.status < 400

    def errors(self) -> tuple[ConsoleMessage, ...]:
        return tuple(m for m in self.console if m.is_error())


def available() -> bool:
    """Is a usable Playwright + Chromium actually installed?

    Import-only: it does not launch a browser, so it is cheap enough to call on
    every turn. A present package with no downloaded browser binary still fails
    at launch, which `probe_page` reports as an error rather than raising.
    """
    try:
        import playwright.sync_api  # noqa: F401
    except Exception:
        return False
    return True


def install_hint() -> str:
    """What to run to make browser checks work. Empty when they already do.

    Kept SEPARATE from any generation instruction, exactly like
    `Stack.install_hint`: this text is for the user, and folding it into a
    prompt would have the model try to satisfy it in code.
    """
    if available():
        return ""
    return (
        "browser checks skipped — no headless browser available. Install once "
        "with `pip install playwright && python -m playwright install chromium` "
        "(needs the network for that one command), then set BROWSER_CHECKS=true."
    )


def _is_local(url: str) -> bool:
    try:
        parsed = urlparse(url or "")
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    return (parsed.hostname or "").lower() in _LOCAL_HOSTS


class Session:
    """A live browser. Reuse one across pages — a launch costs ~0.5s each time."""

    def __init__(self, browser, timeout: float) -> None:
        self._browser = browser
        self._timeout_ms = max(1000, int(timeout * 1000))

    @contextlib.contextmanager
    def _page(self, width: int) -> Iterator[tuple[object, list, list]]:
        """A page wired to collect console and network events for its lifetime."""
        console: list[ConsoleMessage] = []
        failures: list[NetworkFailure] = []
        page = self._browser.new_page(viewport={"width": int(width), "height": 900})
        try:
            page.on(
                "console",
                lambda msg: console.append(
                    ConsoleMessage(level=str(msg.type), text=str(msg.text)[:400])
                ),
            )
            page.on(
                "pageerror",
                lambda exc: console.append(
                    ConsoleMessage(level="pageerror", text=str(exc)[:400])
                ),
            )
            page.on(
                "requestfailed",
                lambda req: failures.append(
                    NetworkFailure(
                        url=str(req.url)[:300],
                        status=0,
                        reason=str(getattr(req.failure, "error_text", "") or "failed"),
                    )
                ),
            )
            page.on(
                "response",
                lambda resp: (
                    failures.append(
                        NetworkFailure(url=str(resp.url)[:300], status=int(resp.status))
                    )
                    if resp.status >= 400
                    else None
                ),
            )
            page.set_default_timeout(self._timeout_ms)
            yield page, console, failures
        finally:
            with contextlib.suppress(Exception):
                page.close()

    def probe(
        self, url: str, width: int = 1280, scripts: dict[str, str] | None = None
    ) -> PageProbe:
        """Navigate once and report what happened.

        Never raises: a navigation that times out or a page that crashes comes
        back as a `PageProbe` carrying `error`, because a probe failure must
        cost the observation, never the turn.
        """
        if not _is_local(url):
            return PageProbe(url=url, width=width, error="refused: not a localhost URL")
        try:
            with self._page(width) as (page, console, failures):
                status: int | None = None
                try:
                    response = page.goto(url, wait_until="load")
                    status = int(response.status) if response is not None else None
                except Exception as exc:
                    return PageProbe(
                        url=url,
                        width=width,
                        console=tuple(console),
                        failed_requests=tuple(failures),
                        error=f"navigation failed: {exc}"[:300],
                    )
                # Let deferred scripts and load-time fetches finish; without
                # this the console is read before the errors are in it.
                with contextlib.suppress(Exception):
                    page.wait_for_timeout(_SETTLE_MS)

                title = ""
                with contextlib.suppress(Exception):
                    title = str(page.title())[:200]

                data: dict = {}
                for name, source in (scripts or {"layout": LAYOUT_SCRIPT}).items():
                    try:
                        data[name] = page.evaluate(source)
                    except Exception as exc:
                        logger.debug("browser: script %s failed: %s", name, exc)
                return PageProbe(
                    url=url,
                    width=width,
                    status=status,
                    title=title,
                    console=tuple(console),
                    failed_requests=tuple(failures),
                    data=data,
                )
        except Exception as exc:
            return PageProbe(url=url, width=width, error=f"browser error: {exc}"[:300])

    def screenshot(self, url: str, width: int = 1280, full_page: bool = True) -> bytes:
        """A PNG of the page, or b"" on any failure.

        Bytes rather than a path: the only consumer is the vision model (W7),
        which base64s them, and writing a temp file into the user's project to
        immediately read it back would be litter.
        """
        if not _is_local(url):
            return b""
        try:
            with self._page(width) as (page, _console, _failures):
                page.goto(url, wait_until="load")
                with contextlib.suppress(Exception):
                    page.wait_for_timeout(_SETTLE_MS)
                return bytes(page.screenshot(full_page=full_page, type="png"))
        except Exception as exc:
            logger.debug("browser: screenshot of %s failed: %s", url, exc)
            return b""

    def click(
        self, url: str, selector: str, width: int = 1280
    ) -> tuple[PageProbe, dict]:
        """Load a page, click one thing, and report what changed.

        The primitive behind W6's dead-button check. ``changed`` reports the
        observable difference — URL, body text length, DOM size, console
        errors — and deliberately not a verdict: "nothing changed" is evidence,
        and whether that is a bug depends on what the control claimed to do.

        Both lengths are collected because they answer different questions:
        `innerText` moves when content appears or a hidden panel opens, and
        `innerHTML` moves when a handler only toggles a class or an attribute.
        A check on the text alone would call a working tab switch dead.
        """
        if not _is_local(url):
            return (
                PageProbe(url=url, width=width, error="refused: not a localhost URL"),
                {},
            )
        try:
            with self._page(width) as (page, console, failures):
                page.goto(url, wait_until="load")
                before_url = str(page.url)
                before_len = 0
                before_html = 0
                with contextlib.suppress(Exception):
                    before_len = int(
                        page.evaluate("() => (document.body.innerText || '').length")
                    )
                with contextlib.suppress(Exception):
                    before_html = int(
                        page.evaluate("() => (document.body.innerHTML || '').length")
                    )
                clicked = False
                try:
                    page.click(selector, timeout=min(self._timeout_ms, 3000))
                    clicked = True
                except Exception as exc:
                    logger.debug("browser: click %s failed: %s", selector, exc)
                with contextlib.suppress(Exception):
                    page.wait_for_timeout(_SETTLE_MS)

                after_url = str(page.url)
                after_len = before_len
                after_html = before_html
                with contextlib.suppress(Exception):
                    after_len = int(
                        page.evaluate("() => (document.body.innerText || '').length")
                    )
                with contextlib.suppress(Exception):
                    after_html = int(
                        page.evaluate("() => (document.body.innerHTML || '').length")
                    )
                probe = PageProbe(
                    url=after_url,
                    width=width,
                    status=None,
                    console=tuple(console),
                    failed_requests=tuple(failures),
                )
                return probe, {
                    "clicked": clicked,
                    "selector": selector,
                    "url_changed": after_url != before_url,
                    "text_length_before": before_len,
                    "text_length_after": after_len,
                    "html_length_before": before_html,
                    "html_length_after": after_html,
                }
        except Exception as exc:
            return (
                PageProbe(url=url, width=width, error=f"browser error: {exc}"[:300]),
                {},
            )


@contextlib.contextmanager
def browser_session(timeout: float | None = None) -> Iterator[Session | None]:
    """One headless browser for the duration of the block, or None.

    Yields **None** rather than raising when Playwright or its Chromium build is
    missing, so every caller degrades to "this check did not run" with one
    `if session is None` — the same shape `vision.py` uses for a missing model.
    """
    if not available():
        yield None
        return
    from playwright.sync_api import sync_playwright  # local: keeps import cheap

    limit = settings.browser_timeout if timeout is None else timeout
    driver = None
    browser = None
    try:
        driver = sync_playwright().start()
        browser = driver.chromium.launch(headless=True)
    except Exception as exc:
        # The common case is the package installed but `playwright install`
        # never run. Nothing to do about it here — report and carry on.
        logger.warning("browser: could not launch chromium: %s", exc)
        with contextlib.suppress(Exception):
            if browser is not None:
                browser.close()
        with contextlib.suppress(Exception):
            if driver is not None:
                driver.stop()
        yield None
        return
    try:
        yield Session(browser, limit)
    finally:
        # Both, always: a leaked Chromium is worse than a leaked Flask, because
        # nothing prints its port and nobody goes looking for it.
        with contextlib.suppress(Exception):
            browser.close()
        with contextlib.suppress(Exception):
            driver.stop()


def probe_pages(
    urls: Sequence[str],
    widths: Sequence[int] | None = None,
    scripts: dict[str, str] | None = None,
    timeout: float | None = None,
) -> list[PageProbe]:
    """Probe several pages at several widths on ONE browser launch.

    Returns [] when no browser is available — indistinguishable, by design, from
    "nothing to probe". Callers that need to tell those apart ask
    `available()` / `install_hint()`, which is how the skip gets reported.

    Capped at `settings.browser_max_pages`; what it drops is the caller's to
    report, per the rule that a cap which hides is a bug.
    """
    selected = [u for u in urls if u][: max(1, settings.browser_max_pages)]
    if not selected:
        return []
    sizes = list(widths or settings.browser_widths) or [1280]
    out: list[PageProbe] = []
    with browser_session(timeout) as session:
        if session is None:
            return []
        for url in selected:
            for width in sizes:
                out.append(session.probe(url, width=width, scripts=scripts))
    return out
