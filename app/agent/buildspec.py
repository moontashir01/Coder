"""Shared build spec — the requirements EVERY file of a multi-file build shares.

`_plan_file_ops` decomposes a request into per-file instructions, and each of
those runs as an independent LLM call. That is fine for "what goes in this
file", but it leaves the *cross-file* requirements — the navigation the user
dictated, the visual style they asked for — to be re-interpreted from scratch by
every call. A 7B model re-interprets them differently each time: page 2 renames
"Our Story" to "About", the stylesheet ignores "soft pastel" and emits Arial and
`#ff6b6b`.

`_sibling_context` already threads the FIRST page's nav markup into later pages,
but only the form, and only once a page exists — if page 1 got the labels wrong,
every later page copies the wrong labels. This module fills the gap upstream:
one extraction pass over the user's own words, producing a compact canonical
block that is injected into the planner AND into every per-file generation.

Two hard rules shape the design:

  * **Never invent requirements.** Everything in the "what the user asked for"
    half (navigation labels, style words, cross-page behaviours) is filtered
    against the user's message after the LLM answers — a label the user never
    typed is dropped. A prompt that says nothing about navigation or style
    yields an empty spec and the pipeline behaves exactly as before.
  * **Do translate style into CSS.** The other half (fonts, palette,
    decorative treatment) is the opposite: "soft pastel, script headings" is
    useless to the generator, so it is deliberately concretized into real
    Google Font names and real hex codes — by the LLM when it cooperates, by
    `_STYLE_PRESETS` when it doesn't. This half is only ever populated when the
    user actually used style words.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Only spend an LLM call when the request plausibly says something shared.
# Deliberately narrow: "stylesheet"/"css"/"styles" are structural words that
# appear in ordinary split requests ("separate the styles into a css file") and
# say nothing about the *look*, so they must NOT trip this.
_SPEC_HINT_RE = re.compile(
    r"\b(nav|navbar|nav\s?bar|navigation|menu|"
    r"fonts?|typography|typeface|palette|colou?rs?|colou?r\s?scheme|"
    r"pastel|theme|aesthetic|vibe|look\s+and\s+feel|"
    r"minimalist|elegant|modern|retro|vintage|playful|luxurious|"
    r"gradient|dark\s?mode|accent)\b",
    re.IGNORECASE,
)

# Font stacks that need NO network. Coder itself is offline, but the sites it
# generates were not: every styled build shipped a hard dependency on
# fonts.googleapis.com, and with no connection the browser blocks on a dead DNS
# lookup per page and then falls back to a default — so the typography the build
# spec chose is the one thing that never appears on screen. These are the
# fallback, and they keep each preset's PAIRING INTENT (a display face for
# headings, a readable one for body) rather than flattening everything to Arial.
_SYS_SANS = 'ui-sans-serif, system-ui, "Segoe UI", Roboto, Helvetica, Arial, sans-serif'
_SYS_SERIF = 'ui-serif, Georgia, Cambria, "Times New Roman", Times, serif'
_SYS_ROUNDED = 'ui-rounded, "SF Pro Rounded", "Segoe UI", system-ui, sans-serif'
_SYS_SCRIPT = '"Segoe Script", "Snell Roundhand", "Brush Script MT", cursive'

# Used when the request has style words but matches no preset.
_DEFAULT_STACKS = (_SYS_SANS, _SYS_SANS)

# Style words we can translate without the LLM's help. Order matters: the first
# preset whose pattern matches supplies any field the LLM left empty.
# "stacks" is the offline mirror of "fonts": (heading, body).
_STYLE_PRESETS: list[tuple[re.Pattern[str], dict]] = [
    (
        re.compile(r"\b(pastel|soft|gentle|delicate|dreamy)\b", re.I),
        {
            "fonts": ("Playfair Display", "Lato"),
            "stacks": (_SYS_SERIF, _SYS_SANS),
            "palette": ("#f6e7ef", "#e8dff5", "#fceade", "#b28fa8", "#4a3f45"),
            "decorative": (
                "soft rounded corners (12px), generous whitespace, subtle "
                "box-shadows, no hard black — use the darkest palette colour "
                "for text"
            ),
        },
    ),
    (
        re.compile(r"\b(script|cursive|calligraph\w*|handwritten|wedding)\b", re.I),
        {
            "fonts": ("Great Vibes", "Lato"),
            "stacks": (_SYS_SCRIPT, _SYS_SANS),
            "palette": ("#fdf6f0", "#f3e0d5", "#e8c4b8", "#a9746e", "#3f3538"),
            "decorative": (
                "script font for h1/h2 at a large size, letter-spaced small-caps "
                "for section labels, thin divider rules between sections"
            ),
        },
    ),
    (
        re.compile(r"\b(floral|botanical|garden|nature|leafy)\b", re.I),
        {
            "fonts": ("Cormorant Garamond", "Lato"),
            "stacks": (_SYS_SERIF, _SYS_SANS),
            "palette": ("#f7faf5", "#e3efdc", "#c7ddbc", "#7a9a72", "#33402f"),
            "decorative": (
                "CSS-only floral accents — pseudo-element flourishes (::before/"
                "::after) and inline SVG leaf dividers; never reference an image "
                "file that does not exist"
            ),
        },
    ),
    (
        re.compile(r"\b(dark\s?mode|dark\s+theme|midnight|nocturnal)\b", re.I),
        {
            "fonts": ("Inter", "Inter"),
            "stacks": (_SYS_SANS, _SYS_SANS),
            "palette": ("#0f1115", "#1a1d24", "#272b34", "#8ab4f8", "#e8eaed"),
            "decorative": (
                "dark surfaces with a single bright accent, 1px subtle borders "
                "instead of shadows"
            ),
        },
    ),
    (
        re.compile(r"\b(retro|vintage|nostalgic|70s|80s)\b", re.I),
        {
            "fonts": ("Righteous", "Karla"),
            "stacks": (_SYS_ROUNDED, _SYS_SANS),
            "palette": ("#fdf0d5", "#f4a259", "#bc4b51", "#5b8e7d", "#2e2b28"),
            "decorative": "thick borders, blocky shadows, warm saturated fills",
        },
    ),
    (
        re.compile(r"\b(playful|fun|vibrant|bold|colou?rful|energetic)\b", re.I),
        {
            "fonts": ("Poppins", "Nunito"),
            "stacks": (_SYS_ROUNDED, _SYS_SANS),
            "palette": ("#fff8f0", "#ffd166", "#ef476f", "#06d6a0", "#22223b"),
            "decorative": "large rounded shapes, bright accents, chunky buttons",
        },
    ),
    (
        re.compile(r"\b(elegant|luxur\w*|sophisticated|refined|classy)\b", re.I),
        {
            "fonts": ("Playfair Display", "Source Sans 3"),
            "stacks": (_SYS_SERIF, _SYS_SANS),
            "palette": ("#faf7f2", "#efe6d9", "#c8b08b", "#7a6a52", "#2b2620"),
            "decorative": (
                "wide letter-spacing on headings, thin hairline rules, muted "
                "gold accent, lots of vertical padding"
            ),
        },
    ),
    (
        re.compile(r"\b(minimal\w*|clean|simple|understated)\b", re.I),
        {
            "fonts": ("Inter", "Inter"),
            "stacks": (_SYS_SANS, _SYS_SANS),
            "palette": ("#ffffff", "#f4f4f5", "#e4e4e7", "#18181b", "#71717a"),
            "decorative": "flat surfaces, no shadows, one accent colour, strong grid",
        },
    ),
    (
        re.compile(r"\b(modern|sleek|contemporary|startup|tech)\b", re.I),
        {
            "fonts": ("Inter", "Inter"),
            "stacks": (_SYS_SANS, _SYS_SANS),
            "palette": ("#ffffff", "#f8fafc", "#e2e8f0", "#2563eb", "#0f172a"),
            "decorative": "8px radii, soft shadows, blue accent, system-ui spacing scale",
        },
    ),
    # The presets below close a silent gap: `_STYLE_WORD_RE` recognised all of
    # these words, so `find_style_keywords` reported a styled request — but no
    # pattern above matched them, `_preset_for` returned {}, and `resolve_theme`
    # therefore wrote no theme at all. "warm and cozy", "professional", "neon"
    # and "industrial" all shipped the scaffold's default look while reporting
    # that the style had been understood.
    (
        re.compile(r"\b(warm|cozy|cosy|rustic|earthy)\b", re.I),
        {
            "fonts": ("Bitter", "Source Sans 3"),
            "stacks": (_SYS_SERIF, _SYS_SANS),
            "palette": ("#fdf8f3", "#f2e4d5", "#d9b48f", "#a4522d", "#3b2b21"),
            "decorative": (
                "terracotta accents on warm off-white, generous padding, soft "
                "12px radii, no pure white and no pure black"
            ),
        },
    ),
    (
        re.compile(r"\b(neon|cyberpunk|synthwave|glow)\b", re.I),
        {
            "fonts": ("Orbitron", "Rajdhani"),
            "stacks": (_SYS_SANS, _SYS_SANS),
            "palette": ("#0a0a12", "#141428", "#2a2a4a", "#00e5ff", "#f0f0ff"),
            "decorative": (
                "near-black surfaces, one saturated cyan accent used for every "
                "border and link, thin 1px outlines instead of shadows"
            ),
        },
    ),
    (
        re.compile(r"\b(industrial|brutalist|concrete|utilitarian)\b", re.I),
        {
            "fonts": ("Archivo", "Archivo"),
            "stacks": (_SYS_SANS, _SYS_SANS),
            "palette": ("#f4f4f2", "#e2e2df", "#9a9a95", "#c2410c", "#1c1c1a"),
            "decorative": (
                "square corners (0 radius), heavy 2px borders, flat fills, no "
                "shadows, uppercase headings"
            ),
        },
    ),
    (
        re.compile(r"\b(muted|monochrome|greyscale|grayscale|understated)\b", re.I),
        {
            "fonts": ("Inter", "Inter"),
            "stacks": (_SYS_SANS, _SYS_SANS),
            "palette": ("#fafafa", "#f0f0f0", "#d4d4d4", "#525252", "#171717"),
            "decorative": (
                "greyscale throughout, weight and spacing carry the hierarchy "
                "instead of colour, hairline borders"
            ),
        },
    ),
    (
        re.compile(r"\b(professional|corporate|business|formal)\b", re.I),
        {
            "fonts": ("Source Serif 4", "Source Sans 3"),
            "stacks": (_SYS_SERIF, _SYS_SANS),
            "palette": ("#ffffff", "#f5f7fa", "#dde3ea", "#14532d", "#111827"),
            "decorative": (
                "restrained palette, 4px radii, clear section rules, generous "
                "line-height, no decorative flourishes"
            ),
        },
    ),
    (
        re.compile(r"\b(airy|breezy|coastal|serene)\b", re.I),
        {
            "fonts": ("Quicksand", "Nunito Sans"),
            "stacks": (_SYS_ROUNDED, _SYS_SANS),
            "palette": ("#f7fbfd", "#e6f2f7", "#c2dde8", "#2b7a99", "#22333b"),
            "decorative": (
                "lots of whitespace, large 16px radii, soft diffuse shadows, "
                "cool blue-green accent"
            ),
        },
    ),
]

# Style words worth recording even when the LLM call fails outright — the union
# of the preset patterns plus a few plain descriptors.
_STYLE_WORD_RE = re.compile(
    r"\b(pastel|soft|gentle|delicate|dreamy|script|cursive|calligraphy|"
    r"handwritten|wedding|floral|botanical|garden|nature|leafy|dark\s?mode|"
    r"midnight|retro|vintage|nostalgic|playful|fun|vibrant|bold|colou?rful|"
    r"energetic|elegant|luxurious|luxury|sophisticated|refined|classy|"
    r"minimal|minimalist|clean|simple|understated|modern|sleek|contemporary|"
    r"warm|cool|muted|monochrome|rustic|industrial|brutalist|neon|"
    r"professional|corporate|friendly|cozy|cosy|airy|earthy|breezy|coastal|"
    r"serene|cyberpunk|synthwave|concrete|utilitarian|business|formal|"
    r"greyscale|grayscale)\b",
    re.IGNORECASE,
)

# A style word only the LLM's *values* can satisfy or fail — "pastel" means
# light and unsaturated, "dark mode" means dark, and a palette that says
# otherwise is the model ignoring the request, not a stylistic choice.
_LIGHT_STYLE_RE = re.compile(r"\b(pastel|soft|light|airy|delicate|dreamy)\b", re.I)
_DARK_STYLE_RE = re.compile(r"\b(dark\s?mode|dark\s+theme|midnight|nocturnal)\b", re.I)

# Words that make a "decorative" sentence an actual instruction rather than a
# restatement of the request ("…to create a warm and inviting atmosphere").
_CONCRETE_CSS_RE = re.compile(
    r"(\d\s?(px|rem|em|%)|border|shadow|radius|rounded|gradient|letter-?spacing|"
    r"uppercase|small-?caps|divider|rule|padding|margin|grid|flex|svg|"
    r"pseudo-?element|::before|::after|background|font-)",
    re.IGNORECASE,
)

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_FONT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .'+-]{1,38}$")
_HTML_FILE_RE = re.compile(r"[\w./-]+\.(?:html?|htm)\b", re.IGNORECASE)

MAX_NAV_ITEMS = 12
MAX_BEHAVIORS = 5


SPEC_INSTRUCTIONS = """
You extract the requirements that EVERY file of a multi-file build shares.
Return ONLY a JSON object, nothing else, in exactly this shape:
{"navigation": [{"label": "<link text, copied verbatim from the request>", "file": "<relative .html filename>"}],
 "style_keywords": ["<style word the request itself used>"],
 "fonts": ["<Google Font for headings>", "<Google Font for body text>"],
 "palette": ["#rrggbb", "#rrggbb", "#rrggbb", "#rrggbb", "#rrggbb"],
 "decorative": "<one sentence of concrete CSS treatment implied by those style words>",
 "behaviors": ["<cross-page requirement, e.g. 'every page has a link to rsvp.html'>"]}

