"""What the rendered page actually looks like, and whether its controls work.

Phases W5 (deterministic layout audit) and W6 (runtime JS + the dead-button
probe) of `docs/web-quality-plan.md`. `browser.py` opens the page and reports
**facts**; this module is where those facts become findings, and it is split the
same way for the same reason:

* Everything that decides anything is a **pure function over a `PageProbe`** —
  `horizontal_overflow`, `low_contrast`, `console_findings`, `triage_controls`,
  `click_findings`. They unit test with a hand-built probe and no browser in the
  loop, which is what keeps the default suite offline.
* `audit_site()` is the only part that drives a browser, and it is a thin loop:
  one session, one navigation per page per width, then one navigation per
  control it decided to click.

**A false failure is worse than no check.** This is the lesson `functional_probe`
step 3 learned the expensive way — probing only entities named in `reads`
reported a failure for a row that had persisted, and the repair loop was sent to
rewrite working code. Every check here reports a *measurement* and anything
ambiguous passes. Three places where that rule visibly cost a check:

* **An element past the right edge is only a defect if the PAGE scrolls.** The
  design system's answer to a wide table is `.table-wrap`, a horizontal scroll
  container — the table inside it is *meant* to extend past the viewport.
  Reporting "clipped element" would fail the very component W1 shipped to fix
  the problem, so overflow is judged on `scrollWidth` and the offending elements
  are named only as the culprit.
* **Contrast is skipped, not failed, whenever the backdrop is unknowable** — a
  background image, a translucent layer, an ancestor chain that never resolves.
* **A form is never submitted to prove its button works.** Native validation
  blocks a submit with an empty required field, so "nothing changed" would be a
  false failure on a *correct* form; and a POST that did go through would insert
  a second row behind the checks that assert against the seeded data.
  `functional_probe` already posts to every write endpoint with real values and
  requires the value to come back, so what the browser adds is the control, and
  that is all it probes. Skipped controls are **reported as skipped** — a check
  that did not run must never read as one that passed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlparse

from app.agent.browser import LAYOUT_SCRIPT, PageProbe, browser_session
from config.settings import settings

logger = logging.getLogger(__name__)

# Slack on the overflow measurement. Sub-pixel layout rounding and a 1px border
# on a full-bleed element both produce a scrollWidth a hair over the viewport
# with nothing actually cut off; a phone-width table is hundreds of pixels over.
_OVERFLOW_SLACK_PX = 4

# WCAG AA. The large-text allowance is real and applies to headings, which are
# exactly the elements a designer legitimately renders in a lighter tint.
_CONTRAST_MIN = 4.5
_CONTRAST_MIN_LARGE = 3.0

# Never reported as a missing asset: the scaffold ships no favicon, every browser
# asks for one anyway, and "add a favicon" is not a defect worth a repair pass.
_IGNORED_REQUESTS = ("/favicon.ico",)

# Console noise that duplicates a network failure we already report with the URL
# and the status. Reporting both says the same thing twice in a repair prompt.
_RESOURCE_NOISE = ("failed to load resource",)

# Accessible names that make a control destructive. Clicking one that WORKS
# empties the seeded data the later probes assert against, so a working button
# would break the checks around it — the one case where success is the problem.
_DESTRUCTIVE_WORDS = (
    "delete",
    "remove",
    "clear",
    "drop",
    "destroy",
    "reset",
    "log out",
    "logout",
    "sign out",
    "signout",
)


# --- the observations W5/W6 need, gathered in the page ----------------------
# Raw numbers only, per `browser.py`'s rule: no thresholds live in JavaScript.
# The `ratio < 7` filter below is a TRANSPORT bound, not a verdict — it keeps a
# 300-element page from returning 300 rows; the pass/fail line is applied in
# Python by `low_contrast`, where it can be tested.

_JS_HELPERS = r"""
  const label = (el) => {
    const id = el.id ? '#' + el.id : '';
    const cls = (el.className && typeof el.className === 'string')
      ? '.' + el.className.trim().split(/\s+/).slice(0, 2).join('.')
      : '';
    return el.tagName.toLowerCase() + id + cls;
  };
  const uniquePath = (el) => {
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1) {
      const parent = node.parentElement;
      if (!parent) break;
      const index = Array.prototype.indexOf.call(parent.children, node) + 1;
      parts.unshift(node.tagName.toLowerCase() + ':nth-child(' + index + ')');
      node = parent;
    }
    return parts.join(' > ');
  };
