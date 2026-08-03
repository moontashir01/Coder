"""Requirements Blueprint — infer the WHOLE build from a short request.

See `docs/requirements-blueprint.md` for the full design. In one paragraph:
"build me a login page" should not ship a lone `login.html`. A login page
*implies* a form + validation, a forgot-password page and flow, a sign-up link,
and — the part Coder never builds — a backend, so the button actually does
something. This module turns a terse build request into a `Blueprint`: the
implied features (tiered), the full file list (frontend + backend + data +
glue), and an **interface contract** (endpoints, form-field↔route bindings, data
schema) that keeps those files consistent with each other.

This is the OPPOSITE of the rest of the pipeline, which is built to invent
nothing (see `buildspec.py`'s anti-hallucination `_clean_nav`). The leash here
is threefold and lives in this file:

  * **Tiers.** Every feature is `requested` (literally asked), `core` (omitting
    it makes the thing non-functional), or `optional` (a nice-to-have). Only
    requested+core build by default; optional is *reported, not built*.
  * **A narrow gate.** `should_blueprint()` fires only for greenfield build
    requests — a build verb plus an app/artifact noun — never a question, an
    edit, or a split/refactor.
  * **Hard caps.** `Blueprint.build_files()` is bounded by the caller's
    `blueprint_max_files`.

Like `buildspec.py`, this module is PURE: it does no LLM call itself. The caller
(`AgentCore._expand_requirements`) makes the one call and hands the parsed JSON
to `blueprint_from_data`, so every rule here is unit-testable fully offline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.agent.buildspec import wants_restyle
from app.agent.runtime_probe import NO_STACK, Stack

if TYPE_CHECKING:  # pragma: no cover
    # Type-only: `projectspec` imports THIS module at runtime, so importing it
    # back would be circular. `from __future__ import annotations` keeps every
    # annotation a string, so the name is never resolved at run time.
    from app.agent.projectspec import Entity

# ---------------------------------------------------------------------------
# The gate — which requests get a blueprint
# ---------------------------------------------------------------------------

# A greenfield authoring verb. Deliberately excludes incremental verbs
# (add/append/update/edit/fix/refactor) — those change something that already
# exists and belong on the ordinary single-file / tool-loop paths.
_BLUEPRINT_VERB_RE = re.compile(
    r"\b(build|create|make|scaffold|generate|design|implement|develop|"
    r"recreate|replicate|clone|prototype)\b",
    re.IGNORECASE,
)

# A noun that names an application or a user-facing experience — something with
# behaviour behind it, not a bare file. "page" is included on purpose: the
# user's canonical example is "a login page", which must trigger. Bare file-kind
# words (file/css/html/js) are intentionally absent — see _SINGLE_FILE_ONLY_RE.
_BLUEPRINT_NOUN_RE = re.compile(
    r"\b(app|application|web\s?app|web\s?site|website|site|webpage|page|"
    r"dashboard|portal|platform|system|service|backend|back\s?end|"
    r"front\s?end|full\s?stack|login|log\s?in|signup|sign\s?up|"
    r"register(?:ation)?|registration|auth(?:entication)?|form|crud|blog|"
    r"store|shop|e-?commerce|marketplace|todo|to-?do|chat|game|"
    r"landing\s?page|admin|feature|tool|api|survey|quiz|booking|calculator)\b",
    re.IGNORECASE,
)

# A message that OPENS with an interrogative is asking ABOUT something, not
# asking for it to be built ("what does a login page need?"). Kept local so this
# module has no dependency on core.py (which imports it).
_QUESTION_RE = re.compile(
    r"^\s*(how|what|why|when|which|who|where|explain|describe|tell me|"
    r"is|are|does|do|can i|should i|could you tell)\b",
    re.IGNORECASE,
)

# A request precisely scoped to ONE non-app file — "create a css file", "make a
# new html file". These are genuine single-file requests; blueprinting a whole
# backend around them would be wrong. The ordinary _file_op_flow owns them.
_SINGLE_FILE_ONLY_RE = re.compile(
    r"\b(a|an|one|single|new|another)\s+"
    r"(css|html|js|javascript|json|python|py|scss|sass|less|markdown|md|"
    r"text|txt|yaml|yml|xml|config)\s+file\b",
    re.IGNORECASE,
)

# Incremental edit to an existing thing ("add a footer to the page", "put a
# button into index.html") — not a fresh build.
#
# ANCHORED to the start of the message (Phase B). Unanchored it also matched the
# trailing clause of a perfectly ordinary greenfield request — "build a shop and
# add reviews to it" was read as an edit and never blueprinted. What makes a
# message an edit is that it OPENS by asking for one; an edit verb halfway
# through is just a description of what the new thing should contain.
_EDIT_INTO_RE = re.compile(
    r"^\s*(?:(?:can|could|would)\s+you\s+|please\s+|now\s+|also\s+|then\s+)*"
    r"(add|append|insert|put|move|include)\b[^.]*?\b(to|into|in)\b",
    re.IGNORECASE,
)

# The subset of build nouns that name a whole APPLICATION rather than a single
# surface. "page" and "form" are deliberately absent: "a login page" must
# blueprint, but "an html file for the about page" is genuinely one file.
_APP_NOUN_RE = re.compile(
    r"\b(app|application|web\s?app|web\s?site|website|site|dashboard|portal|"
    r"platform|system|service|blog|store|shop|e-?commerce|marketplace|todo|"
    r"to-?do|crud|full\s?stack|backend|back\s?end|admin|api)\b",
    re.IGNORECASE,
)

# An explicit opt-out of the backend. A full build is many LLM calls and minutes
# on a 7B, and someone who asks for one static page must still get one — so this
# is honoured as `prefer="none"` (no backend proposed, no scaffold, no data
# layer) rather than by refusing to plan the build at all.
_STATIC_ONLY_RE = re.compile(
    r"\b(just|only|plain|pure)\s+(html|css|static|frontend|front\s?end)\b|"
    r"\b(static|frontend|front\s?end|html)[- ]only\b|"
    r"\bno\s+(backend|back\s?end|server|database|db)\b|"
    r"\bwithout\s+(a\s+)?(backend|back\s?end|server|database)\b",
    re.IGNORECASE,
)


def wants_static_only(message: str) -> bool:
    """Did the user explicitly ask for no backend? (Phase B escape hatch.)"""
    return bool(_STATIC_ONLY_RE.search(message or ""))


# Explicit split/reorganize wording is already owned by wants_multifile /
# _multi_file_flow — never divert it here.
_SPLIT_RE = re.compile(
    r"\b(separate|split|extract|reorganize|reorganise|restructure|refactor)\b",
    re.IGNORECASE,
)


# The mirror image of _BLUEPRINT_VERB_RE: the incremental verbs a build gate
# must reject are exactly the ones an amendment gate must accept.
_AMEND_VERB_RE = re.compile(
    r"\b(add|adds|adding|append|insert|put|include|attach|"
    r"update|updates|change|changes|modify|edit|adjust|tweak|"
    r"rename|remove|delete|drop|hide|"
    r"extend|support|enable|allow|"
    r"also|additionally|plus|now|next|then)\b",
    re.IGNORECASE,
)

# "can you also show me how X works" is a question about the project, not a
# request to change it — even though it contains "also" and "show".
_AMEND_QUESTION_RE = re.compile(
    r"^\s*(how|what|why|when|which|who|where|explain|describe|tell me|show me|"
    r"is|are|does|do|did|can you (?:explain|tell|show)|should i|could you tell)\b",
    re.IGNORECASE,
)


def should_amend(message: str, spec_exists: bool) -> bool:
    """True when a message asks to CHANGE a project we already remember.

    The mirror of `should_blueprint()`: it fires on exactly the incremental
    verbs that gate rejects — add / update / change / remove / rename / also /
    now — and **only when a ProjectSpec exists**. Without a spec there is
    nothing to amend and routing is completely unchanged, which is what keeps
    this from touching any existing behaviour.

    Consulted in one place (`AgentCore.chat`), ahead of the blueprint gate, so a
    greenfield "build me a blog" (no incremental verb) still blueprints even in
    a project that has a spec.
    """
    m = message or ""
    if not spec_exists:
        return False
    if _AMEND_QUESTION_RE.match(m):
        return False
    if _SPLIT_RE.search(m):
        return False  # split/refactor is _multi_file_flow's, as it always was
    # A restyle is an amendment that carries none of the incremental verbs:
    # "make it navy" changes the project without adding, updating or removing
    # anything, so it fell through to ordinary routing and no stage on that path
    # rewrites theme.css. Yielding to `should_blueprint` keeps a greenfield
    # request that happens to name a look ("build me a blog, make it purple") on
    # the build path, where the theme is written anyway.
    if wants_restyle(m) and not should_blueprint(m):
        return True
    return bool(_AMEND_VERB_RE.search(m))


def should_blueprint(message: str) -> bool:
    """True when a message is a greenfield build worth expanding into a blueprint.

    Narrow by construction (see the module docstring): a build verb AND an
    app/artifact noun, and none of the disqualifiers — question, single-file
    request, incremental edit, or an explicit split/refactor. Consulted in one
    place (`AgentCore.chat`), so widening it cannot change how any other request
    routes.
    """
    m = message or ""
    if _QUESTION_RE.match(m):
        return False
    # "create a css file" is one file; "build me a website with a css file for
    # the styling" is not. The single-file veto now yields to an application
    # noun, which is the difference between naming the scope and naming a part
    # of it (Phase B).
    if _SINGLE_FILE_ONLY_RE.search(m) and not _APP_NOUN_RE.search(m):
        return False
    if _SPLIT_RE.search(m):
        return False
    if _EDIT_INTO_RE.search(m):
        return False
    return bool(_BLUEPRINT_VERB_RE.search(m) and _BLUEPRINT_NOUN_RE.search(m))


# Phrasing that asks for something to be made without naming a build verb —
# "I need somewhere to track my expenses". Together with the build verbs this
# bounds what the tier-2 classifier is allowed to look at.
_WANT_RE = re.compile(
    r"\b(?:i|we)\s+(?:want|need|would like|'d like)\b|"
    r"\b(?:help me|make me|set (?:me )?up|put together|working on|building)\b|"
    # A bare noun phrase IS the request: "something to organize my recipes",
    # "somewhere to track expenses", "a place my club can post events". These
    # carry no verb and no noun the build regexes know, which is exactly the
    # class tier 2 exists for — found by writing the Phase E eval for it.
    # The lookahead drops the report-a-problem sense of the same words, so
    # "something is wrong with the parser" does not buy a classifier call.
    r"\b(?:something|somewhere|some way|a way|a place|an app|a tool|a site)\b"
    r"(?!\s+(?:is|was|are|were|isn't|seems|looks|went|broke|happened|failed))",
    re.IGNORECASE,
)

# A message that OPENS with a dev command is an instruction about the workspace,
# not a request for an application — "run the build" and "deploy the site" both
# carry words the build regexes like ("build", "site"). Leading-only on purpose:
# "start a blog for me" is a build, so `start` is deliberately absent too.
_COMMAND_RE = re.compile(
    r"^\s*(?:please\s+|now\s+)?"
    r"(run|execute|install|uninstall|deploy|commit|push|pull|merge|rebase|"
    r"test|lint|format|restart|stop|kill|undo|index|clone|checkout)\b",
    re.IGNORECASE,
)


def may_be_web_build(message: str) -> bool:
    """Is this worth ASKING a model whether it's a web build? (Phase B tier 2.)

    `should_blueprint` is a verb×noun regex, and a noun list cannot enumerate
    what people build: "a recipe organizer", "somewhere to track my expenses",
    "a place my club can post events" all miss it and silently ship static HTML
    with no server and no database. A model can answer that question; a regex
    cannot. But a model call per turn is not free, so this decides — cheaply and
    deterministically — which turns are even candidates.

    It rejects everything `should_blueprint` rejects for its own reasons
    (questions, splits, single-file requests, opening edits), rejects requests
    tier 1 has *already* accepted (no point asking twice), and then requires
    some sign the user wants something made at all. What survives is a short
    list of genuine maybes, which is exactly what a one-token classifier is for.
    """
    m = message or ""
    if not m.strip() or should_blueprint(m):
        return False
    if _QUESTION_RE.match(m) or _SPLIT_RE.search(m) or _EDIT_INTO_RE.search(m):
        return False
    if _COMMAND_RE.match(m):
        return False
    if _SINGLE_FILE_ONLY_RE.search(m) and not _APP_NOUN_RE.search(m):
        return False
    # Catches the interrogative openings `_QUESTION_RE` misses — "show me how a
    # login page works" is a question about building, not a request to build.
    if _AMEND_QUESTION_RE.match(m):
        return False
    return bool(_BLUEPRINT_VERB_RE.search(m) or _WANT_RE.search(m))


# ---------------------------------------------------------------------------
# The data model
# ---------------------------------------------------------------------------

TIER_REQUESTED = "requested"
TIER_CORE = "core"
TIER_OPTIONAL = "optional"
_TIERS = (TIER_REQUESTED, TIER_CORE, TIER_OPTIONAL)
# Built by default. Optional is reported, never silently built.
DEFAULT_BUILD_TIERS = (TIER_REQUESTED, TIER_CORE)

_VALID_ACTIONS = ("create", "edit")
# Extensionless filenames that are still legitimate build artifacts.
_EXTENSIONLESS_OK = {
    "readme",
    "dockerfile",
    "makefile",
    "license",
    "procfile",
    ".gitignore",
    ".env",
}

MAX_FEATURES = 20
MAX_ENDPOINTS = 12
MAX_SCHEMA = 8
MAX_BINDINGS = 8
# How many sections `derive_home_page` will link from the home page. A home
# page that lists every route is a sitemap, not a home page.
MAX_HOME_LINKS = 8

_FILENAME_RE = re.compile(r"^[\w./-]+$")


@dataclass(frozen=True)
class PlannedFile:
    """One file the blueprint wants to create/edit — same shape as core.FileOp,
    plus an informational ``role`` (frontend/backend/data/glue)."""

    filename: str
    action: str = "create"
    instruction: str = ""
    role: str = ""
    # Phase C2: entity names this file displays or writes, as DECLARED by the
    # layout call rather than guessed from its instruction prose. This is what
    # `ProjectSpec.from_blueprint` turns into `Page.reads`, which impact
    # analysis and the functional probe both key off.
    reads: tuple[str, ...] = ()


@dataclass(frozen=True)
class Feature:
    """One capability the request implies, tagged with how strongly it's implied."""

    name: str
    tier: str = TIER_CORE
    files: tuple[str, ...] = ()


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str
    request: str = ""  # e.g. "{email, password}"
    response: str = ""  # e.g. "200 {ok, redirect} | 401 {error}"
    # Phase C2, both declared by the layout call: which entity this route reads
    # or writes, and which template it renders. Previously `from_blueprint` had
    # to recover the first by substring-matching the path against table names.
    entity: str = ""
    template: str = ""

    def to_line(self) -> str:
        line = f"{self.method} {self.path}"
        if self.request:
            line += f"  body: {self.request}"
        if self.response:
            line += f"  -> {self.response}"
        if self.entity:
            line += f"  [{self.entity}]"
        return line