Rules:
- "navigation", "style_keywords" and "behaviors" describe what the request ACTUALLY
  SAYS. Copy navigation labels VERBATIM, in the order given. Invent nothing: no
  extra pages, no labels the request does not contain. If the request says nothing
  about navigation, return [].
- "fonts", "palette" and "decorative" are the opposite: TRANSLATE the style words
  into concrete choices — real Google Font family names, real 6-digit hex codes.
  If (and only if) "style_keywords" is empty, return [] / [] / "".
- Output ONLY the JSON. No prose, no markdown fences."""


@dataclass(frozen=True)
class BuildSpec:
    """Canonical cross-file requirements distilled from the user's own words."""

    nav: tuple[tuple[str, str], ...] = ()  # (label, target filename)
    style_keywords: tuple[str, ...] = ()
    fonts: tuple[str, ...] = ()
    # Offline mirror of `fonts`: (heading stack, body stack) using only families
    # already on the machine. Emitted instead of a Google Fonts <link> when
    # there is no network — see `to_context_block`.
    font_stacks: tuple[str, ...] = ()
    palette: tuple[str, ...] = ()
    decorative: str = ""
    behaviors: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not (
            self.nav
            or self.style_keywords
            or self.fonts
            or self.palette
            or self.decorative
            or self.behaviors
        )

    def nav_labels(self) -> tuple[str, ...]:
        return tuple(label for label, _ in self.nav)

    def nav_files(self) -> tuple[str, ...]:
        return tuple(target for _, target in self.nav)

    def stacks(self) -> tuple[str, str]:
        """(heading, body) system font stacks — always a usable pair."""
        if len(self.font_stacks) >= 2:
            return self.font_stacks[0], self.font_stacks[1]
        if len(self.font_stacks) == 1:
            return self.font_stacks[0], self.font_stacks[0]
        return _DEFAULT_STACKS

    def to_context_block(self, allow_network: bool = False) -> str:
        """The block injected into the planner and every per-file generation.

        Compact by construction — it rides in the same prompt as the plan
        manifest and the sibling context, inside `llm_num_ctx`.

        ``allow_network`` mirrors `settings.allow_network` and decides how the
        typography is expressed: real Google Fonts with a `<link>` when the
        generated site can reach the internet, system font stacks when it
        cannot. It defaults to **False** — the offline-safe branch — so a caller
        that forgets to pass it cannot accidentally ship a dead CDN dependency.
        """
        if self.is_empty():
            return ""
        parts = ["## Build spec — applies to EVERY file in this build"]

        if self.nav:
            items = "\n".join(
                f'{i}. "{label}" -> {target}'
                for i, (label, target) in enumerate(self.nav, 1)
            )
            parts.append(
                "### Navigation — the user specified it; use it EXACTLY\n"
                "Every page carries this same list of links, with these exact "
                "labels, in this exact order, pointing at these exact files. Only "
                "the current page's link may additionally be marked active. Do not "
                "rename, reorder, add or drop an item:\n"
                f"{items}\n"
                "Every file listed above must exist."
            )

        design: list[str] = []
        if self.fonts:
            heading = self.fonts[0]
            body = self.fonts[1] if len(self.fonts) > 1 else self.fonts[0]
            if allow_network:
                design.append(
                    f"Fonts: '{heading}' for headings, '{body}' for body text. Load "
                    "them from Google Fonts with a <link> in every page's <head> and "
                    "set them in font-family (with a generic fallback)."
                )
            else:
                h_stack, b_stack = self.stacks()
                design.append(
                    "Fonts: this machine has NO network, so do NOT link Google "
                    "Fonts or any CDN — the request would hang and then fall back "
                    "to a default anyway, losing the styling entirely. Use these "
                    "system stacks, declared ONCE as CSS variables and referenced "
                    "as var(--font-heading) / var(--font-body) everywhere:\n"
                    f"  --font-heading: {h_stack};\n"
                    f"  --font-body: {b_stack};"
                )
        if self.palette:
            design.append(
                "Colour palette — use these EXACT hex values, do not substitute "
                "defaults: " + ", ".join(self.palette)
            )
        if self.decorative:
            design.append(f"Treatment: {self.decorative}.")
        if design:
            kw = ", ".join(self.style_keywords)
            header = "### Design — the concrete reading of the requested style"
            if kw:
                header += f' ("{kw}")'
            parts.append(header + "\n" + "\n".join(design))
        elif self.style_keywords:
            parts.append(
                "### Design\nRequested style: "
                + ", ".join(self.style_keywords)
                + ". Choose specific fonts and colours that match it and use the "
                "same ones in every file."
            )

        if self.behaviors:
            parts.append(
                "### Cross-page requirements\n"
                + "\n".join(f"- {b}" for b in self.behaviors)
            )
        return "\n\n".join(parts)