"""

AUDIT_SCRIPT = (
    r"""
() => {
"""
    + _JS_HELPERS
    + r"""
  const parseColor = (value) => {
    const m = /rgba?\(([^)]+)\)/.exec(value || '');
    if (!m) return null;
    const parts = m[1].split(',').map((x) => parseFloat(x));
    if (parts.length < 3 || parts.some((n) => isNaN(n))) return null;
    return {
      r: parts[0], g: parts[1], b: parts[2],
      a: parts.length > 3 ? parts[3] : 1,
    };
  };
  const channel = (v) => {
    const s = v / 255;
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
  };
  const luminance = (c) =>
    0.2126 * channel(c.r) + 0.7152 * channel(c.g) + 0.0722 * channel(c.b);
  const contrast = (a, b) => {
    const la = luminance(a), lb = luminance(b);
    const hi = Math.max(la, lb), lo = Math.min(la, lb);
    return (hi + 0.05) / (lo + 0.05);
  };
  // The colour actually behind the text, or null when it cannot be known —
  // a background image, or any translucent layer on the way up. Null is a
  // SKIP, never a failure.
  const backdrop = (el) => {
    let node = el;
    while (node && node.nodeType === 1) {
      const style = getComputedStyle(node);
      if (style.backgroundImage && style.backgroundImage !== 'none') return null;
      const color = parseColor(style.backgroundColor);
      if (color && color.a >= 0.999) return color;
      if (color && color.a > 0.001) return null;
      node = node.parentElement;
    }
    return { r: 255, g: 255, b: 255, a: 1 };  // the browser's own canvas
  };

  const skip = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'SVG']);
  const samples = [];
  let measured = 0, skipped = 0, seen = 0;
  for (const el of document.querySelectorAll('body *')) {
    if (seen++ > 600) break;
    if (skip.has(el.tagName)) continue;
    let own = '';
    for (const node of el.childNodes) {
      if (node.nodeType === 3) own += node.nodeValue;
    }
    own = own.trim();
    if (own.length < 2) continue;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    if (parseFloat(style.opacity || '1') < 0.999) { skipped++; continue; }
    const fg = parseColor(style.color);
    const bg = backdrop(el);
    if (!fg || !bg || fg.a < 0.999) { skipped++; continue; }
    measured++;
    const size = parseFloat(style.fontSize || '16') || 16;
    const weight = parseInt(style.fontWeight || '400', 10) || 400;
    const ratio = contrast(fg, bg);
    if (ratio < 7 && samples.length < 12) {
      samples.push({
        selector: label(el),
        path: uniquePath(el),
        ratio: Math.round(ratio * 100) / 100,
        font_size: size,
        bold: weight >= 700,
        text: own.slice(0, 60),
      });
    }
  }

  const main = document.querySelector('main') || document.body;
  return {
    contrast: samples,
    contrast_measured: measured,
    contrast_skipped: skipped,
    main_text_length: (main.innerText || '').trim().length,
    main_child_count: main.children.length,
    main_media_count: main.querySelectorAll('img, svg, canvas, video, iframe').length,
    form_count: document.querySelectorAll('form').length,
    forms_without_submit: Array.from(document.querySelectorAll('form'))
      .filter((f) => !f.querySelector(
        'button:not([type=button]):not([type=reset]), input[type=submit], input[type=image]'
      ))
      .map((f) => label(f))
      .slice(0, 5),
  };
}
"""
)


CONTROLS_SCRIPT = (
    r"""
