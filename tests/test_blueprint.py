"""Requirements Blueprint (app/agent/blueprint.py, runtime_probe.py) + its seam
in AgentCore.chat().

All offline: the gate/parsing are pure functions; the seam tests monkeypatch the
one LLM-calling method (`_expand_requirements`) and `_multi_file_flow`, so no
real Ollama is ever reached.
"""

from types import SimpleNamespace

import pytest

from app.agent.blueprint import (
    ApiContract,
    Blueprint,
    Endpoint,
    Feature,
    PlannedFile,
    TIER_CORE,
    TIER_OPTIONAL,
    TIER_REQUESTED,
    blueprint_from_data,
    should_blueprint,
)
from app.agent.core import AgentCore
from app.agent.runtime_probe import STDLIB_STACK, Stack, detect_stack
from config.settings import settings


class ScriptedLLM:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    def invoke(self, messages):
        out = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        return SimpleNamespace(content=out)


# ---------------------------------------------------------------------------
# should_blueprint — the gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msg",
    [
        "build me a login page",
        "create a todo app",
        "make a dashboard for sales figures",
        "scaffold a blog website",
        "design a signup form",
        "build an ecommerce store",
        "generate a full stack CRUD app",
        "implement a login system",
    ],
)
def test_should_blueprint_fires_on_greenfield_builds(msg):
    assert should_blueprint(msg) is True


@pytest.mark.parametrize(
    "msg",
    [
        "explain how a login page works",  # question
        "what does a login page need?",  # question
        "add a login form to index.html",  # incremental edit
        "put a button into the page",  # incremental edit
        "split styles.css into two files",  # explicit split
        "refactor the login page",  # refactor
        "create a css file",  # single-file request
        "make a new html file",  # single-file request
        "write a python function that adds two numbers",  # snippet, no build verb+noun
        "fix the navbar",  # repair, no build verb
    ],
)
def test_should_blueprint_skips_non_greenfield(msg):
    assert should_blueprint(msg) is False


# ---------------------------------------------------------------------------
# detect_stack — grounded in what's installed
# ---------------------------------------------------------------------------


def test_detect_stack_defaults_to_stdlib_when_nothing_present():
    stack = detect_stack(_has_module=lambda n: False, _which=lambda n: None)
    assert stack.backend == "stdlib"
    assert stack.language == "python"
    assert stack.runnable is True


def test_detect_stack_prefers_flask_when_installed():
    stack = detect_stack(_has_module=lambda n: n == "flask", _which=lambda n: None)
    assert stack.backend == "flask"


def test_detect_stack_uses_fastapi_when_only_fastapi_present():
    stack = detect_stack(_has_module=lambda n: n == "fastapi", _which=lambda n: None)
    assert stack.backend == "fastapi"


def test_detect_stack_none_prefers_no_backend():
    stack = detect_stack(prefer="none")
    assert stack.backend == "none"


def test_detect_stack_node_needs_network_to_use_express():
    node_which = lambda n: "/usr/bin/node" if n == "node" else None
    # Node present but offline → can't confirm express is vendored → stdlib.
    offline = detect_stack(
        allow_network=False, _has_module=lambda n: False, _which=node_which
    )
    assert offline.backend == "stdlib"
    # Node present and network allowed → express is fair game.
    online = detect_stack(
        allow_network=True, _has_module=lambda n: False, _which=node_which
    )
    assert online.language == "node"
    assert online.backend == "express"


# ---------------------------------------------------------------------------
# blueprint_from_data — parsing, filtering, tiers
# ---------------------------------------------------------------------------


def _login_data():
    return {
        "summary": "A login page with email/password auth and password reset",
        "features": [
            {"name": "Login form", "tier": "requested", "files": ["login.html", "login.js"]},
            {"name": "Auth backend", "tier": "core", "files": ["server.py"]},
            {"name": "OAuth sign-in", "tier": "optional", "files": ["oauth.py"]},
            {"name": "Phantom", "tier": "core", "files": ["does-not-exist.py"]},
        ],
        "files": [
            {"filename": "login.html", "action": "create", "role": "frontend"},
            {"filename": "login.js", "action": "create", "role": "frontend"},
            {"filename": "server.py", "action": "create", "role": "backend"},
            {"filename": "oauth.py", "action": "create", "role": "backend"},
            {"filename": "../evil.py"},  # path escape → rejected
            {"filename": "passwd"},  # extensionless, not allowlisted → rejected
        ],
        "contract": {
            "endpoints": [
                {
                    "method": "post",
                    "path": "/api/login",
                    "request": "{email, password}",
                    "response": "200 {ok} | 401 {error}",
                },
                {"method": "GET", "path": "no-leading-slash"},  # rejected
            ],
            "form_bindings": ["#login-form submits POST /api/login with email, password"],
            "data_schema": ["users(email TEXT PRIMARY KEY, password_hash TEXT)"],
        },
    }