def mentions_shared_spec(message: str) -> bool:
    """Is it worth an extraction call? False → the spec would be empty anyway."""
    return bool(_SPEC_HINT_RE.search(message or ""))


def _normalize(text: str) -> str:
    """Lowercase, punctuation-free form used for 'did the user actually say this'."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())


def _slugify(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")
    return slug or "page"


def find_style_keywords(message: str) -> tuple[str, ...]:
    """Style words the user actually used, in order, de-duplicated."""
    out: list[str] = []
    seen: set[str] = set()
    for m in _STYLE_WORD_RE.finditer(message or ""):
        word = " ".join(m.group(0).lower().split())
        if word not in seen:
            seen.add(word)
            out.append(word)
    return tuple(out)


def _preset_for(keywords: tuple[str, ...], message: str) -> dict:
    """Concrete design defaults for the first preset the request matches.

    The safety net for Gap 2: when the LLM answers with abstractions (or not at
    all) the generator still receives real fonts and real hex codes.
    """
    haystack = " ".join(keywords) + " " + (message or "")
    for pattern, preset in _STYLE_PRESETS:
        if pattern.search(haystack):
            return preset
    return {}


def _clean_nav(items, message: str) -> tuple[tuple[str, str], ...]:
    """Keep only navigation items whose label the user actually wrote."""
    norm_msg = _normalize(message)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in items or []:
        if isinstance(item, str):
            label, target = item, ""
        elif isinstance(item, dict):
            label = str(item.get("label") or item.get("name") or "").strip()
            target = str(item.get("file") or item.get("href") or "").strip()
        else:
            continue
        label = " ".join(label.split())
        norm_label = _normalize(label)
        # The anti-hallucination guard: a label the user never typed is dropped.
        if not norm_label or norm_label not in norm_msg:
            continue
        if norm_label in seen:
            continue
        target = target.split("#", 1)[0].split("?", 1)[0].strip().lstrip("/\\")
        if not target or not target.lower().endswith((".html", ".htm")):
            target = f"{_slugify(label)}.html"
        seen.add(norm_label)
        out.append((label, target))
        if len(out) >= MAX_NAV_ITEMS:
            break
    return tuple(out)


def _clean_behaviors(
    items, message: str, nav: tuple[tuple[str, str], ...]
) -> tuple[str, ...]:
    """Keep cross-page requirements that refer to something we know is real."""
    known = {t.lower() for _, t in nav} | {_normalize(label) for label, _ in nav}
    known |= {m.group(0).lower() for m in _HTML_FILE_RE.finditer(message or "")}
    out: list[str] = []
    for item in items or []:
        text = " ".join(str(item or "").split())[:200]
        if not text:
            continue
        low = text.lower()
        norm = _normalize(text)
        if not any(k and (k in low or k in norm) for k in known):
            continue  # invented requirement about a page nobody mentioned
        if text not in out:
            out.append(text)
        if len(out) >= MAX_BEHAVIORS:
            break
    return tuple(out)


def _clean_fonts(items) -> tuple[str, ...]:
    out: list[str] = []
    for item in items or []:
        name = " ".join(str(item or "").strip().strip("'\"").split())
        if name and _FONT_RE.match(name) and name not in out:
            out.append(name)
        if len(out) >= 3:
            break
    return tuple(out)


def _clean_palette(items) -> tuple[str, ...]:
    out: list[str] = []
    for item in items or []:
        color = str(item or "").strip()
        if _HEX_RE.match(color) and color.lower() not in {c.lower() for c in out}:
            out.append(color)
        if len(out) >= 6:
            break
    return tuple(out)


def chroma_lightness(hex_color: str) -> tuple[float, float]:
    """(chroma, lightness) of a #rgb/#rrggbb colour, both 0..1.

    Chroma (max-min channel), not HSL saturation: a pastel is a light *tint*,
    and every tint has a high HSL saturation (#fff8f0 scores 1.0), so
    saturation would reject the very colours it is meant to accept.
    """
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))
    hi, lo = max(r, g, b), min(r, g, b)
    return hi - lo, (hi + lo) / 2


def palette_matches_style(palette: tuple[str, ...], message: str) -> bool:
    """Does a palette actually express the style words the request used?

    Only the objective cases are judged: a "soft pastel" build whose colours are
    saturated primaries, or a "dark mode" build made of near-whites, is the model
    ignoring the request — and the preset is a better answer than what it
    returned. Every other style word passes (taste is not checkable).
    """
    if not palette:
        return False
    measured = [chroma_lightness(c) for c in palette if _HEX_RE.match(c)]
    if not measured:
        return False
    if _DARK_STYLE_RE.search(message or ""):
        return sum(1 for _, light in measured if light <= 0.35) * 2 >= len(measured)
    if _LIGHT_STYLE_RE.search(message or ""):
        # A palette still needs its dark text colour, so "most of it" is the bar.
        pastel = sum(
            1 for chroma, light in measured if light >= 0.75 and chroma <= 0.35
        )
        return pastel * 2 >= len(measured)
    return True


# ---------------------------------------------------------------------------
# Themes as data (Phase W1b, docs/web-quality-plan.md)
# ---------------------------------------------------------------------------
# `to_context_block` states the palette to the model as prose, and the model
# obeys it at its own discretion — measured drift is routine. The tokens below
# are written into `static/css/theme.css` instead, so the preset applies whether
# or not the model cooperates. Every rule in the scaffold's style.css is written
# in terms of these variables, which is what makes a whole-site restyle a
# one-file change with no markup edit anywhere.
#
# Pure and deterministic: same message in, same CSS out, no LLM call.


def _rgb(hex_color: str) -> tuple[float, float, float]:
    """#rgb / #rrggbb -> three 0..1 channels."""
    h = (hex_color or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _to_hex(channels: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c * 255))):02x}" for c in channels)