() => {
"""
    + _JS_HELPERS
    + r"""
  const accessibleName = (el) => {
    const aria = el.getAttribute('aria-label');
    const text = (aria || el.innerText || el.value || el.title || '').trim();
    return text.replace(/\s+/g, ' ').slice(0, 60);
  };
  const formFacts = (form) => {
    if (!form) return { in_form: false };
    const required = Array.from(
      form.querySelectorAll('input[required], select[required], textarea[required]')
    ).filter((i) => !i.value).length;
    return {
      in_form: true,
      form_method: (form.getAttribute('method') || 'get').toLowerCase(),
      form_action: (form.getAttribute('action') || '').slice(0, 200),
      required_empty: required,
      file_inputs: form.querySelectorAll('input[type=file]').length,
    };
  };

  const out = [];
  const push = (el, kind, extra) => {
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return;  // never rendered
    out.push(Object.assign({
      kind: kind,
      selector: label(el),
      path: uniquePath(el),
      name: accessibleName(el),
      type: (el.getAttribute('type') || '').toLowerCase(),
      disabled: !!el.disabled,
      has_onclick: !!el.getAttribute('onclick'),
      href: (el.getAttribute('href') || '').slice(0, 200),
    }, formFacts(el.form || el.closest('form')), extra || {}));
  };

  for (const el of document.querySelectorAll(
    'button, input[type=submit], input[type=button], input[type=reset]'
  )) {
    if (out.length >= 40) break;
    push(el, 'button', {});
  }
  // Only anchors that CANNOT navigate on their own: those must be wired to
  // JavaScript to do anything, so a dead one is a real defect. An ordinary
  // <a href="/products"> is a link, and whether it 404s is a question about
  // the route, which references.py and the page probe already answer.
  for (const el of document.querySelectorAll('a[href]')) {
    if (out.length >= 40) break;
    const href = (el.getAttribute('href') || '').trim();
    if (href === '' || href === '#' || href.toLowerCase().startsWith('javascript:')) {
      push(el, 'link', {});
    }
  }
  return out;
}
"""
)


SCRIPTS = {
    "layout": LAYOUT_SCRIPT,
    "audit": AUDIT_SCRIPT,
    "controls": CONTROLS_SCRIPT,
}


# --- findings ---------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One measured defect. Carries the page, the culprit and the number.

    ``severity`` splits what drives a repair from what is merely worth saying.
    An `error` is unambiguous and actionable — the page scrolls sideways, the
    console threw, the button does nothing. A `warning` is real but weaker
    evidence (an image with no intrinsic size may simply not have loaded yet),
    and sending the model to rewrite a template for one would be the false
    failure this module exists to avoid.
    """

    kind: str
    page: str
    detail: str
    width: int = 0
    severity: str = "error"
    selector: str = ""

    def line(self) -> str:
        where = self.page or "/"
        if self.width:
            where += f" at {self.width}px"
        return f"{where}: {self.detail}"


@dataclass(frozen=True)
class AuditCheck:
    """One aggregated verdict, in the shape `smoke.ProbeCheck` reports."""

    label: str
    ok: bool
    detail: str = ""


