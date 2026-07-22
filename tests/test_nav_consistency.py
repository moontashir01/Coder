"""Deterministic post-generation checks: one navbar per build, one asset per job.

Gap 3 — every page written in a turn must carry the same navigation.
Gap 4 — a reference that misspells a file we already wrote is repointed, not
        satisfied by creating a second, near-identical file.

All offline and LLM-free: both passes are pure parsing + rewriting.
"""

from types import SimpleNamespace

from app.agent.buildspec import BuildSpec
from app.agent.core import AgentCore
from app.agent.references import (
    find_similar_file,
    nav_signature,
    replace_nav_block,
    rewrite_reference,
    set_active_link,
)


class ScriptedLLM:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    def invoke(self, messages):
        out = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        return SimpleNamespace(content=out)


def _write_trace(*paths):
    return [
        {
            "tool": "write_file",
            "arguments": {"path": str(p)},
            "result": {"success": True, "result": "ok", "error": None},
        }
        for p in paths
    ]


def _page(nav: str, body: str = "<main>hi</main>") -> str:
    return f"<!DOCTYPE html><html><head><title>t</title></head><body>{nav}{body}</body></html>"


NAV_A = (
    "<nav>"
    '<a href="index.html" class="active">Our Story</a>'
    '<a href="details.html">Event Details</a>'
    '<a href="rsvp.html">RSVP</a>'
    "</nav>"
)
NAV_RENAMED = (
    "<nav>"
    '<a href="index.html">Home</a>'
    '<a href="details.html">Details</a>'
    "</nav>"
)


# ---------------------------------------------------------------------------
# nav_signature — what counts as "the same navbar"
# ---------------------------------------------------------------------------


def test_signature_ignores_the_active_marker_and_link_form():
    a = '<nav><a href="index.html" class="nav-link active">Home</a><a href="about.html">About</a></nav>'
    b = '<nav><a href="./index.html" class="nav-link">Home</a><a href="about.html" class="active">About</a></nav>'
    assert nav_signature(a) == nav_signature(b)


def test_signature_differs_on_a_renamed_or_missing_item():
    assert nav_signature(NAV_A) != nav_signature(NAV_RENAMED)
    three = nav_signature(NAV_A)
    dropped = nav_signature(NAV_A.replace('<a href="rsvp.html">RSVP</a>', ""))
    assert three != dropped


def test_signature_of_navless_markup_is_empty():
    assert nav_signature("") == ()


# ---------------------------------------------------------------------------
# set_active_link / replace_nav_block — moving one nav onto another page
# ---------------------------------------------------------------------------


def test_set_active_link_moves_the_marker_to_this_page():
    out = set_active_link(NAV_A, "rsvp.html")
    assert '<a href="rsvp.html" class="active">RSVP</a>' in out
    assert 'href="index.html" class="active"' not in out
    assert nav_signature(out) == nav_signature(NAV_A)  # nothing else changed


def test_set_active_link_preserves_other_classes():
    nav = '<nav><a class="nav-link active" href="a.html">A</a><a class="nav-link" href="b.html">B</a></nav>'
    out = set_active_link(nav, "b.html")
    assert 'class="nav-link"' in out
    assert 'class="nav-link active"' in out
    assert out.count("active") == 1


def test_replace_nav_block_swaps_only_the_nav():
    page = _page(NAV_RENAMED, "<main>keep me</main>")
    out = replace_nav_block(page, NAV_A)
    assert NAV_A in out
    assert ">Home</a>" not in out  # the replaced nav's labels are gone
    assert "keep me" in out
    assert out.startswith("<!DOCTYPE html>")


def test_replace_nav_block_handles_a_linking_header():
    page = _page('<header><a href="a.html">A</a></header>')
    out = replace_nav_block(page, NAV_A)
    assert NAV_A in out
    assert "<header>" not in out


# ---------------------------------------------------------------------------
# AgentCore._repair_nav_consistency
# ---------------------------------------------------------------------------