def relative_luminance(hex_color: str) -> float:
    """WCAG relative luminance (0..1). Not the same as `chroma_lightness`'s
    lightness — that one is a cheap ordering key, this one is the perceptual
    quantity a contrast ratio is defined on."""
    out = 0.0
    for channel, weight in zip(_rgb(hex_color), (0.2126, 0.7152, 0.0722)):
        linear = (
            channel / 12.92
            if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
        )
        out += linear * weight
    return out


def contrast_ratio(a: str, b: str) -> float:
    """WCAG contrast ratio between two colours, 1.0 (identical) to 21.0."""
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _ensure_contrast(color: str, against: str, target: float = 4.5) -> str:
    """Darken (or lighten) ``color`` until it is readable on ``against``.

    A preset's most colourful shade is chosen as the accent because it carries
    the requested mood — but mood says nothing about legibility, and a mid-tone
    accent used for link text on a light background routinely lands near 3:1.
    Rather than abandon the palette, move the accent along its own lightness
    axis until it passes; the hue, and therefore the look, survives.

    Returns ``color`` unchanged when it already passes or when no amount of
    adjustment reaches the target (a grey on grey), because a mangled colour is
    worse than a slightly weak one.
    """
    if contrast_ratio(color, against) >= target:
        return color
    lighten = relative_luminance(against) < 0.5
    channels = _rgb(color)
    for step in range(1, 20):
        factor = 1 + step * 0.06 if lighten else 1 - step * 0.05
        candidate = _to_hex(tuple(c * factor for c in channels))  # type: ignore[arg-type]
        if contrast_ratio(candidate, against) >= target:
            return candidate
    return color