@dataclass
class SiteAudit:
    """Everything one browser session observed, plus what it did NOT observe."""

    ran: bool = False
    findings: tuple[Finding, ...] = ()
    pages: tuple[str, ...] = ()
    widths: tuple[int, ...] = ()
    unobserved: tuple[str, ...] = ()  # page(s) the browser could not load
    dropped_pages: tuple[str, ...] = ()  # over `browser_max_pages`
    controls_clicked: int = 0
    controls_skipped: tuple[str, ...] = ()
    controls_dropped: int = 0  # over `browser_max_controls`
    # (page, width, PNG bytes) for W7's vision critique. Empty unless the caller
    # asked for them — a screenshot nobody looks at is 200 KB of nothing.
    screenshots: tuple[tuple[str, int, bytes], ...] = ()

    def errors(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == "error")

    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity != "error")

    def of_kind(self, *kinds: str) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.kind in kinds)

    @property
    def observations(self) -> int:
        """How many page-at-width views were actually seen.

        Zero means the browser ran and saw nothing — every navigation failed.
        That is a SKIP, not a clean bill of health, and `checks()` returns
        nothing at all rather than four passes nobody earned.
        """
        return max(0, len(self.pages) * len(self.widths) - len(self.unobserved))

    def checks(self) -> list[AuditCheck]:
        """The aggregate lines reported beside the functional probe's.

        Aggregated per QUESTION rather than per page: five honest lines beat
        forty, and the page and selector live in each `detail` where the repair
        prompt needs them. A question whose pages could not all be observed says
        so — this is the same rule the skip reporting follows everywhere else.
        """
        if not self.ran or not self.observations:
            return []
        scope = (
            f"{len(self.pages)} page(s) at {', '.join(str(w) for w in self.widths)}px"
        )
        caveat = (
            f" ({len(self.unobserved)} page(s) never loaded and were not checked)"
            if self.unobserved
            else ""
        )
        out: list[AuditCheck] = []

        def add(label: str, kinds: tuple[str, ...], passed_detail: str) -> None:
            found = [f for f in self.of_kind(*kinds) if f.severity == "error"]
            detail = (
                "; ".join(f.line() for f in found[:4])
                if found
                else passed_detail + caveat
            )
            out.append(AuditCheck(f"browser: {label}", not found, detail))

        add("no page scrolls sideways", ("overflow",), scope)
        add("the console is clean", ("console", "network"), scope)
        add("every page renders content", ("empty",), scope)
        add("text contrast is at least 4.5:1", ("contrast",), scope)

        controls = [f for f in self.of_kind("dead-control") if f.severity == "error"]
        if self.controls_clicked or controls:
            detail = (
                "; ".join(f.line() for f in controls[:4])
                if controls
                else f"{self.controls_clicked} control(s) clicked"
            )
            if self.controls_skipped:
                detail += f"; skipped {len(self.controls_skipped)}"
            if self.controls_dropped:
                detail += f"; {self.controls_dropped} over the cap, not clicked"
            out.append(
                AuditCheck(
                    "browser: every control does something", not controls, detail
                )
            )
        return out

    def note(self) -> str:
        """What to append to the answer beyond the checks — warnings and skips.

        Separate from `checks()` on purpose: a warning must not read as a failed
        check, and a skipped control must not read as a passed one.
        """
        bits: list[str] = []
        if self.ran and not self.observations:
            bits.append(
                f"  skip browser checks ran but opened nothing — all "
                f"{len(self.unobserved)} navigation(s) failed"
            )
        for finding in self.warnings()[:4]:
            bits.append(f"  note {finding.line()}")
        for skipped in self.controls_skipped[:4]:
            bits.append(f"  skip {skipped}")
        if self.dropped_pages:
            bits.append(
                f"  skip {len(self.dropped_pages)} page(s) over the browser page "
                "budget were not opened: " + ", ".join(self.dropped_pages[:4])
            )
        return "\n".join(bits)


def page_of(url: str) -> str:
    """The route a probe URL names — what a finding should say, not the port."""
    try:
        parsed = urlparse(url or "")
    except Exception:
        return url or "/"
    path = parsed.path or "/"
    return f"{path}?{parsed.query}" if parsed.query else path


# --- W5: the layout audit, as pure functions over one probe -----------------


def horizontal_overflow(probe: PageProbe) -> list[Finding]:
    """The single most common responsive bug: the page scrolls sideways.

    Judged on the document's scroll width, never on an individual element's
    box — see the module docstring on `.table-wrap`. The overflowing elements
    are reported as the *culprit* because a measurement with no culprit produces
    a repair instruction nobody can act on.
    """
    layout = (probe.data or {}).get("layout") or {}
    viewport = _int(layout.get("viewport_width"))
    scroll = max(
        _int(layout.get("scroll_width")), _int(layout.get("body_scroll_width"))
    )
    if not viewport or not scroll:
        return []  # the script did not run — nothing measured, nothing claimed
    if scroll <= viewport + _OVERFLOW_SLACK_PX:
        return []
    culprits = [
        c.get("selector", "")
        for c in (layout.get("overflowing") or [])
        if isinstance(c, dict)
    ]
    detail = (
        f"the page is {scroll}px wide in a {viewport}px viewport, so it scrolls "
        "sideways"
    )
    if culprits:
        # The measurement, and what caused it. What to DO about it belongs in
        # `repair_instruction`, not in a line the user reads as a fact.
        detail += " — widest: " + ", ".join(x for x in culprits[:3] if x)
    return [
        Finding(
            kind="overflow",
            page=page_of(probe.url),
            detail=detail,
            width=probe.width,
            selector=culprits[0] if culprits else "",
        )
    ]


