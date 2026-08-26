"""Point at the running page, edit the source behind it.

The premise, and the reason this is worth a module: a 7B is not bad at writing
twenty lines of markup. It is bad at deciding *where*. Everything in `core.py`
from `_extract_filename` through `_locate_named_file`, `_resolve_target_from_spec`
and `_last_write_fallback` exists to answer that one question by inference, and
each of those helpers carries a comment about the live build where its guess was
wrong. A click answers it by observation instead.

The second-order effect is the bigger one. Because the mapping ends at a span of
REAL TEXT taken out of the template on disk, the SEARCH half of the edit is
constructed here rather than quoted by the model — it cannot be misquoted,
cannot fail to match, and never falls through to a whole-file rewrite. The model
is left with the one job it is actually good at: writing the replacement.

Everything above `capture_click` is pure — text and dataclasses in, a target or
a refusal out — so the whole mapping is tested with no browser and no server.

The house rule runs through every step: **exactly one candidate, or decline**.
Editing the wrong element is silent and looks like the model ignoring the
request; declining says so and costs one sentence.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.agent.verify import mask_template_tags

logger = logging.getLogger(__name__)

#: Tags that are containers rather than things a person means to point at. A
#: click reported on one of these is walked up from, never edited directly.
_STRUCTURAL = frozenset({"html", "body", "main", "head"})

#: A route parameter, in either stack's spelling: `/products/<int:id>` (Flask)
#: and `/products/:id` (Express).
_PARAM_RE = re.compile(r"<[^>]+>|:[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class Element:
    """What the overlay reports about the thing the user clicked.

    Deliberately a plain record with no browser types in it: the mapping below
    is the part worth testing, and it must be reachable without launching
    anything.
    """

    tag: str = ""
    text: str = ""
    html: str = ""
    element_id: str = ""
    classes: tuple[str, ...] = ()
    url_path: str = "/"

    @classmethod
    def from_payload(cls, data: dict) -> "Element":
        """Build one from the overlay's JSON, tolerating anything missing."""
        classes = data.get("classes") or []
        if isinstance(classes, str):
            classes = classes.split()
        return cls(
            tag=str(data.get("tag") or "").lower(),
            text=" ".join(str(data.get("text") or "").split()),
            html=str(data.get("html") or ""),
            element_id=str(data.get("id") or ""),
            classes=tuple(str(c) for c in classes if str(c).strip()),
            url_path=str(data.get("path") or "/"),
        )


@dataclass(frozen=True)
class PointerTarget:
    """A resolved click: the file, the exact text to replace, and how we know."""

    path: str  # project-relative template path
    search: str  # verbatim source text — the SEARCH half, already verified
    how: str  # which rule matched, for the answer line
    region: str = ""  # the `{% block %}` it sits in, when the stack has them
    line: int = 0  # 1-based, for the answer line


@dataclass(frozen=True)
class Decline:
    """Why the click could not be resolved. Always says which step gave up."""

    reason: str


@dataclass
class _Candidate:
    start: int
    end: int
    how: str
    depth: int = field(default=0)


# ---------------------------------------------------------------------------
# URL → template
# ---------------------------------------------------------------------------


def _route_matches(route_path: str, url_path: str) -> bool:
    """Does a concrete URL match a route pattern, parameters and all."""
    pattern = _PARAM_RE.sub("__param__", route_path or "")
    parts = [p for p in pattern.split("/") if p]
    actual = [p for p in (url_path or "").split("?")[0].split("/") if p]
    if len(parts) != len(actual):
        return False
    return all(p == "__param__" or p == a for p, a in zip(parts, actual))


def template_for_path(graph, url_path: str) -> str | Decline:
    """Which template rendered this URL, or why we cannot say.

    A path is judged against EVERY matching route, not the first one — the
    union rule `check_links` already follows, and for the same reason:
    `/products/:id` also matches `/products/new`, so first-match answers a
    different question than the one asked. Routes that agree on the template
    are one answer; routes that disagree are an ambiguity, and an ambiguity is
    a refusal.
    """
    # A route stores the template NAME as its source writes it —
    # `render_template("products.html")`, `res.render("products")` — and
    # neither is a path. The graph owns that rule; re-deriving it here is how
    # two answers to one question start to differ.
    resolve = getattr(graph, "resolve_template", None)
    hits = {
        (resolve(template) if resolve else template) or template
        for method, path, _view, template in getattr(graph, "routes", ())
        if template and method.upper() in ("GET", "") and _route_matches(path, url_path)
    }
    if not hits:
        return Decline(
            f"No route in the project renders {url_path!r} — the page may be "
            "served by something this parser cannot read."
        )
    if len(hits) > 1:
        names = ", ".join(sorted(hits))
        return Decline(
            f"{url_path!r} is rendered by more than one template ({names}); "
            "which one the click came from cannot be told apart."
        )
    return hits.pop()


# ---------------------------------------------------------------------------
# Element → span of template source
# ---------------------------------------------------------------------------