# ---------------------------------------------------------------------------
# Colours the request names outright
# ---------------------------------------------------------------------------
# The preset table answers "what does *pastel* look like". It has no answer for
# the most direct way anyone asks for a look — naming the colours. "a dark blue
# and gold colour scheme" and "a purple and white palette" reached the palette
# code as nothing at all: no preset matched, `resolve_theme` returned {}, and
# the scaffold's default theme shipped. This reads the colours off the message.

_COLOR_NAMES: dict[str, str] = {
    "red": "#dc2626",
    "crimson": "#b91c1c",
    "maroon": "#7f1d1d",
    "burgundy": "#6b1f2e",
    "orange": "#ea580c",
    "amber": "#f59e0b",
    "gold": "#c9a227",
    "golden": "#c9a227",
    "yellow": "#eab308",
    "mustard": "#ca8a04",
    "olive": "#4d7c0f",
    "green": "#16a34a",
    "emerald": "#059669",
    "forest": "#166534",
    "mint": "#34d399",
    "lime": "#65a30d",
    "sage": "#7a9a72",
    "teal": "#0d9488",
    "turquoise": "#06b6d4",
    "cyan": "#06b6d4",
    "aqua": "#06b6d4",
    "blue": "#2563eb",
    "navy": "#1e3a8a",
    "azure": "#0284c7",
    "sky": "#0ea5e9",
    "cobalt": "#1d4ed8",
    "indigo": "#4f46e5",
    "purple": "#7c3aed",
    "violet": "#8b5cf6",
    "lavender": "#a78bfa",
    "lilac": "#c4b5fd",
    "plum": "#86198f",
    "magenta": "#c026d3",
    "fuchsia": "#c026d3",
    "pink": "#db2777",
    "rose": "#e11d48",
    "coral": "#f43f5e",
    "salmon": "#fb7185",
    "peach": "#fb923c",
    "brown": "#78350f",
    "tan": "#b45309",
    "beige": "#d6c6a8",
    "cream": "#f5efe0",
    "ivory": "#f8f4e8",
    "charcoal": "#374151",
    "slate": "#475569",
    "grey": "#6b7280",
    "gray": "#6b7280",
    "silver": "#9ca3af",
    "black": "#111111",
    "white": "#ffffff",
}