def empty_content(probe: PageProbe) -> list[Finding]:
    """The page answered 200 and then rendered nothing at all.

    Deliberately the strictest possible reading of "empty": no text, no media,
    no element children. A page with an empty table still rendered the table,
    and calling that a failure would send the repair loop after seeded data.
    """
    layout = (probe.data or {}).get("layout") or {}
    audit = (probe.data or {}).get("audit") or {}
    if not audit:
        return []
    if _int(layout.get("main_text_length")) or _int(audit.get("main_text_length")):
        return []
    if _int(audit.get("main_media_count")) or _int(audit.get("main_child_count")):
        return []
    return [
        Finding(
            kind="empty",
            page=page_of(probe.url),
            detail="the page rendered, but <main> is completely empty",
            width=probe.width,
        )
    ]


def low_contrast(probe: PageProbe) -> list[Finding]:
    """Text below WCAG AA against the colour actually behind it.

    Needs a browser and cannot be done by reading the CSS: the value that
    matters is the *computed* colour after the cascade, custom properties and
    the theme override have all had their say.
    """
    audit = (probe.data or {}).get("audit") or {}
    out: list[Finding] = []
    for sample in audit.get("contrast") or ():
        if not isinstance(sample, dict):
            continue
        ratio = _float(sample.get("ratio"))
        if not ratio:
            continue
        size = _float(sample.get("font_size")) or 16.0
        large = size >= 24 or (size >= 18.66 and bool(sample.get("bold")))
        minimum = _CONTRAST_MIN_LARGE if large else _CONTRAST_MIN
        if ratio >= minimum:
            continue
        selector = str(sample.get("selector") or "")
        text = str(sample.get("text") or "").strip()
        out.append(
            Finding(
                kind="contrast",
                page=page_of(probe.url),
                detail=(
                    f"{selector} has {ratio:.1f}:1 contrast against its background "
                    f"(needs {minimum}:1)" + (f" — {text!r}" if text else "")
                ),
                width=probe.width,
                selector=selector,
            )
        )
    return out


def unsized_images(probe: PageProbe) -> list[Finding]:
    """Images with no intrinsic dimensions — the layout-shift source.

    A **warning**, not an error: an image that simply has not decoded yet
    measures the same as one that will never load, and this module does not
    fail a page on evidence that weak.
    """
    layout = (probe.data or {}).get("layout") or {}
    srcs = [s for s in (layout.get("images_without_size") or ()) if s]
    if not srcs:
        return []
    return [
        Finding(
            kind="image-size",
            page=page_of(probe.url),
            detail=(
                f"{len(srcs)} image(s) have no intrinsic size, so the layout "
                "shifts as they load — " + ", ".join(str(s)[:60] for s in srcs[:3])
            ),
            width=probe.width,
            severity="warning",
        )
    ]


def layout_findings(probe: PageProbe) -> list[Finding]:
    """Every W5 measurement for one probe."""
    if not probe.ok:
        return []
    return [
        *horizontal_overflow(probe),
        *empty_content(probe),
        *low_contrast(probe),
        *unsized_images(probe),
    ]


# --- W6: runtime JavaScript and the network ---------------------------------


def console_findings(probe: PageProbe) -> list[Finding]:
    """Uncaught exceptions and `console.error` on the page.

    A `ReferenceError: addToCart is not defined` in a repair prompt is the
    difference between a fix that lands and one that guesses — the same reason
    `smoke.server_error()` lifts the exception out of a 5xx.
    """
    out: list[Finding] = []
    for message in probe.errors():
        text = (message.text or "").strip()
        if not text:
            continue
        if any(noise in text.lower() for noise in _RESOURCE_NOISE):
            continue  # `network_findings` reports this one with the URL
        out.append(
            Finding(
                kind="console",
                page=page_of(probe.url),
                detail=f"JavaScript error: {text[:200]}",
                width=probe.width,
            )
        )
    return out