@dataclass(frozen=True)
class ApiContract:
    """The canonical interface every file must agree on (the key to frontend and
    backend actually lining up — weaknesses.md #6, applied to behaviour/API)."""

    endpoints: tuple[Endpoint, ...] = ()
    form_bindings: tuple[
        str, ...
    ] = ()  # "#login-form posts to /api/login {email,password}"
    data_schema: tuple[str, ...] = ()  # "users(email TEXT PK, password_hash TEXT)"

    def is_empty(self) -> bool:
        return not (self.endpoints or self.form_bindings or self.data_schema)


@dataclass(frozen=True)
class Blueprint:
    """The whole inferred build, distilled from one short request."""

    summary: str = ""
    features: tuple[Feature, ...] = ()
    files: tuple[PlannedFile, ...] = ()
    contract: ApiContract = field(default_factory=ApiContract)
    stack: Stack = field(default_factory=lambda: NO_STACK)
    # Phase C1: the schema decided BEFORE the layout, structured and diffable.
    # `contract.data_schema` still carries the same tables as free text for the
    # prompt block; this is the authoritative copy that `crud.py` generates the
    # data layer from and `ProjectSpec` stores. Empty = pre-Phase-C behaviour.
    entities: tuple["Entity", ...] = ()

    # -- selection -------------------------------------------------------

    def build_files(self, include_optional: bool = False) -> tuple[PlannedFile, ...]:
        """The files to actually create: everything except files claimed ONLY by
        optional features. A file needed by any built feature is always kept; a
        file no feature mentions (shared styles, README) is always kept."""
        built_tiers = DEFAULT_BUILD_TIERS + (
            (TIER_OPTIONAL,) if include_optional else ()
        )
        built: set[str] = set()
        optional_only: set[str] = set()
        for feat in self.features:
            target = built if feat.tier in built_tiers else optional_only
            for fname in feat.files:
                target.add(fname)
        optional_only -= built  # a file a built feature needs is never dropped
        return tuple(pf for pf in self.files if pf.filename not in optional_only)

    def optional_features(self) -> tuple[Feature, ...]:
        return tuple(f for f in self.features if f.tier == TIER_OPTIONAL)

    def is_actionable(self) -> bool:
        """Only take over routing when the blueprint genuinely EXPANDS the request
        — at least two files to build. One file is what the ordinary flow already
        does well, so defer to it."""
        return len(self.build_files()) >= 2

    def optional_note(self) -> str:
        """A user-facing line offering the optional tier we did not build."""
        opt = self.optional_features()
        if not opt:
            return ""
        names = ", ".join(f.name for f in opt)
        return f"Not built (optional — say the word and I'll add them): {names}."

    # -- prompt threading ------------------------------------------------

    def to_context_block(self) -> str:
        """The interface contract injected into EVERY per-file generation, so the
        form and the server agree on routes, field names, and the data shape.
        Compact by construction — it rides in the same prompt as the plan
        manifest and sibling context, inside `llm_num_ctx`."""
        parts: list[str] = ["## Build blueprint — applies to EVERY file in this build"]
        if self.summary:
            parts.append(self.summary)

        if self.stack and self.stack.backend != "none":
            # "what runs on this machine" was accurate while the stack was purely
            # probed; it is forced now (settings.web_stack), so a stack that
            # isn't installed here still reaches this block. The instruction to
            # the model is unchanged either way — the missing package is the
            # user's to install, and `Stack.install_hint` is what tells them.
            parts.append(
                "### Backend stack — use EXACTLY this, no other framework\n"
                + self.stack.to_prompt_line()
            )

        c = self.contract
        if c.endpoints:
            parts.append(
                "### API endpoints — the frontend calls these EXACT paths; the "
                "backend defines them EXACTLY. Do not rename or invent variants:\n"
                + "\n".join(f"- {e.to_line()}" for e in c.endpoints)
            )
        if c.form_bindings:
            parts.append(
                "### Form ↔ endpoint wiring — the form submits to the named route "
                "with these EXACT field names; the server reads the SAME names:\n"
                + "\n".join(f"- {b}" for b in c.form_bindings)
            )
        if c.data_schema:
            parts.append(
                "### Data — every file that touches storage uses this EXACT shape:\n"
                + "\n".join(f"- {s}" for s in c.data_schema)
            )
        return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Parsing / filtering the LLM's JSON into a Blueprint