def _open_tag_spans(scan: str, tag: str) -> list[tuple[int, int]]:
    """(start, end) of every opening `<tag ...>` in the MASKED text."""
    out = []
    for m in re.finditer(rf"<{re.escape(tag)}(\s[^>]*)?>", scan, re.IGNORECASE):
        out.append((m.start(), m.end()))
    return out


def _element_end(scan: str, tag: str, open_end: int) -> int | None:
    """Where the element opened at ``open_end`` closes, nesting counted.

    Returns None for an unbalanced document rather than a guessed end: splicing
    at a guessed boundary is how a repair pass corrupts a file, which is
    `mask_template_tags`' own lesson one layer down.
    """
    depth = 1
    pos = open_end
    token = re.compile(rf"<(/?){re.escape(tag)}(\s[^>]*)?>", re.IGNORECASE)
    while depth:
        m = token.search(scan, pos)
        if m is None:
            return None
        depth += -1 if m.group(1) else 1
        pos = m.end()
    return pos


def _void_element(tag: str) -> bool:
    return tag in {"img", "input", "br", "hr", "meta", "link", "source"}


def _candidates(source: str, element: Element) -> list[_Candidate]:
    """Every span of the template that could have produced this element.

    Three rules, tried in order of how much they prove, and the FIRST rule that
    yields exactly one candidate wins:

    1. the element's `id`, which a template writes literally;
    2. its full class list, when that combination appears once;
    3. its visible text, when the template contains it verbatim.

    Text is last because a template's text is often an expression — the value
    is in the database, not the file — so it proves the least. It is also the
    rule that catches headings and buttons, which is most of what anybody
    clicks.
    """
    scan = mask_template_tags(source)
    tag = element.tag or ""
    if not tag or tag in _STRUCTURAL:
        return []
    opens = _open_tag_spans(scan, tag)
    if not opens:
        return []

    def span_for(open_start: int, open_end: int, how: str) -> _Candidate | None:
        if _void_element(tag):
            return _Candidate(open_start, open_end, how)
        end = _element_end(scan, tag, open_end)
        if end is None:
            return None
        return _Candidate(open_start, end, how)

    # 1 — id
    if element.element_id:
        needle = re.compile(
            rf"""\bid\s*=\s*["']{re.escape(element.element_id)}["']""", re.IGNORECASE
        )
        hits = [
            c
            for s, e in opens
            if needle.search(scan[s:e])
            for c in [span_for(s, e, "id")]
            if c
        ]
        if len(hits) == 1:
            return hits

    # 2 — the full class list
    if element.classes:
        hits = []
        for s, e in opens:
            attr = re.search(r"""\bclass\s*=\s*["']([^"']*)["']""", scan[s:e], re.I)
            if not attr:
                continue
            present = set(attr.group(1).split())
            if set(element.classes) <= present:
                c = span_for(s, e, "class")
                if c:
                    hits.append(c)
        if len(hits) == 1:
            return hits

    # 3 — the visible text, verbatim
    text = element.text.strip()
    if text and not _void_element(tag):
        hits = []
        for s, e in opens:
            end = _element_end(scan, tag, e)
            if end is None:
                continue
            inner = " ".join(source[e:end].split())
            if text and text in inner:
                hits.append(_Candidate(s, end, "text"))
        if len(hits) == 1:
            return hits
        if len(hits) > 1:
            # Prefer the tightest one: a repeated label inside a loop names the
            # loop body, and the innermost span is the element the user meant.
            tightest = min(hits, key=lambda c: c.end - c.start)
            smallest = [
                h for h in hits if h.end - h.start == tightest.end - tightest.start
            ]
            if len(smallest) == 1:
                return smallest
    return []


def locate_in_template(source: str, element: Element) -> tuple[str, str, int] | Decline:
    """The verbatim source text behind a clicked element.

    Returns `(search_text, how, line_number)`. The text comes out of the REAL
    source, never the masked copy, so template expressions inside it survive
    into the SEARCH block exactly as written.
    """
    hits = _candidates(source, element)
    if not hits:
        return Decline(
            f"Found no unique <{element.tag or '?'}> in the template matching "
            "what was clicked. Nothing was changed — describe the change in "
            "words instead, or click something with an id or its own text."
        )
    hit = hits[0]
    search = source[hit.start : hit.end]
    if not search.strip():
        return Decline("The matched span was empty.")
    line = source.count("\n", 0, hit.start) + 1
    return search, hit.how, line


# ---------------------------------------------------------------------------
# The whole mapping
# ---------------------------------------------------------------------------