def network_findings(probe: PageProbe) -> list[Finding]:
    """Requests the page made that did not come back usable.

    The runtime complement to `references.py`'s static scan: it also catches the
    case where the file exists on disk but 404s because Flask never routed it.
    """
    out: list[Finding] = []
    for failure in probe.failed_requests:
        url = failure.url or ""
        if any(url.endswith(ignored) for ignored in _IGNORED_REQUESTS):
            continue
        if failure.status:
            detail = f"{page_of(url)} returned HTTP {failure.status}"
        else:
            detail = f"{page_of(url)} could not be fetched ({failure.reason})"
        out.append(
            Finding(
                kind="network",
                page=page_of(probe.url),
                detail="the page asks for an asset that fails: " + detail,
                width=probe.width,
            )
        )
    return out


def runtime_findings(probe: PageProbe) -> list[Finding]:
    """Every W6 page-level observation (the control probe is separate)."""
    if probe.error:
        return []  # the page never loaded; `unobserved` records that instead
    return [*console_findings(probe), *network_findings(probe)]


# --- W6: the dead-button probe ----------------------------------------------


def is_destructive(control: dict) -> bool:
    """Would clicking this destroy state the later checks depend on?

    By accessible name and by `type=reset`. Named rather than inferred: a
    button whose label says Delete is the one case where a control WORKING is
    the problem, because it empties the seeded rows the other probes assert on.
    """
    name = str(control.get("name") or "").lower()
    selector = str(control.get("selector") or "").lower()
    if str(control.get("type") or "").lower() == "reset":
        return True
    return any(word in name or word in selector for word in _DESTRUCTIVE_WORDS)


def triage_controls(controls) -> tuple[list[dict], list[str]]:
    """Split the page's controls into "click this" and "skipped, because…".

    The skip reasons are returned, not swallowed: this codebase's rule is that a
    check which did not run is never reported as one that passed.
    """
    click: list[dict] = []
    skipped: list[str] = []
    for control in controls or ():
        if not isinstance(control, dict):
            continue
        name = str(control.get("name") or control.get("selector") or "control")
        if control.get("disabled"):
            skipped.append(f"{name}: disabled")
            continue
        if is_destructive(control):
            skipped.append(f"{name}: destructive, would empty the seeded data")
            continue
        if control.get("in_form"):
            method = str(control.get("form_method") or "get").lower()
            if method != "get":
                skipped.append(
                    f"{name}: submits a {method.upper()} form — the functional "
                    "probe posts to that route instead"
                )
                continue
            if _int(control.get("required_empty")) or _int(control.get("file_inputs")):
                skipped.append(
                    f"{name}: the form has an empty required field, so the "
                    "browser would block the submit"
                )
                continue
        click.append(control)
    return click, skipped


def click_findings(control: dict, page: str, changed: dict, probe: PageProbe):
    """Did clicking this control do anything? One finding, or none.

    "Nothing changed" is only claimed when nothing changed by ANY of the
    signals available — the URL, the rendered text, the DOM itself, or a
    JavaScript error. A handler that only fires a background request with no
    visible result is the acknowledged blind spot; on a server-rendered Jinja
    app it barely exists, and widening the check to cover it would mean failing
    controls on weaker evidence than this module accepts.
    """
    name = str(control.get("name") or control.get("selector") or "a control")
    selector = str(control.get("selector") or "")
    if not changed.get("clicked"):
        return [
            Finding(
                kind="dead-control",
                page=page,
                detail=f"{name!r} ({selector}) could not be clicked",
                severity="warning",
                selector=selector,
            )
        ]
    errors = [m.text for m in probe.errors() if (m.text or "").strip()]
    if errors:
        return [
            Finding(
                kind="dead-control",
                page=page,
                detail=(f"clicking {name!r} ({selector}) raised: {errors[0][:200]}"),
                selector=selector,
            )
        ]
    moved = bool(changed.get("url_changed"))
    text_moved = _int(changed.get("text_length_after")) != _int(
        changed.get("text_length_before")
    )
    dom_moved = _int(changed.get("html_length_after")) != _int(
        changed.get("html_length_before")
    )
    if moved or text_moved or dom_moved:
        return []
    return [
        Finding(
            kind="dead-control",
            page=page,
            detail=(
                f"clicking {name!r} ({selector}) changes nothing — no navigation, "
                "no DOM change, no message. It is wired to nothing."
            ),
            selector=selector,
        )
    ]