# ---------------------------------------------------------------------------


def _norm_filename(raw) -> str:
    """A safe relative filename, or '' if it can't be one."""
    name = str(raw or "").strip().strip("'\"").lstrip("/\\")
    name = name.split("#", 1)[0].split("?", 1)[0].strip()
    if not name or not _FILENAME_RE.match(name) or ".." in name:
        return ""
    basename = name.replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in basename and name.lower() not in _EXTENSIONLESS_OK:
        return ""
    return name


def _norm_tier(raw) -> str:
    t = str(raw or "").strip().lower()
    # A blank/garbled tier means the model listed a file it thinks is needed but
    # fumbled the label — treat as core (built) rather than dropping it; the
    # file cap and the optional-tier gate are the real scope guards.
    return t if t in _TIERS else TIER_CORE


def _clean_files(items) -> tuple[PlannedFile, ...]:
    out: list[PlannedFile] = []
    seen: set[str] = set()
    for item in items or []:
        if isinstance(item, str):
            fname, action, instruction, role = item, "create", "", ""
            reads: tuple[str, ...] = ()
        elif isinstance(item, dict):
            fname = item.get("filename") or item.get("file") or item.get("name")
            action = str(item.get("action") or "create").strip().lower()
            instruction = " ".join(str(item.get("instruction") or "").split())[:400]
            role = str(item.get("role") or "").strip().lower()[:20]
            reads = _norm_idents(item.get("reads"))
        else:
            continue
        fname = _norm_filename(fname)
        if not fname or fname.lower() in seen:
            continue
        if action not in _VALID_ACTIONS:
            action = "create"
        seen.add(fname.lower())
        out.append(PlannedFile(fname, action, instruction, role, reads))
    return tuple(out)