def test_blueprint_parses_and_sanitizes_files():
    bp = blueprint_from_data(_login_data(), "build me a login page", STDLIB_STACK)
    names = {f.filename for f in bp.files}
    assert names == {"login.html", "login.js", "server.py", "oauth.py"}
    assert "../evil.py" not in names
    assert "passwd" not in names
    assert bp.stack.backend == "stdlib"


def test_blueprint_endpoints_validated_and_normalized():
    bp = blueprint_from_data(_login_data(), "build me a login page", STDLIB_STACK)
    assert len(bp.contract.endpoints) == 1  # the no-leading-slash one is dropped
    ep = bp.contract.endpoints[0]
    assert ep.method == "POST"  # lowercased input uppercased
    assert ep.path == "/api/login"


def test_blueprint_feature_files_filtered_to_known():
    bp = blueprint_from_data(_login_data(), "build me a login page", STDLIB_STACK)
    phantom = next(f for f in bp.features if f.name == "Phantom")
    assert phantom.files == ()  # does-not-exist.py isn't in the file list


def test_blueprint_default_build_excludes_optional_only_files():
    bp = blueprint_from_data(_login_data(), "build me a login page", STDLIB_STACK)
    default = {f.filename for f in bp.build_files()}
    assert default == {"login.html", "login.js", "server.py"}  # no oauth.py
    with_opt = {f.filename for f in bp.build_files(include_optional=True)}
    assert "oauth.py" in with_opt


def test_blueprint_optional_note_offers_unbuilt_features():
    bp = blueprint_from_data(_login_data(), "build me a login page", STDLIB_STACK)
    note = bp.optional_note()
    assert "OAuth sign-in" in note


def test_blueprint_context_block_states_the_contract():
    bp = blueprint_from_data(_login_data(), "build me a login page", STDLIB_STACK)
    block = bp.to_context_block()
    assert "/api/login" in block
    assert "users(email TEXT PRIMARY KEY" in block
    assert "standard library" in block.lower()  # the chosen stack is stated


def test_blueprint_unknown_tier_defaults_to_core_and_is_built():
    data = {
        "files": [{"filename": "a.py"}, {"filename": "b.py"}],
        "features": [{"name": "X", "tier": "banana", "files": ["a.py"]}],
    }
    bp = blueprint_from_data(data, "build a thing", STDLIB_STACK)
    assert bp.features[0].tier == TIER_CORE
    assert {f.filename for f in bp.build_files()} == {"a.py", "b.py"}


def test_blueprint_actionable_needs_two_build_files():
    one = blueprint_from_data(
        {"files": [{"filename": "index.html"}]}, "build a page", STDLIB_STACK
    )
    assert one.is_actionable() is False
    two = blueprint_from_data(
        {"files": [{"filename": "index.html"}, {"filename": "app.js"}]},
        "build a page",
        STDLIB_STACK,
    )
    assert two.is_actionable() is True


def test_blueprint_from_none_is_empty_and_inert():
    bp = blueprint_from_data(None, "build a page", STDLIB_STACK)
    assert bp.is_actionable() is False
    assert bp.build_files() == ()


# ---------------------------------------------------------------------------
# _ensure_backend — synthesize the server file the model declared but forgot
# ---------------------------------------------------------------------------


def test_ensure_backend_synthesizes_from_declared_endpoint():
    """The exact live failure: contact form declares POST /submit + a 'Backend
    Server' feature but no server file in `files` → net adds one."""
    data = {
        "files": [{"filename": "contact.html", "role": "frontend"}],
        "features": [
            {"name": "Contact Form", "tier": "requested", "files": ["contact.html"]},
            {"name": "Backend Server", "tier": "core", "files": []},
        ],
        "contract": {
            "endpoints": [
                {"method": "POST", "path": "/submit", "request": "{name,email,message}"}
            ]
        },
    }
    bp = blueprint_from_data(data, "make a contact form", STDLIB_STACK)
    names = [f.filename for f in bp.build_files()]
    assert "server.py" in names
    assert bp.is_actionable() is True  # was 1 file, now 2


def test_ensure_backend_from_feature_name_without_endpoint():
    data = {
        "files": [{"filename": "todo.html"}],
        "features": [{"name": "Backend Logic", "tier": "core", "files": []}],
    }
    bp = blueprint_from_data(data, "build a todo app", STDLIB_STACK)
    assert "server.py" in [f.filename for f in bp.build_files()]