async def test_nav_repair_patches_the_page_that_disagrees(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    index = tmp_path / "index.html"
    details = tmp_path / "details.html"
    rsvp = tmp_path / "rsvp.html"
    index.write_text(_page(NAV_A), encoding="utf-8")
    details.write_text(_page(NAV_A), encoding="utf-8")
    rsvp.write_text(_page(NAV_RENAMED), encoding="utf-8")  # the outlier

    a = AgentCore(session_id="pytest_nav_fix")
    a._llm_direct = ScriptedLLM(["should not be called"])

    note, trace = await a._repair_nav_consistency(_write_trace(index, details, rsvp))

    fixed = rsvp.read_text(encoding="utf-8")
    assert nav_signature(fixed) == nav_signature(NAV_A)
    assert "RSVP" in fixed and "Home</a>" not in fixed
    # the active marker follows the page it now lives on
    assert '<a href="rsvp.html" class="active">RSVP</a>' in fixed
    assert index.read_text(encoding="utf-8") == _page(NAV_A)  # majority untouched
    assert "rsvp.html" in note
    assert a._llm_direct.calls == 0  # deterministic — no LLM
    assert len(trace) == 1


async def test_nav_repair_noop_when_pages_already_agree(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    index = tmp_path / "index.html"
    details = tmp_path / "details.html"
    index.write_text(_page(NAV_A), encoding="utf-8")
    # same nav, different item marked active — NOT a disagreement
    details.write_text(_page(set_active_link(NAV_A, "details.html")), encoding="utf-8")
    before = details.read_text(encoding="utf-8")

    a = AgentCore(session_id="pytest_nav_noop")
    note, trace = await a._repair_nav_consistency(_write_trace(index, details))

    assert note == ""
    assert trace == []
    assert details.read_text(encoding="utf-8") == before


async def test_nav_repair_prefers_the_nav_matching_the_user_spec(tmp_path, monkeypatch):
    """Two pages, two navs, no majority: the tie goes to the one whose labels
    the user actually asked for — not simply to whichever page was written
    first, which is the bug _sibling_context alone can't catch."""
    monkeypatch.chdir(tmp_path)
    wrong = tmp_path / "index.html"
    right = tmp_path / "rsvp.html"
    wrong.write_text(_page(NAV_RENAMED), encoding="utf-8")  # written first
    right.write_text(_page(NAV_A), encoding="utf-8")

    a = AgentCore(session_id="pytest_nav_spec")
    a._build_spec = BuildSpec(
        nav=(
            ("Our Story", "index.html"),
            ("Event Details", "details.html"),
            ("RSVP", "rsvp.html"),
        )
    )

    note, _ = await a._repair_nav_consistency(_write_trace(wrong, right))

    assert nav_signature(wrong.read_text(encoding="utf-8")) == nav_signature(NAV_A)
    assert "index.html" in note


async def test_nav_repair_skips_pages_without_a_nav(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    index = tmp_path / "index.html"
    bare = tmp_path / "bare.html"
    index.write_text(_page(NAV_A), encoding="utf-8")
    bare.write_text("<html><body><p>fragment</p></body></html>", encoding="utf-8")
    before = bare.read_text(encoding="utf-8")

    a = AgentCore(session_id="pytest_nav_bare")
    note, trace = await a._repair_nav_consistency(_write_trace(index, bare))

    assert bare.read_text(encoding="utf-8") == before  # no markup injected
    assert note == ""


async def test_nav_repair_ignores_non_html_writes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    css = tmp_path / "styles.css"
    css.write_text("body{color:red}", encoding="utf-8")
    a = AgentCore(session_id="pytest_nav_css")
    assert await a._repair_nav_consistency(_write_trace(css)) == ("", [])


# ---------------------------------------------------------------------------
# Near-miss asset references (Gap 4)
# ---------------------------------------------------------------------------


def test_find_similar_file_matches_a_plural_or_punctuation_variant(tmp_path):
    (tmp_path / "script.js").write_text("x", encoding="utf-8")
    assert find_similar_file(tmp_path / "scripts.js", tmp_path).name == "script.js"

    (tmp_path / "styles.css").write_text("x", encoding="utf-8")
    assert find_similar_file(tmp_path / "style.css", tmp_path).name == "styles.css"

    (tmp_path / "main-app.js").write_text("x", encoding="utf-8")
    assert find_similar_file(tmp_path / "mainapp.js", tmp_path).name == "main-app.js"


def test_find_similar_file_does_not_match_a_different_name_or_type(tmp_path):
    (tmp_path / "styles.css").write_text("x", encoding="utf-8")
    assert find_similar_file(tmp_path / "main.css", tmp_path) is None
    assert find_similar_file(tmp_path / "styles.js", tmp_path) is None
    assert find_similar_file(tmp_path / "styles.css", tmp_path) is None  # itself


def test_rewrite_reference_touches_only_the_reference():
    html = '<script src="scripts.js"></script><p>scripts.js is the file</p>'
    out, n = rewrite_reference(html, "scripts.js", "script.js")
    assert out == '<script src="script.js"></script><p>scripts.js is the file</p>'
    assert n == 1


def test_rewrite_reference_keeps_query_strings_and_css_url():
    out, n = rewrite_reference('<link href="style.css?v=2">', "style.css", "styles.css")
    assert 'href="styles.css?v=2"' in out
    out, n2 = rewrite_reference("@import url(style.css);", "style.css", "styles.css")
    assert "url(styles.css)" in out
    assert n == 1 and n2 == 1


async def test_repair_repoints_a_near_miss_instead_of_creating_a_duplicate(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "script.js").write_text("console.log(1)", encoding="utf-8")
    index = tmp_path / "index.html"
    index.write_text(
        '<html><body><script src="scripts.js"></script></body></html>',
        encoding="utf-8",
    )

    a = AgentCore(session_id="pytest_nearmiss")
    a._llm_direct = ScriptedLLM(["should not be called"])

    note, trace = await a._repair_dead_references(
        _write_trace(index, tmp_path / "script.js")
    )

    assert not (tmp_path / "scripts.js").exists()  # no duplicate asset
    assert 'src="script.js"' in index.read_text(encoding="utf-8")
    assert a._llm_direct.calls == 0  # deterministic
    assert "scripts.js -> script.js" in note
    assert trace and trace[0]["tool"] == "write_file"


async def test_repair_still_creates_a_genuinely_new_dependency(tmp_path, monkeypatch):
    """The near-miss check must not swallow a real missing file: `main.css` is
    not a variant spelling of `styles.css`, so it is still generated."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "styles.css").write_text("body{}", encoding="utf-8")
    index = tmp_path / "index.html"
    index.write_text(
        '<html><head><link rel="stylesheet" href="main.css"></head><body></body></html>',
        encoding="utf-8",
    )

    a = AgentCore(session_id="pytest_nearmiss_new")
    a._llm_direct = ScriptedLLM(["FILENAME: main.css\nbody{color:navy}"])

    note, _ = await a._repair_dead_references(_write_trace(index))

    assert (tmp_path / "main.css").is_file()
    assert "created 1" in note.lower()