def _clean_features(items, known_files: set[str]) -> tuple[Feature, ...]:
    out: list[Feature] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = " ".join(str(item.get("name") or "").split())[:80]
        if not name:
            continue
        tier = _norm_tier(item.get("tier"))
        raw_files = item.get("files") or []
        files = tuple(
            f
            for f in (_norm_filename(x) for x in raw_files)
            # keep only file links the file list actually contains, so a feature
            # can't drag in a phantom filename
            if f and f.lower() in known_files
        )
        out.append(Feature(name=name, tier=tier, files=files))
        if len(out) >= MAX_FEATURES:
            break
    return tuple(out)


def _clean_endpoints(items) -> tuple[Endpoint, ...]:
    out: list[Endpoint] = []
    seen: set[tuple[str, str]] = set()
    for item in items or []:
        if isinstance(item, str):
            # "POST /api/login" style
            bits = item.split()
            method = bits[0].upper() if bits else ""
            path = bits[1] if len(bits) > 1 else ""
            request = response = ""
            entity = template = ""
        elif isinstance(item, dict):
            method = str(item.get("method") or "GET").strip().upper()
            path = str(item.get("path") or item.get("route") or "").strip()
            request = " ".join(str(item.get("request") or "").split())[:120]
            response = " ".join(str(item.get("response") or "").split())[:120]
            entity = (_norm_idents([item.get("entity")]) or ("",))[0]
            template = _norm_filename(item.get("template"))
        else:
            continue
        if not path.startswith("/") or method not in (
            "GET",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        ):
            continue
        key = (method, path)
        if key in seen:
            continue
        seen.add(key)
        out.append(Endpoint(method, path, request, response, entity, template))
        if len(out) >= MAX_ENDPOINTS:
            break
    return tuple(out)