def test_ensure_backend_not_added_when_no_signal():
    """A genuinely static build (no endpoints, no backend feature) is left alone."""
    data = {
        "files": [{"filename": "index.html"}, {"filename": "styles.css"}],
        "features": [{"name": "Landing hero", "tier": "requested", "files": ["index.html"]}],
    }
    bp = blueprint_from_data(data, "build a landing page", STDLIB_STACK)
    assert "server.py" not in [f.filename for f in bp.files]


def test_ensure_backend_not_duplicated_when_present():
    data = {
        "files": [
            {"filename": "login.html"},
            {"filename": "server.py", "role": "backend"},
        ],
        "contract": {"endpoints": [{"method": "POST", "path": "/api/login"}]},
    }
    bp = blueprint_from_data(data, "build a login page", STDLIB_STACK)
    server_files = [f for f in bp.files if f.filename == "server.py"]
    assert len(server_files) == 1  # not duplicated


def test_ensure_backend_uses_node_filename_for_node_stack():
    node = Stack("node", "express", True, "node available")
    data = {
        "files": [{"filename": "index.html"}],
        "contract": {"endpoints": [{"method": "GET", "path": "/api/todos"}]},
    }
    bp = blueprint_from_data(data, "build a todo app", node)
    names = [f.filename for f in bp.build_files()]
    assert "server.js" in names
    assert "server.py" not in names


# ---------------------------------------------------------------------------
# The chat() seam — inert when off, drives _multi_file_flow when on
# ---------------------------------------------------------------------------


def _actionable_blueprint():
    return Blueprint(
        summary="A login page with auth",
        features=(
            Feature("Login", TIER_REQUESTED, ("login.html",)),
            Feature("Backend", TIER_CORE, ("server.py",)),
            Feature("OAuth", TIER_OPTIONAL, ("oauth.py",)),
        ),
        files=(
            PlannedFile("login.html", "create", "the form"),
            PlannedFile("server.py", "create", "the /api/login route"),
            PlannedFile("oauth.py", "create", "optional oauth"),
        ),
        contract=ApiContract(
            endpoints=(Endpoint("POST", "/api/login", "{email,password}", "200|401"),),
        ),
        stack=STDLIB_STACK,
    )


async def test_seam_inert_when_flag_off(tmp_path, monkeypatch):
    """Flag off (the default): the blueprint method is never even consulted, and
    a build request routes exactly as it does today."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "expand_requirements", False)
    a = AgentCore(session_id="pytest_bp_off")

    async def _boom(*args, **kwargs):
        raise AssertionError("_expand_requirements must not run when flag is off")

    monkeypatch.setattr(a, "_expand_requirements", _boom)
    a._llm_direct = ScriptedLLM(["FILENAME: login.html\n<html></html>"])

    answer, trace = await a.chat("build me a login page")

    assert (tmp_path / "login.html").is_file()  # ordinary single-file flow ran


async def test_seam_skips_non_build_when_flag_on(tmp_path, monkeypatch):
    """Flag on, but a question isn't a build — the gate skips expansion."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "expand_requirements", True)
    a = AgentCore(session_id="pytest_bp_question")

    async def _boom(*args, **kwargs):
        raise AssertionError("_expand_requirements must not run for a question")

    monkeypatch.setattr(a, "_expand_requirements", _boom)

    routed = {}

    async def _fake_route(msg, refs, **kwargs):
        routed["msg"] = msg
        return "answered", []

    monkeypatch.setattr(a, "_route_one", _fake_route)

    answer, _ = await a.chat("explain how a login page works")
    assert answer == "answered"