# "dark blue" is navy and "pale green" is mint — a modifier changes which colour
# was asked for, so it is read as part of the name rather than discarded.
_COLOR_MODIFIERS = {
    "dark": -0.35,
    "deep": -0.35,
    "rich": -0.2,
    "light": 0.45,
    "pale": 0.5,
    "soft": 0.4,
    "pastel": 0.55,
    "bright": 0.0,
    "vivid": 0.0,
}

_COLOR_WORD_RE = re.compile(
    r"(?:\b(?P<mod>" + "|".join(_COLOR_MODIFIERS) + r")\s+)?"
    r"\b(?P<name>" + "|".join(sorted(_COLOR_NAMES, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
_HEX_IN_TEXT_RE = re.compile(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")

# A colour word only counts as a *style* request in a styling context. Without
# this, "a green energy company" and "a Black Friday deals page" would each
# repaint the whole site — the `_clean_nav` failure (inventing what the user did
# not ask for) wearing a different hat. A literal hex needs no such evidence:
# nobody types #1e3a8a by accident.
_COLOR_CONTEXT_RE = re.compile(
    r"\b(colou?rs?|colou?r\s?scheme|palette|theme|themed|styled?|style|look|"
    r"looks|accent|background|design|branding|vibe|aesthetic|tones?|shades?|"
    r"paint)\b",
    re.IGNORECASE,
)

# Wording that asks for the site's LOOK to change, as opposed to its content.
# `wants_restyle` gates the one pass allowed to overwrite a theme the user may
# have hand-tuned, so it is kept narrow: it must fire on a request to restyle
# and on nothing else. It doubles as colour-request evidence, so "restyle it in
# navy" needs no separate word naming what navy is.
_RESTYLE_VERB_RE = re.compile(
    r"\b(restyle|recolou?r|re-?theme|redesign|reskin|"
    r"make\s+it|turn\s+it|style\s+it|"
    r"change\s+(?:the\s+)?(?:colou?rs?|palette|theme|style|look|design)|"
    r"switch\s+(?:it\s+)?to)\b",
    re.IGNORECASE,
)

# Below this chroma a colour cannot carry the accent role — white, black and
# grey are page and text, never the thing that stands out.
_ACCENT_MIN_CHROMA = 0.12


def _mix(color: str, toward: str, amount: float) -> str:
    """Blend ``color`` ``amount`` of the way toward ``toward`` (0 = unchanged)."""
    return _to_hex(
        tuple(  # type: ignore[arg-type]
            a + (b - a) * amount for a, b in zip(_rgb(color), _rgb(toward))
        )
    )


def find_requested_colors(message: str) -> tuple[tuple[str, str], ...]:
    """Colours the message actually names, as ``(label, hex)`` in the order used.

    Returns () unless the message shows styling intent (`_COLOR_CONTEXT_RE`) or
    contains a literal hex — a colour word in ordinary prose is a noun, not a
    design decision.
    """
    text = message or ""
    hexes = _HEX_IN_TEXT_RE.findall(text)
    # A style word is itself evidence of styling intent: "a modern site in
    # purple" names no palette/colour/theme, and without this the specific half
    # of the request would be dropped in favour of the preset's own blue.
    if not hexes and not (
        _COLOR_CONTEXT_RE.search(text)
        or _STYLE_WORD_RE.search(text)
        or _RESTYLE_VERB_RE.search(text)
    ):
        return ()

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in hexes:
        if raw.lower() not in seen:
            seen.add(raw.lower())
            out.append((raw.lower(), raw.lower()))
    for match in _COLOR_WORD_RE.finditer(text):
        name = match.group("name").lower()
        mod = (match.group("mod") or "").lower()
        color = _COLOR_NAMES[name]
        shift = _COLOR_MODIFIERS.get(mod, 0.0)
        if shift < 0:
            color = _mix(color, "#000000", -shift)
        elif shift > 0:
            color = _mix(color, "#ffffff", shift)
        label = f"{mod} {name}".strip()
        if color.lower() in seen:
            continue
        seen.add(color.lower())
        out.append((label, color))
        if len(out) >= 4:
            break
    return tuple(out)


def palette_from_colors(colors: tuple[str, ...], dark: bool) -> tuple[str, ...]:
    """Build a full five-role palette around the colours the request named.

    The named colours carry the hue; the surfaces and text are *derived* from
    them so the result is a coherent theme rather than the two colours pasted
    onto the default one. `theme_tokens` then assigns the roles as it does for
    any preset, which is what keeps one code path for both.

    **The page is light unless the request asked for dark.** "a dark blue and
    gold site" most often means navy on white, not a navy background, and the
    conservative reading is the one that cannot ruin the build — `dark mode`
    still reaches its own preset, and `_DARK_STYLE_RE` is what sets ``dark``.
    """
    if not colors:
        return ()
    chromatic = [c for c in colors if chroma_lightness(c)[0] >= _ACCENT_MIN_CHROMA]
    primary = chromatic[0] if chromatic else colors[0]
    secondary = chromatic[1] if len(chromatic) > 1 else None

    if dark:
        return (
            _mix(primary, "#000000", 0.88),
            _mix(primary, "#000000", 0.80),
            _mix(primary, "#000000", 0.62),
            secondary or primary,
            _mix(primary, "#ffffff", 0.92),
        )
    return (
        "#ffffff",
        _mix(primary, "#ffffff", 0.95),
        _mix(primary, "#ffffff", 0.82),
        primary if secondary is None else secondary,
        _mix(primary, "#000000", 0.78),
    )


def theme_tokens(
    palette: tuple[str, ...], stacks: tuple[str, ...] = ()
) -> dict[str, str]:
    """Map a palette onto the CSS custom properties style.css is written against.

    Ordered by lightness rather than by the order the preset lists them, so the
    role each colour plays is derived from what the colour *is*. A palette given
    in a different order therefore produces the same theme.

    **Which END is the page is decided first, by the palette's own weight.**
    Reading the lightest colour as the background unconditionally looks right
    until a dark palette arrives: the "dark mode" preset
    (`#0f1115 #1a1d24 #272b34 #8ab4f8 #e8eaed`) is mostly dark, so its lightest
    entry is the *text* — and assigning that to the page produced a light theme
    with a blue surface from a request that said "dark mode". Caught by
    `test_a_dark_palette_declares_a_dark_color_scheme`.
    """
    ordered = sorted(
        (c for c in palette if _HEX_RE.match(c or "")),
        key=lambda c: chroma_lightness(c)[1],
    )
    if len(ordered) < 3:
        return {}

    is_dark = sum(chroma_lightness(c)[1] for c in ordered) / len(ordered) < 0.5
    page_end = list(ordered) if is_dark else list(reversed(ordered))
    # page_end runs from the page outwards: background, then the surfaces layered
    # on it, and the text colour at the far end.
    bg, surface = page_end[0], page_end[1]
    border = page_end[2] if len(page_end) >= 3 else surface
    text = page_end[-1]

    # Secondary text: the least colourful shade that is neither the page nor the
    # body text, dragged into legibility below. A mid-tone from the palette
    # would otherwise read as a third accent.
    middle = [c for c in page_end[1:-1]] or [text]
    muted = min(middle, key=lambda c: chroma_lightness(c)[0])

    # The accent is the most chromatic shade that isn't the page itself, pulled
    # into legibility against that page.
    accent = max((c for c in ordered if c != bg), key=lambda c: chroma_lightness(c)[0])
    accent = _ensure_contrast(accent, bg)
    accent_text = max(
        ("#ffffff", "#000000", text), key=lambda c: contrast_ratio(c, accent)
    )

    tokens = {
        "--color-bg": bg,
        "--color-surface": surface,
        "--color-border": border,
        "--color-text": text,
        "--color-muted": _ensure_contrast(muted, bg, 4.5),
        "--color-accent": accent,
        "--color-accent-text": accent_text,
    }
    if len(stacks) >= 2:
        tokens["--font-heading"] = stacks[0]
        tokens["--font-body"] = stacks[1]
    return tokens


def resolve_theme(message: str) -> dict:
    """The theme a request asks for, or {} when it names no style at all.

    Deliberately LLM-free: it never consults the extraction call's palette,
    because this runs *before* generation (beside the scaffold copy) and that
    call happens later, inside the multi-file flow. A request with no style
    words and no colours keeps the scaffold's default theme, which is the honest
    answer — inventing a look nobody asked for is the failure `_clean_nav`
    exists to prevent.

    **Colours the user named beat the preset they also matched.** "a modern site
    in purple" matches the modern preset, whose palette is blue; using it would
    answer the vaguer half of the request and discard the specific half, which is
    precisely the "not the style I asked for" complaint. The preset still supplies
    the typography, so "an elegant site in navy and gold" keeps its serif pairing.
    """
    keywords = find_style_keywords(message)
    colors = find_requested_colors(message)
    if not keywords and not colors:
        return {}

    preset = _preset_for(keywords, message)
    stacks = tuple(preset.get("stacks") or ())
    if colors:
        palette = _clean_palette(
            palette_from_colors(
                tuple(c for _label, c in colors),
                dark=bool(_DARK_STYLE_RE.search(message or "")),
            )
        )
        stacks = stacks or _DEFAULT_STACKS
    else:
        palette = _clean_palette(preset.get("palette") or ())

    tokens = theme_tokens(palette, stacks)
    if not tokens:
        return {}
    return {
        "keywords": keywords + tuple(label for label, _c in colors),
        "tokens": tokens,
    }


def wants_restyle(message: str) -> bool:
    """Does this message ask for the look of an existing site to change?

    Two conditions, both required. The wording must actually be a restyle
    request, and it must resolve to a theme — so "make it responsive" and "make
    it faster" match the first test, resolve to nothing, and are correctly not
    treated as restyles.
    """
    if not _RESTYLE_VERB_RE.search(message or ""):
        return False
    return bool(resolve_theme(message))


def theme_css(theme: dict) -> str:
    """Render `resolve_theme`'s tokens as the generated `static/css/theme.css`.

    Emits `:root` ONLY — no `prefers-color-scheme` block. A palette chosen for
    "soft pastel" has no correct mechanical inverse, and deriving one by rule
    produces colours nobody picked; the requested look is the look. The
    scaffold's default theme, which was not chosen by anyone, keeps its dark
    scheme for exactly the same reason.
    """
    tokens = (theme or {}).get("tokens") or {}
    if not tokens:
        return ""
    keywords = ", ".join((theme or {}).get("keywords") or ()) or "the request"
    dark = relative_luminance(tokens.get("--color-bg", "#ffffff")) < 0.4
    lines = [
        "/* Theme — generated from the style this project asked for:",
        f" * {keywords}.",
        " *",
        " * Only custom properties live here; style.css is written entirely in",
        " * terms of them, so editing a value below restyles every page and",
        " * every component at once. Nothing regenerates this file after the",
        " * first build — it is yours to tune.",
        " */",
        "",
        ":root {",
        f"  color-scheme: {'dark' if dark else 'light'};",
    ]
    for name, value in tokens.items():
        lines.append(f"  {name}: {value};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def build_spec_from_data(data: dict | None, message: str) -> BuildSpec:
    """Turn a parsed extraction response into a filtered, concretized spec.

    ``data`` may be None (the LLM call failed) — the style half still degrades
    to the rule-based presets, and the "what the user asked for" half stays
    empty rather than being guessed at.
    """
    data = data if isinstance(data, dict) else {}

    nav = _clean_nav(data.get("navigation"), message)
    behaviors = _clean_behaviors(data.get("behaviors"), message, nav)

    # Style words are taken from the message itself, not the model's echo of
    # them, so "style_keywords" can never smuggle in a style nobody asked for.
    keywords = find_style_keywords(message)
    fonts: tuple[str, ...] = ()
    font_stacks: tuple[str, ...] = ()
    palette: tuple[str, ...] = ()
    decorative = ""
    if keywords:
        fonts = _clean_fonts(data.get("fonts"))
        palette = _clean_palette(data.get("palette"))
        decorative = " ".join(str(data.get("decorative") or "").split())[:240]
        if not palette_matches_style(palette, message):
            palette = ()  # gold and crimson are not "soft pastel" — use the preset
        if not _CONCRETE_CSS_RE.search(decorative):
            decorative = ""  # a restatement of the request tells the model nothing
        preset = _preset_for(keywords, message)
        if preset:  # fill in whatever the model failed to make concrete
            fonts = fonts or tuple(preset["fonts"])
            palette = palette or tuple(preset["palette"])
            decorative = decorative or preset["decorative"]
        # The offline pair always comes from the preset, never from the model:
        # a Google Font name the LLM picked says nothing about which families
        # this machine actually has installed.
        font_stacks = tuple(preset.get("stacks", _DEFAULT_STACKS)) if preset else ()
        if fonts and not font_stacks:
            font_stacks = _DEFAULT_STACKS

    return BuildSpec(
        nav=nav,
        style_keywords=keywords,
        fonts=fonts,
        font_stacks=font_stacks,
        palette=palette,
        decorative=decorative,
        behaviors=behaviors,
    )