_ENTITY_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _norm_idents(items, limit: int = 5) -> tuple[str, ...]:
    """Entity names from model output: plain identifiers, lowercased, deduped.

    Mirrors `projectspec._ident` — this module cannot import it (that module
    imports this one), and the rule is short enough that restating it beats
    inventing a shared package for two regexes.
    """
    out: list[str] = []
    for item in items or []:
        name = str(item or "").strip().strip("\"'`").replace("-", "_").lower()[:40]
        if name and _ENTITY_NAME_RE.match(name) and name not in out:
            out.append(name)
        if len(out) >= limit:
            break
    return tuple(out)


def _clean_str_list(items, limit: int, maxlen: int = 160) -> tuple[str, ...]:
    out: list[str] = []
    for item in items or []:
        text = " ".join(str(item or "").split())[:maxlen]
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return tuple(out)


# A feature name that says "this build has a server side" even when the model
# forgot to add the server file to `files`.
_BACKEND_FEATURE_RE = re.compile(
    r"\b(backend|server|api|endpoint|storage|database|persist|auth|store)\b",
    re.IGNORECASE,
)
_BACKEND_FILE_EXTS = (".py", ".go", ".rb", ".php", ".java")


def _has_backend_file(files: tuple[PlannedFile, ...]) -> bool:
    for pf in files:
        if pf.role in ("backend", "server"):
            return True
        basename = pf.filename.rsplit("/", 1)[-1]
        ext = "." + basename.rsplit(".", 1)[-1].lower() if "." in basename else ""
        if ext in _BACKEND_FILE_EXTS:
            return True
    return False


