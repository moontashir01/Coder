"""Tests for cross-file reference checking + auto-create (weaknesses.md #2/#3).

All offline: extraction/resolution are pure, the auto-create integration uses a
scripted LLM and writes to tmp_path.
"""

from types import SimpleNamespace

from app.agent.core import AgentCore
from app.agent.references import (
    extract_local_references,
    extract_nav_block,
    find_dead_references,
    is_creatable,
)


class ScriptedLLM:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    def invoke(self, messages):
        out = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        return SimpleNamespace(content=out)


# ---------------------------------------------------------------------------
# extract_local_references
# ---------------------------------------------------------------------------


def test_extract_html_local_refs():
    html = (
        "<!DOCTYPE html><html><head>"
        '<link rel="stylesheet" href="styles.css">'
        '<link rel="stylesheet" href="https://cdn.example.com/x.css">'
        "</head><body>"
        '<img src="assets/logo.png">'
        '<a href="about.html">About</a>'
        '<a href="/contact">Contact</a>'  # route, not a file → ignored
        '<a href="#top">Top</a>'  # anchor → ignored
        '<script src="script.js"></script>'
        '<script src="//cdn.example.com/lib.js"></script>'  # external → ignored
        "<script>console.log('inline')</script>"  # inline, no src → ignored
        "</body></html>"
    )
    refs = extract_local_references(html, ".html")
    assert refs == ["styles.css", "about.html", "assets/logo.png", "script.js"]


def test_extract_html_strips_query_and_fragment():
    html = '<link href="styles.css?v=3"><script src="app.js#x"></script>'
    assert extract_local_references(html, ".html") == ["styles.css", "app.js"]


def test_extract_css_refs():
    css = (
        '@import "base.css";\n'
        "@import url(theme.css);\n"
        "body { background: url('bg.png'); }\n"
        "h1 { background: url(https://cdn/x.png); }\n"  # external → ignored
        "p { background: url(data:image/png;base64,AAAA); }\n"  # data → ignored
    )
    assert extract_local_references(css, ".css") == ["base.css", "theme.css", "bg.png"]


def test_extract_js_relative_imports_only():
    js = (
        "import { a } from './util.js';\n"
        "import './styles.css';\n"
        "const x = require('../lib/helper');\n"
        "import React from 'react';\n"  # npm package → ignored
        "const y = await import('./lazy.js');\n"
    )
    assert extract_local_references(js, ".js") == [
        "./util.js",
        "./styles.css",
        "../lib/helper",
        "./lazy.js",
    ]


def test_extract_ignores_unknown_type():
    assert extract_local_references("anything href='x.css'", ".py") == []


def test_is_creatable():
    assert is_creatable("script.js") is True
    assert is_creatable("styles.css") is True
    assert is_creatable("about.html") is True
    assert is_creatable("./util") is True  # extensionless JS module
    assert is_creatable("logo.png") is False
    assert is_creatable("font.woff2") is False


# ---------------------------------------------------------------------------
# find_dead_references
# ---------------------------------------------------------------------------


def test_find_dead_references_reports_missing(tmp_path):
    (tmp_path / "styles.css").write_text("body{}", encoding="utf-8")  # exists
    index = tmp_path / "index.html"
    index.write_text(
        '<link href="styles.css"><script src="script.js"></script>',
        encoding="utf-8",
    )
    dead = find_dead_references(index, tmp_path)
    names = [ref for ref, _ in dead]
    assert names == ["script.js"]  # styles.css exists, script.js does not


def test_find_dead_references_resolves_subdirs(tmp_path):
    (tmp_path / "js").mkdir()
    page = tmp_path / "index.html"
    page.write_text('<script src="js/app.js"></script>', encoding="utf-8")
    dead = find_dead_references(page, tmp_path)
    assert [ref for ref, _ in dead] == ["js/app.js"]
    resolved = dead[0][1]
    assert resolved == (tmp_path / "js" / "app.js").resolve()


def test_find_dead_references_js_extension_candidates(tmp_path):
    (tmp_path / "util.js").write_text("export const a = 1;", encoding="utf-8")
    main = tmp_path / "main.js"
    main.write_text("import './util';\nimport './missing';", encoding="utf-8")
    dead = find_dead_references(main, tmp_path)
    # './util' resolves to util.js (present); './missing' has no candidate.
    assert [ref for ref, _ in dead] == ["./missing"]