async def test_seam_runs_blueprint_when_on_and_actionable(tmp_path, monkeypatch):
    """Flag on + a greenfield build + an actionable blueprint → the blueprint's
    files become preplanned_ops and its contract rides in extra_context."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "expand_requirements", True)
    monkeypatch.setattr(settings, "blueprint_optional_tier", False)
    a = AgentCore(session_id="pytest_bp_on")

    async def _fake_expand(msg):
        return _actionable_blueprint()

    monkeypatch.setattr(a, "_expand_requirements", _fake_expand)

    captured = {}

    async def _fake_mff(user_message, refs, extra_context="", preplanned_ops=None):
        captured["ops"] = preplanned_ops
        captured["extra"] = extra_context
        return "Handled 2 file(s)", []  # empty trace → ref-repair block skipped

    monkeypatch.setattr(a, "_multi_file_flow", _fake_mff)

    answer, _ = await a.chat("build me a login page")

    ops = captured["ops"]
    assert [o.filename for o in ops] == ["login.html", "server.py"]  # oauth excluded
    assert "/api/login" in captured["extra"]  # contract threaded in
    assert "OAuth" in answer  # optional feature reported, not built


async def test_seam_falls_through_when_blueprint_not_actionable(tmp_path, monkeypatch):
    """A one-file blueprint doesn't expand anything → defer to normal routing."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "expand_requirements", True)
    a = AgentCore(session_id="pytest_bp_thin")

    async def _fake_expand(msg):
        return Blueprint(files=(PlannedFile("index.html"),))  # 1 file, not actionable

    monkeypatch.setattr(a, "_expand_requirements", _fake_expand)

    async def _boom(*args, **kwargs):
        raise AssertionError("_run_blueprint must not run for a thin blueprint")

    monkeypatch.setattr(a, "_run_blueprint", _boom)

    async def _fake_route(msg, refs, **kwargs):
        return "normal routing", []

    monkeypatch.setattr(a, "_route_one", _fake_route)

    answer, _ = await a.chat("build me a login page")
    assert answer == "normal routing"


# ---------------------------------------------------------------------------
# Blueprint coverage verification (Phase 2) — weaknesses.md #3
# ---------------------------------------------------------------------------


def _bp_with(files, endpoints=()):
    return Blueprint(
        summary="a build",
        features=(),
        files=tuple(PlannedFile(f) for f in files),
        contract=ApiContract(endpoints=tuple(endpoints)),
        stack=STDLIB_STACK,
    )


def test_unwired_endpoints_flags_routes_absent_from_backend(tmp_path):
    a = AgentCore(session_id="pytest_unwired")
    (tmp_path / "server.py").write_text("# handles /api/login here\n", encoding="utf-8")
    bp = _bp_with(
        ["server.py"],
        endpoints=[
            Endpoint("POST", "/api/login"),
            Endpoint("POST", "/api/reset"),  # not in server.py
        ],
    )
    unwired = a._unwired_endpoints(bp, tmp_path)
    assert unwired == ["/api/reset"]


def test_unwired_endpoints_empty_when_all_defined(tmp_path):
    a = AgentCore(session_id="pytest_wired")
    (tmp_path / "server.py").write_text(
        "routes = ['/api/login', '/api/reset']\n", encoding="utf-8"
    )
    bp = _bp_with(
        ["server.py"],
        endpoints=[Endpoint("POST", "/api/login"), Endpoint("POST", "/api/reset")],
    )
    assert a._unwired_endpoints(bp, tmp_path) == []


async def test_coverage_creates_missing_planned_file(tmp_path, monkeypatch):
    """The exact failure the user reported: the backend file never got written.
    Coverage creates it, threading the contract."""
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_cov_create")
    (tmp_path / "login.html").write_text("<html></html>", encoding="utf-8")
    a._llm_direct = ScriptedLLM(
        ["FILENAME: server.py\nprint('serving /api/login')\n"]
    )
    bp = _bp_with(
        ["login.html", "server.py"],  # login.html exists, server.py is missing
        endpoints=[Endpoint("POST", "/api/login")],
    )

    note, cov_trace = await a._verify_blueprint_coverage(bp, [])

    assert (tmp_path / "server.py").is_file()
    assert "server.py" in note
    assert "Created missing" in note


async def test_coverage_reports_unwired_endpoint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AgentCore(session_id="pytest_cov_report")
    (tmp_path / "server.py").write_text("# only /api/login\n", encoding="utf-8")
    bp = _bp_with(
        ["server.py"],
        endpoints=[Endpoint("POST", "/api/login"), Endpoint("POST", "/api/reset")],
    )

    note, _ = await a._verify_blueprint_coverage(bp, [])
    assert "may not meet" in note
    assert "/api/reset" in note


async def test_coverage_inert_when_no_blueprint_ran(tmp_path, monkeypatch):
    """A non-blueprint turn (self._blueprint is None) never runs coverage."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "expand_requirements", False)
    a = AgentCore(session_id="pytest_cov_inert")

    async def _boom(*args, **kwargs):
        raise AssertionError("coverage must not run when no blueprint drove the turn")

    monkeypatch.setattr(a, "_verify_blueprint_coverage", _boom)
    a._llm_direct = ScriptedLLM(["FILENAME: notes.md\n# notes\n"])

    await a.chat("create a notes.md file")  # ordinary single-file turn
    assert (tmp_path / "notes.md").is_file()