def _server_filename(stack: Stack) -> str:
    return "server.js" if stack.language == "node" else "server.py"


def _ensure_backend(
    files: tuple[PlannedFile, ...],
    features: tuple[Feature, ...],
    contract: ApiContract,
    stack: Stack,
) -> tuple[tuple[PlannedFile, ...], tuple[Feature, ...]]:
    """Deterministic safety net for the commonest 7B blueprint failure.

    The model routinely DECLARES a backend — a `POST /submit` endpoint, or a
    "Backend Server" feature — and then omits the actual server file from
    `files`. That leaves the build non-actionable, so it silently drops back to a
    layout-only page (the exact "no backend / dead button" failure this whole
    feature exists to fix). When the blueprint's OWN output signals a backend but
    no server file is present, synthesize one from the declared contract. Never
    fires when the model signalled nothing (a genuinely static build is left
    static) or the stack has no backend.
    """
    if stack.backend == "none":
        return files, features
    signaled = bool(contract.endpoints) or any(
        _BACKEND_FEATURE_RE.search(f.name) for f in features
    )
    if not signaled or _has_backend_file(files):
        return files, features
    name = _server_filename(stack)
    if any(pf.filename.lower() == name.lower() for pf in files):
        return files, features
    routes = "; ".join(e.to_line() for e in contract.endpoints) or (
        "the routes the frontend calls"
    )
    instr = (
        "Implement the backend server so submissions actually persist. Define "
        f"these routes and a data store: {routes}. Read the form's exact field "
        "names and respond with JSON."
    )
    files = files + (PlannedFile(name, "create", instr, "backend"),)
    features = features + (Feature("Backend server", TIER_CORE, (name,)),)
    return files, features


def _mentions(entity: "Entity", *texts: str) -> bool:
    """Does any of ``texts`` name this entity, by singular or table name?"""
    blob = " ".join(t or "" for t in texts).lower()
    return entity.name.lower() in blob or entity.table.lower() in blob


# Words in a template's name that say "this is the form for creating one", not
# "this is the list of them" — the distinction `derive_pages_from_entities` needs
# to tell which of the two pages an entity is still missing.
#
# Matched against the stem's TOKENS, not with `\b`: `_` is a word character, so
# `\badd\b` does not match `add_product` and the model's own form page would be
# read as a listing, earning the entity a second, duplicate form.
_FORM_WORDS = frozenset({"new", "add", "create", "edit", "update", "form"})
_STEM_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def _is_form_stem(stem: str) -> bool:
    return bool(_FORM_WORDS & set(_STEM_SPLIT_RE.split(stem.lower())))