def test_find_dead_references_skips_escaping_root(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    page = sub / "index.html"
    page.write_text('<script src="../../outside.js"></script>', encoding="utf-8")
    # Resolves outside tmp_path → not reported (never touch outside the sandbox).
    assert find_dead_references(page, tmp_path) == []


def test_find_dead_references_none_when_all_present(tmp_path):
    (tmp_path / "a.css").write_text("body{}", encoding="utf-8")
    (tmp_path / "b.js").write_text("x=1", encoding="utf-8")
    page = tmp_path / "index.html"
    page.write_text('<link href="a.css"><script src="b.js"></script>', encoding="utf-8")
    assert find_dead_references(page, tmp_path) == []


# ---------------------------------------------------------------------------
# AgentCore._repair_dead_references — end-to-end auto-create
# ---------------------------------------------------------------------------


def _write_trace(path):
    return [
        {
            "tool": "write_file",
            "arguments": {"path": str(path)},
            "result": {"success": True, "result": "ok", "error": None},
        }
    ]


async def test_repair_creates_missing_referenced_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    index = tmp_path / "index.html"
    index.write_text(
        '<html><body><script src="script.js"></script></body></html>',
        encoding="utf-8",
    )
    a = AgentCore(session_id="pytest_ref_create")
    a._llm_direct = ScriptedLLM(["FILENAME: script.js\nconsole.log('todo app');"])

    note, trace = await a._repair_dead_references(_write_trace(index))

    created = tmp_path / "script.js"
    assert created.is_file()
    assert "console.log" in created.read_text(encoding="utf-8")
    assert "created 1" in note.lower()
    assert "script.js" in note
    assert trace and trace[0]["tool"] == "write_file"


async def test_repair_reports_but_does_not_fabricate_binary_asset(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    index = tmp_path / "index.html"
    index.write_text('<html><body><img src="logo.png"></body></html>', encoding="utf-8")
    a = AgentCore(session_id="pytest_ref_binary")
    a._llm_direct = ScriptedLLM(["should not be called"])

    note, trace = await a._repair_dead_references(_write_trace(index))

    assert not (tmp_path / "logo.png").exists()  # never fabricated
    assert a._llm_direct.calls == 0  # no generation for a binary asset
    assert "logo.png" in note
    assert "not auto-created" in note.lower()


async def test_repair_noop_when_all_references_resolve(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "script.js").write_text("console.log(1)", encoding="utf-8")
    index = tmp_path / "index.html"
    index.write_text(
        '<html><body><script src="script.js"></script></body></html>',
        encoding="utf-8",
    )
    a = AgentCore(session_id="pytest_ref_noop")
    a._llm_direct = ScriptedLLM(["should not be called"])

    note, trace = await a._repair_dead_references(_write_trace(index))

    assert note == ""
    assert trace == []
    assert a._llm_direct.calls == 0


async def test_repair_does_not_hijack_last_write_target(tmp_path, monkeypatch):
    """Auto-creating a dependency must not steal the follow-up edit target."""
    monkeypatch.chdir(tmp_path)
    index = tmp_path / "index.html"
    index.write_text(
        '<html><body><script src="script.js"></script></body></html>',
        encoding="utf-8",
    )
    a = AgentCore(session_id="pytest_ref_lastwrite")
    a._last_write_path = str(index.resolve())  # the primary artifact
    a._llm_direct = ScriptedLLM(["FILENAME: script.js\nconsole.log('x');"])

    await a._repair_dead_references(_write_trace(index))

    # Still points at index.html, not the auto-created script.js.
    assert a._last_write_path == str(index.resolve())


async def test_repair_respects_max_reference_repairs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from config.settings import settings

    monkeypatch.setattr(settings, "max_reference_repairs", 1)
    index = tmp_path / "index.html"
    index.write_text(
        "<html><body>"
        '<script src="a.js"></script><script src="b.js"></script>'
        "</body></html>",
        encoding="utf-8",
    )
    a = AgentCore(session_id="pytest_ref_cap")
    a._llm_direct = ScriptedLLM(["FILENAME: a.js\nconsole.log('a');"])

    note, trace = await a._repair_dead_references(_write_trace(index))

    created = [p.name for p in tmp_path.iterdir() if p.suffix == ".js"]
    assert len(created) == 1  # capped at one despite two dead references


# ---------------------------------------------------------------------------
# extract_nav_block
# ---------------------------------------------------------------------------


def test_extract_nav_block_prefers_nav_element():
    html = (
        "<html><head><title>x</title></head><body>"
        '<nav class="main"><a href="index.html">Home</a></nav>'
        "<header><a href='other.html'>H</a></header>"
        "</body></html>"
    )
    nav = extract_nav_block(html)
    assert nav.startswith('<nav class="main">')
    assert nav.endswith("</nav>")


def test_extract_nav_block_falls_back_to_linking_header():
    html = "<body><header><a href='about.html'>About</a></header></body>"
    assert extract_nav_block(html).startswith("<header>")


def test_extract_nav_block_ignores_header_without_links():
    html = "<body><header><h1>Just a title</h1></header></body>"
    assert extract_nav_block(html) is None


def test_extract_nav_block_none_when_absent():
    assert extract_nav_block("<body><p>hi</p></body>") is None

