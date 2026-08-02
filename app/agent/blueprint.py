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

from app.agent.runtime_probe import NO_STACK, Stack

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
_EDIT_INTO_RE = re.compile(
    r"\b(add|append|insert|put|move|include)\b[^.]*?\b(to|into|in)\b",
    re.IGNORECASE,
)

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
    if _SINGLE_FILE_ONLY_RE.search(m):
        return False
    if _SPLIT_RE.search(m):
        return False
    if _EDIT_INTO_RE.search(m):
        return False
    return bool(_BLUEPRINT_VERB_RE.search(m) and _BLUEPRINT_NOUN_RE.search(m))


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

_FILENAME_RE = re.compile(r"^[\w./-]+$")


@dataclass(frozen=True)
class PlannedFile:
    """One file the blueprint wants to create/edit — same shape as core.FileOp,
    plus an informational ``role`` (frontend/backend/data/glue)."""

    filename: str
    action: str = "create"
    instruction: str = ""
    role: str = ""


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

    def to_line(self) -> str:
        line = f"{self.method} {self.path}"
        if self.request:
            line += f"  body: {self.request}"
        if self.response:
            line += f"  -> {self.response}"
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
        elif isinstance(item, dict):
            fname = item.get("filename") or item.get("file") or item.get("name")
            action = str(item.get("action") or "create").strip().lower()
            instruction = " ".join(str(item.get("instruction") or "").split())[:400]
            role = str(item.get("role") or "").strip().lower()[:20]
        else:
            continue
        fname = _norm_filename(fname)
        if not fname or fname.lower() in seen:
            continue
        if action not in _VALID_ACTIONS:
            action = "create"
        seen.add(fname.lower())
        out.append(PlannedFile(fname, action, instruction, role))
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
        elif isinstance(item, dict):
            method = str(item.get("method") or "GET").strip().upper()
            path = str(item.get("path") or item.get("route") or "").strip()
            request = " ".join(str(item.get("request") or "").split())[:120]
            response = " ".join(str(item.get("response") or "").split())[:120]
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
        out.append(Endpoint(method, path, request, response))
        if len(out) >= MAX_ENDPOINTS:
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


def blueprint_from_data(
    data: dict | None, message: str, stack: Stack | None = None
) -> Blueprint:
    """Turn a parsed extraction response into a filtered Blueprint.

    ``data`` may be None (the LLM call failed) → an empty, non-actionable
    blueprint, so the caller falls back to ordinary routing. Everything is
    bounded and validated: filenames must be safe relative paths, tiers must be
    known, endpoints must have a method and an absolute path.
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
        data_schema=_clean_str_list(contract_raw.get("data_schema"), MAX_SCHEMA),
    )

    # Net for the "declared a backend, forgot the file" failure (7B under-spec).
    files, features = _ensure_backend(files, features, contract, stack)

    return Blueprint(
        summary=summary,
        features=features,
        files=files,
        contract=contract,
        stack=stack,
    )