def derive_pages_from_entities(
    files: tuple[PlannedFile, ...],
    features: tuple[Feature, ...],
    contract: ApiContract,
    entities: tuple["Entity", ...],
    stack: Stack,
) -> tuple[tuple[PlannedFile, ...], tuple[Feature, ...], ApiContract]:
    """Give every stored thing a way in and a way to see it (Phase C3).

    The point of deciding the schema first is that the layout can be *derived*
    from it. The layout call is asked to do that, but a prompt rule is a hope: on
    a 7B model a four-table request routinely comes back with pages for two of
    them. This closes the gap deterministically — the same "deterministic beats
    generated" rule that already produced `scaffold.py` and `crud.py`, applied to
    routes and pages, so "every entity is reachable" is a postcondition rather
    than an aspiration.

    Per entity, three things must exist: a page that LISTS it, a page that
    CREATES one, and the routes behind both. Whatever the model already planned
    is kept as-is — this only fills holes, and it never renames or removes.

    Runs for any stack that ships a SCAFFOLD (Flask, Node — phase N3), because
    a fixed `templates/`+`@app.route` or `views/`+`app.get` layout is what makes
    the synthesized paths correct. On the stdlib or fastapi stacks the file
    layout is the model's own, so guessing filenames there would create files
    nothing serves; those keep whatever the layout call produced.
    """
    adapter = _adapter_for(stack)
    if not entities or adapter is None:
        return files, features, contract

    ext = adapter.template_ext
    tpl_dir = adapter.template_dir
    planned_pages = [pf for pf in files if pf.filename.lower().endswith((".html", ext))]
    new_files: list[PlannedFile] = []
    new_features: list[Feature] = []
    new_endpoints: list[Endpoint] = []
    taken = {pf.filename.lower() for pf in files}
    routes = {(e.method, e.path) for e in contract.endpoints}

    for entity in entities:
        list_tpl = f"{tpl_dir}/{entity.table}{ext}"
        form_tpl = f"{tpl_dir}/new_{entity.name}{ext}"
        list_path = f"/{entity.table}"
        form_path = f"/{entity.table}/new"

        # Already covered? A page counts for this entity if it declared `reads`
        # for it (Phase C2) or its filename names it. `new_*.html` is the form,
        # anything else naming the entity is the listing.
        has_list = False
        has_form = False
        for pf in planned_pages:
            stem = pf.filename.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
            if not (entity.name in pf.reads or _mentions(entity, pf.filename)):
                continue
            if _is_form_stem(stem):
                has_form = True
            else:
                has_list = True

        # Routes are synthesized ONLY for the templates this pass creates, and
        # tracked here per page. Routing the model's own pages too was the first
        # version, and it broke both ways: `GET /<table>` was added beside the
        # model's own listing route (two routes, one of them rendering a
        # template nobody created), while a coarse "this entity already has a
        # GET" guard then swallowed `GET /<table>/new`, leaving the form page
        # unreachable — the dead end this pass exists to prevent. Whatever the
        # model planned, it also routed; the coverage check owns gaps there.
        wants_list_route = False
        wants_form_routes = False

        if not has_list and list_tpl.lower() not in taken:
            taken.add(list_tpl.lower())
            wants_list_route = True
            new_files.append(
                PlannedFile(
                    filename=list_tpl,
                    action="create",
                    instruction=(
                        f"List every row of `{entity.table}` "
                        f"({', '.join(f.name for f in entity.fields)}), one card or "
                        f"table row each, with a link to the page that adds a new "
                        f"{entity.name}. {adapter.page_note} The rows come from the "
                        f"route as a `{entity.table}` variable."
                    ),
                    role="frontend",
                    reads=(entity.name,),
                )
            )
            new_features.append(
                Feature(f"Browse {entity.table}", TIER_CORE, (list_tpl,))
            )

        if not has_form and form_tpl.lower() not in taken:
            writable = [f.name for f in entity.fields if not f.pk]
            taken.add(form_tpl.lower())
            wants_form_routes = True
            new_files.append(
                PlannedFile(
                    filename=form_tpl,
                    action="create",
                    instruction=(
                        f"A form that adds one {entity.name}: "
                        f'<form method="post" action="{form_path}"> with an input '
                        f"named exactly for each of {', '.join(writable)}. "
                        f"{adapter.page_note} No fetch() — a plain form post."
                    ),
                    role="frontend",
                    reads=(entity.name,),
                )
            )
            new_features.append(Feature(f"Add a {entity.name}", TIER_CORE, (form_tpl,)))

        wanted = ()
        if wants_list_route:
            wanted += (("GET", list_path, list_tpl),)
        if wants_form_routes:
            # Both halves: the GET serves the form, the POST accepts it. A form
            # page with only a POST route cannot be opened at all.
            wanted += (("GET", form_path, form_tpl), ("POST", form_path, form_tpl))
        for method, path, tpl in wanted:
            if (method, path) in routes:
                continue
            routes.add((method, path))
            new_endpoints.append(
                Endpoint(
                    method=method,
                    path=path,
                    request=(
                        "{" + ", ".join(f.name for f in entity.fields if not f.pk) + "}"
                        if method == "POST"
                        else ""
                    ),
                    response=(
                        f"302 -> {list_path}" if method == "POST" else "200 HTML page"
                    ),
                    entity=entity.name,
                    template=tpl,
                )
            )

    if not (new_files or new_endpoints):
        return files, features, contract
    return (
        files + tuple(new_files),
        features + tuple(new_features),
        ApiContract(
            endpoints=contract.endpoints + tuple(new_endpoints),
            form_bindings=contract.form_bindings,
            data_schema=contract.data_schema,
        ),
    )


# The scaffold ships `templates/index.html` as a placeholder hero, and
# `scaffold_flask` never overwrites — so the home page was the one page in the
# site that nothing ever wrote. The layout call is not asked for it (the Flask
# layout table names `templates/<page>.html` and no prompt mentions a home
# page), and `derive_pages_from_entities` covers entities, which the home page
# belongs to none of. Every build therefore shipped the same "This project was
# scaffolded by Coder and is already running" block as its front door.
HOME_TEMPLATE = "templates/index.html"


def _adapter_for(stack: Stack):
    """The stack adapter that OWNS this build's file layout, or None.

    None means "this stack has no scaffold, so its file layout is the model's
    own" — stdlib, fastapi, none — and the derivation passes then leave the plan
    exactly as the layout call produced it, which is the pre-N3 behaviour for
    everything except Node.

    Imported inside the function: `stacks.flask_adapter` -> `scaffold` -> this
    module, so a top-level import would close the cycle.
    """
    if stack is None:
        return None
    from app.agent.stacks import get_adapter, key_for_stack

    adapter = get_adapter(key_for_stack(stack.language, stack.backend))
    return adapter if stack.backend in adapter.backends else None


def _home_label(path: str, template: str) -> str:
    """A human label for a section the home page links to (`/products` -> Products)."""
    stem = (path or "").strip("/").split("/")[0]
    if not stem:
        stem = template.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return " ".join(w.capitalize() for w in _STEM_SPLIT_RE.split(stem.lower()) if w)