# --- the driver -------------------------------------------------------------


def _dedupe(findings) -> tuple[Finding, ...]:
    """Same defect seen at 1280 and again at 390 is one defect.

    Keyed on the measurement rather than the width, EXCEPT for overflow — a page
    that only scrolls sideways on a phone is a different fact from one that does
    it everywhere, and the width is the whole point of the finding.
    """
    seen: set[tuple] = set()
    out: list[Finding] = []
    for finding in findings:
        key = (
            finding.kind,
            finding.page,
            finding.detail,
            finding.width if finding.kind == "overflow" else 0,
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(finding)
    return tuple(out)


def probe_urls(base_url: str, routes) -> list[str]:
    """`(base, routes) -> full URLs`, de-duplicated, always including `/`."""
    base = (base_url or "").rstrip("/")
    paths: list[str] = []
    for route in routes or ():
        path = (route or "").strip()
        if not path.startswith("/") or "<" in path:
            continue  # a parameterised route has no address without a real id
        if path not in paths:
            paths.append(path)
    if "/" not in paths:
        paths.insert(0, "/")
    return [base + path for path in paths]


def audit_site(
    base_url: str,
    routes=(),
    widths=None,
    timeout: float | None = None,
    max_pages: int | None = None,
    max_controls: int | None = None,
    screenshot_pages: int = 0,
) -> SiteAudit:
    """Render every page, measure it, then click what is safe to click.

    ONE browser for all of it — a launch costs ~0.5s, and this runs inside the
    smoke test's single process window (two servers would fight over :5000 and
    over `app.db`, which is why `--webapp` already turns the smoke test off).

    Returns `SiteAudit(ran=False)` when no browser is available, so the caller
    reports a skip rather than a pass. Individual probes and clicks never raise
    (`browser.Session` turns every failure into a `PageProbe` carrying `error`);
    a failure of the session itself propagates to the `on_serving` hook, which
    swallows it — costing the observations, never the turn.
    """
    urls = probe_urls(base_url, routes)
    cap = max_pages if max_pages is not None else settings.browser_max_pages
    dropped = urls[max(1, cap) :]
    urls = urls[: max(1, cap)]
    sizes = [int(w) for w in (widths or settings.browser_widths) if int(w) > 0] or [
        1280
    ]
    control_budget = (
        max_controls if max_controls is not None else settings.browser_max_controls
    )

    audit = SiteAudit(
        pages=tuple(page_of(u) for u in urls),
        widths=tuple(sizes),
        dropped_pages=tuple(page_of(u) for u in dropped),
    )
    findings: list[Finding] = []
    unobserved: list[str] = []
    skipped: list[str] = []
    shots: list[tuple[str, int, bytes]] = []
    clicked = 0
    dropped_controls = 0
    pending: list[tuple[str, str, dict]] = []  # (url, page, control)

    with browser_session(timeout) as session:
        if session is None:
            return audit
        audit.ran = True
        for index, url in enumerate(urls):
            controls_seen = False
            for width in sizes:
                probe = session.probe(url, width=width, scripts=SCRIPTS)
                if probe.error or not probe.ok:
                    unobserved.append(f"{page_of(url)} at {width}px")
                    continue
                findings.extend(layout_findings(probe))
                findings.extend(runtime_findings(probe))
                # W7's raw material, and only when someone asked for it: each
                # screenshot costs a vision call, so the cap is on PAGES and the
                # caller sets it. Viewport rather than full page — a 4000px-tall
                # PNG is downscaled to illegibility before the VL model sees it,
                # and what a person judges first is the fold.
                if index < max(0, int(screenshot_pages)):
                    png = session.screenshot(url, width=width, full_page=False)
                    if png:
                        shots.append((page_of(url), width, png))
                if not controls_seen:
                    controls_seen = True
                    to_click, why_not = triage_controls(
                        (probe.data or {}).get("controls") or ()
                    )
                    skipped.extend(f"{page_of(url)}: {reason}" for reason in why_not)
                    for control in to_click:
                        pending.append((url, page_of(url), control))

        # Bound the fan-out the way `blueprint_max_files` does — and report what
        # that cost, rather than truncating where nobody can see it.
        budget = max(0, int(control_budget))
        dropped_controls = max(0, len(pending) - budget)
        for url, page, control in pending[:budget]:
            selector = str(control.get("path") or control.get("selector") or "")
            if not selector:
                continue
            probe, changed = session.click(url, selector, width=sizes[0])
            clicked += 1
            findings.extend(click_findings(control, page, changed, probe))

    audit.findings = _dedupe(findings)
    audit.unobserved = tuple(unobserved)
    audit.controls_clicked = clicked
    audit.controls_skipped = tuple(skipped)
    audit.controls_dropped = dropped_controls
    audit.screenshots = tuple(shots)
    return audit


# --- feeding the repair loop ------------------------------------------------
# Which findings are worth rewriting a file over, and which are only worth
# saying. The line is drawn by whether the fix is well-posed in ONE file:
#
#   * `contrast` is not here. The palette lives in `theme.css`, which is frozen
#     and written deterministically by `write_theme` — `_ensure_contrast`
#     already clears AA for every preset, so a contrast failure means a page
#     wrote its own colour, and which file that is cannot be read off the
#     measurement. Reported, and the theme keeps its guarantee.
#   * `network` is not here either. A reference to a file that does not exist
#     is `_repair_dead_references`' job, and it CREATES the missing file rather
#     than rewriting the page that asked for it.

REPAIRABLE_KINDS = frozenset({"overflow", "console", "dead-control", "empty"})


def repair_plan(audit: SiteAudit, resolve) -> list[tuple[str, tuple[Finding, ...]]]:
    """Group repairable errors by the file that owns them, worst page first.

    ``resolve(finding) -> str | None`` maps a finding to a project-relative
    path; returning None means "this one is reported, not repaired". That split
    is deliberate — the mapping needs the ProjectSpec and the files on disk,
    which is core's knowledge, while which findings are actionable at all is
    this module's.
    """
    grouped: dict[str, list[Finding]] = {}
    for finding in audit.errors():
        if finding.kind not in REPAIRABLE_KINDS:
            continue
        target = resolve(finding)
        if not target:
            continue
        grouped.setdefault(target, []).append(finding)
    return sorted(
        ((path, tuple(items)) for path, items in grouped.items()),
        key=lambda pair: (-len(pair[1]), pair[0]),
    )


def repair_instruction(target: str, findings) -> str:
    """What to tell the model about what the browser saw.

    Names the page, the selector and the measurement, exactly like
    `_smoke_repair_instruction` names the request that broke — specificity is
    what makes a repair land, and "make it responsive" is not specificity.
    """
    listed = "\n".join(f"- {f.line()}" for f in list(findings)[:6])
    return (
        f"The app runs, but opening its pages in a real browser showed these "
        f"problems in `{target}`:\n{listed}\n\n"
        f"Fix `{target}` so each one is gone.\n"
        "- Use the components that already exist: a wide table belongs inside "
        "`.table-wrap` (`{{ ui.table(...) }}` does this for you), and anything "
        "with a fixed pixel width needs `max-width: 100%`.\n"
        "- A control that does nothing is either wired to a route (`<a "
        "href=\"{{ url_for('...') }}\">`, or a form that posts to one) or "
        "removed. Do not leave a button whose handler does not exist.\n"
        "- Do not add a stylesheet, do not write a hex colour or a font family, "
        "and do not remove or rename any existing route, field or link. Change "
        "only what these findings name."
    )


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
