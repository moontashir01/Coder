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

# Style words we can translate without the LLM's help. Order matters: the first
# preset whose pattern matches supplies any field the LLM left empty.
_STYLE_PRESETS: list[tuple[re.Pattern[str], dict]] = [
    (
        re.compile(r"\b(pastel|soft|gentle|delicate|dreamy)\b", re.I),
        {
            "fonts": ("Playfair Display", "Lato"),
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
            "palette": ("#fdf0d5", "#f4a259", "#bc4b51", "#5b8e7d", "#2e2b28"),
            "decorative": "thick borders, blocky shadows, warm saturated fills",
        },
    ),
    (
        re.compile(r"\b(playful|fun|vibrant|bold|colou?rful|energetic)\b", re.I),
        {
            "fonts": ("Poppins", "Nunito"),
            "palette": ("#fff8f0", "#ffd166", "#ef476f", "#06d6a0", "#22223b"),
            "decorative": "large rounded shapes, bright accents, chunky buttons",
        },
    ),
    (
        re.compile(r"\b(elegant|luxur\w*|sophisticated|refined|classy)\b", re.I),
        {
            "fonts": ("Playfair Display", "Source Sans 3"),
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
            "palette": ("#ffffff", "#f4f4f5", "#e4e4e7", "#18181b", "#71717a"),
            "decorative": "flat surfaces, no shadows, one accent colour, strong grid",
        },
    ),
    (
        re.compile(r"\b(modern|sleek|contemporary|startup|tech)\b", re.I),
        {
            "fonts": ("Inter", "Inter"),
            "palette": ("#ffffff", "#f8fafc", "#e2e8f0", "#2563eb", "#0f172a"),
            "decorative": "8px radii, soft shadows, blue accent, system-ui spacing scale",
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
    r"professional|corporate|friendly|cozy|airy)\b",
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

    def to_context_block(self) -> str:
        """The block injected into the planner and every per-file generation.

        Compact by construction — it rides in the same prompt as the plan
        manifest and the sibling context, inside `llm_num_ctx`.
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
            design.append(
                f"Fonts: '{heading}' for headings, '{body}' for body text. Load them "
                "from Google Fonts with a <link> in every page's <head> and set them "
                "in font-family (with a generic fallback)."
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

    return BuildSpec(
        nav=nav,
        style_keywords=keywords,
        fonts=fonts,
        palette=palette,
        decorative=decorative,
        behaviors=behaviors,
    )