def derive_home_page(
    files: tuple[PlannedFile, ...],
    features: tuple[Feature, ...],
    contract: ApiContract,
    stack: Stack,
) -> tuple[tuple[PlannedFile, ...], tuple[Feature, ...]]:
    """Put the home page in the build plan, since nothing else ever does.

    Same rule as `derive_pages_from_entities` and `scaffold.py` before it —
    deterministic beats generated — applied to the one page every visitor sees
    first. Planning it is all that is needed: it is not `_FROZEN`, so
    `_file_op_flow` routes the existing scaffold file to `_surgical_edit`, and
    `template_edit_region` confines that edit to `{% block content %}`. The
    action is therefore `edit`, which is also what the plan manifest should say.

    **Whatever the layout call planned wins.** If it named `templates/index.html`
    itself, this returns untouched — planning it twice would put the same file
    through two generation passes, the second overwriting the first.

    Links come from the contract's own GET endpoints rather than from the entity
    list, so the model's own pages are reachable too. Form pages are skipped ("add
    a product" is a button on the products page, not a section of the site) and so
    are parameterised routes, which have no fixed URL to link.

    Scaffolded stacks only, for `derive_pages_from_entities`' reason: the fixed
    layout is what makes the path correct. On a stack with no scaffold the file
    layout is the model's own and this would name a file nothing serves.
    """
    adapter = _adapter_for(stack)
    if adapter is None:
        return files, features
    home = adapter.home_template.lower()
    if any(pf.filename.replace("\\", "/").lower() == home for pf in files):
        return files, features

    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for endpoint in contract.endpoints:
        path = endpoint.path or ""
        if (endpoint.method or "").upper() != "GET" or not endpoint.template:
            continue
        if path in ("", "/") or path in seen or "<" in path:
            continue
        stem = endpoint.template.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if _is_form_stem(stem):
            continue
        label = _home_label(path, endpoint.template)
        if not label:
            continue
        seen.add(path)
        links.append((label, path))
        if len(links) >= MAX_HOME_LINKS:
            break

    if links:
        body = (
            'then a `<div class="grid">` holding one card per section of the '
            "site, each linking to its page: "
            + ", ".join(f"{label} ({path})" for label, path in links)
            + "."
        )
    else:
        body = (
            "then one short paragraph saying what a visitor can do here, linking "
            "only to pages that exist."
        )

    instruction = (
        "The home page a visitor lands on. It currently holds the scaffold's "
        "placeholder text, which must be REPLACED — none of it belongs in the "
        "finished site. Write: a "
        '`<section class="hero">` with an `<h1>` naming this site and a '
        '`<p class="lede">` saying in one sentence what it is for, '
        + body
        + " "
        + adapter.home_edit_note
        + " Use the shipped macros and classes — write no new CSS and no "
        "`<style>` block."
    )

    return (
        files
        + (
            PlannedFile(
                filename=adapter.home_template,
                action="edit",
                instruction=instruction,
                role="frontend",
            ),
        ),
        features + (Feature("Home page", TIER_CORE, (adapter.home_template,)),),
    )


def blueprint_from_data(
    data: dict | None,
    message: str,
    stack: Stack | None = None,
    entities: tuple["Entity", ...] = (),
) -> Blueprint:
    """Turn a parsed extraction response into a filtered Blueprint.

    ``data`` may be None (the LLM call failed) → an empty, non-actionable
    blueprint, so the caller falls back to ordinary routing. Everything is
    bounded and validated: filenames must be safe relative paths, tiers must be
    known, endpoints must have a method and an absolute path.

    ``entities`` is the schema decided by the Phase C1 call. When present it is
    AUTHORITATIVE: `data_schema` is printed from it rather than taken from the
    model's own free text, so the tables the data layer generates and the tables
    the prompt describes are the same tables by construction. Empty entities
    leave every line of this function behaving as it did before Phase C.
    """
    data = data if isinstance(data, dict) else {}
    stack = stack or NO_STACK

    summary = " ".join(str(data.get("summary") or "").split())[:300]
    files = _clean_files(data.get("files"))
    known = {f.filename.lower() for f in files}
    features = _clean_features(data.get("features"), known)

    contract_raw = data.get("contract")
    contract_raw = contract_raw if isinstance(contract_raw, dict) else {}
    contract = ApiContract(
        endpoints=_clean_endpoints(contract_raw.get("endpoints")),
        form_bindings=_clean_str_list(contract_raw.get("form_bindings"), MAX_BINDINGS),
        data_schema=(
            tuple(e.summary() for e in entities[:MAX_SCHEMA])
            if entities
            else _clean_str_list(contract_raw.get("data_schema"), MAX_SCHEMA)
        ),
    )

    # Net for the "declared a backend, forgot the file" failure (7B under-spec).
    files, features = _ensure_backend(files, features, contract, stack)
    # Phase C3: every entity gets a list page, a create form and their routes.
    files, features, contract = derive_pages_from_entities(
        files, features, contract, entities, stack
    )
    # After the pages exist, so the home page can link the routes they added.
    files, features = derive_home_page(files, features, contract, stack)

    return Blueprint(
        summary=summary,
        features=features,
        files=files,
        contract=contract,
        stack=stack,
        entities=entities,
    )