def resolve_element(
    root: str | Path, adapter, element: Element
) -> PointerTarget | Decline:
    """Click → (template, exact source text). Never raises; declines instead.

    Stack-agnostic: the route reader, the template directory and the block
    scoping all come from the adapter, so this works on a Jinja page and an EJS
    view without knowing which it is looking at.
    """
    base = Path(root)
    try:
        graph = adapter.build_template_graph(base)
    except Exception as exc:  # a graph we cannot build is a decline, not a crash
        logger.debug("pointer: template graph failed: %s", exc, exc_info=True)
        return Decline("Could not read this project's routes and templates.")

    template = template_for_path(graph, element.url_path)
    if isinstance(template, Decline):
        return template

    path = base / template
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return Decline(f"{template} is named by a route but could not be read.")

    located = locate_in_template(source, element)
    if isinstance(located, Decline):
        # A child template inherits its shell: a click on the nav belongs to the
        # layout, not to the page, and saying so is more useful than "not found".
        for parent in graph.parents(template):
            try:
                parent_source = (base / parent).read_text("utf-8", errors="replace")
            except Exception:
                continue
            found = locate_in_template(parent_source, element)
            if not isinstance(found, Decline):
                search, how, line = found
                return PointerTarget(parent, search, f"{how} (in the layout)", "", line)
        return located

    search, how, line = located
    region = ""
    try:
        block = adapter.template_edit_region(template, source)
        if block is not None and getattr(block, "body", "") and search in block.body:
            region = getattr(block, "name", "")
    except Exception:
        region = ""
    return PointerTarget(template, search, how, region, line)


# ---------------------------------------------------------------------------
# The browser half
# ---------------------------------------------------------------------------

#: Injected before any page script runs. Hover outlines what would be picked;
#: a click reports it and is swallowed, so a link never navigates out from
#: under the person choosing it.
OVERLAY_SCRIPT = r"""
(() => {
  const HL = '2px solid #e11d48';
  let last = null;
  const paint = (el, on) => {
    if (!el || !el.style) return;
    if (on) { el.dataset.coderOutline = el.style.outline || ''; el.style.outline = HL; }
    else { el.style.outline = el.dataset.coderOutline || ''; delete el.dataset.coderOutline; }
  };
  document.addEventListener('mouseover', (e) => {
    if (last !== e.target) { paint(last, false); last = e.target; paint(last, true); }
  }, true);
  document.addEventListener('click', (e) => {
    e.preventDefault(); e.stopPropagation();
    const el = e.target;
    paint(el, false);
    const payload = {
      tag: (el.tagName || '').toLowerCase(),
      id: el.id || '',
      classes: Array.from(el.classList || []),
      text: (el.innerText || '').trim().slice(0, 400),
      html: (el.outerHTML || '').slice(0, 4000),
      path: location.pathname + (location.search || ''),
    };
    if (window.__coderPick) window.__coderPick(payload);
  }, true);
  // `pointer-events:none` is load-bearing, not polish: without it the bar
  // swallows every click landing under it, and at the TOP of the page that is
  // the nav and the header — the two things people point at most. Measured
  // against a real browser, where Playwright reported the bar "intercepts
  // pointer events" and the click never reached the link. It sits at the
  // bottom for the same reason, so it covers the least valuable strip.
  const bar = document.createElement('div');
  bar.textContent = 'Coder: click the part of the page you want to change';
  bar.style.cssText = 'position:fixed;z-index:2147483647;left:0;right:0;bottom:0;'
    + 'pointer-events:none;'
    + 'background:#111;color:#fff;font:13px system-ui;padding:6px 10px;text-align:center';
  const attach = () => document.body && document.body.appendChild(bar);
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', attach);
  } else { attach(); }
})();
"""


def install_picker(page, sink: dict) -> None:
    """Wire the overlay and its callback onto a Playwright page.

    Split out of `capture_click` so the wiring can be exercised without a human
    at the keyboard: `capture_click` itself is a *wait*, and a test cannot click
    for you, but everything that decides whether a click becomes a usable
    payload — the binding name, the fields the overlay reports, the swallowed
    navigation — lives here and is driven by `tests/test_pointer.py` against a
    real browser and a real HTTP server.
    """
    page.expose_function("__coderPick", lambda data: sink.update(data or {}))
    page.add_init_script(OVERLAY_SCRIPT)


def capture_click(url: str, timeout: float = 120.0) -> Element | Decline:
    """Open the running app and wait for one click. Headed, on purpose.

    The rest of this codebase drives Chromium headless because it is measuring;
    this one is the user's own eyes, so the window is visible and the only
    thing it does is wait for them. Localhost only, checked before launch —
    `browser.py`'s rule, and it matters more here than there because this
    window is interactive.
    """
    from app.agent.browser import _is_local, available, install_hint

    if not available():
        return Decline(install_hint())
    if not _is_local(url):
        return Decline(f"Refusing to open {url}: only localhost is allowed.")

    from playwright.sync_api import sync_playwright

    picked: dict = {}
    driver = None
    browser = None
    try:
        driver = sync_playwright().start()
        browser = driver.chromium.launch(headless=False)
        page = browser.new_page()
        install_picker(page, picked)
        page.goto(url, wait_until="domcontentloaded")
        deadline = timeout * 1000
        waited = 0.0
        while not picked and waited < deadline:
            if page.is_closed():
                break
            page.wait_for_timeout(200)
            waited += 200
    except Exception as exc:
        logger.warning("pointer: browser session failed: %s", exc)
        return Decline(f"The browser could not be opened: {exc}")
    finally:
        for closer in (browser, driver):
            if closer is None:
                continue
            try:
                closer.close() if closer is browser else closer.stop()
            except Exception:
                pass
    if not picked:
        return Decline("Nothing was clicked — the window closed or timed out.")
    return Element.from_payload(picked)
