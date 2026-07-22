"""Tests for the vision pipeline: an @-referenced image becomes text context.

All offline: the vision model is a fake ChatOllama, the coding model a scripted
fake, and the "screenshot" is a few bytes in tmp_path (nothing decodes it).
"""

import base64
import io
from types import SimpleNamespace

import pytest

from app.agent import vision as vision_mod
from app.agent.core import (
    AgentCore,
    _split_image_refs,
    _strip_at_refs,
    _wants_image_build,
)
from app.agent.vision import _describe_image, _failure_hint, is_image
from config.settings import settings

_DESCRIPTION = (
    "LAYOUT: header, hero, three-column card grid, footer.\n"
    "NAVIGATION: Home, About, Contact.\n"
    "COLOR PALETTE: dark navy background (#1a1a2e), coral accent (#ff6b6b).\n"
    "TYPOGRAPHY: sans-serif, large headings.\n"
)

_PNG_BYTES = b"\x89PNG\r\n\x1a\n-not-a-real-png-but-nothing-decodes-it"


class ScriptedLLM:
    """Coding-model stand-in that also records the prompts it was given."""

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0
        self.messages: list = []

    def invoke(self, messages):
        self.messages.append(messages)
        out = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        return SimpleNamespace(content=out)


class FakeVisionLLM:
    """Stands in for ChatOllama in vision.py — records how it was built/called."""

    instances: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls: list = []
        FakeVisionLLM.instances.append(self)

    def invoke(self, messages):
        self.calls.append(messages)
        return SimpleNamespace(content=_DESCRIPTION)


@pytest.fixture
def fake_vision(monkeypatch):
    """Replace the vision model with a fake; yields the class (see .instances)."""
    FakeVisionLLM.instances = []
    monkeypatch.setattr(vision_mod, "ChatOllama", FakeVisionLLM)
    return FakeVisionLLM


@pytest.fixture
def screenshot(tmp_path, monkeypatch):
    """A fake screenshot in an empty cwd."""
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "shot.png"
    p.write_bytes(_PNG_BYTES)
    return p


# ---------------------------------------------------------------------------
# 1. Image vs text detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["shot.png", "a/b/MOCK.JPG", "design.jpeg", "x.webp", "y.gif", "z.bmp"]
)
def test_is_image_true(path):
    assert is_image(path) is True


@pytest.mark.parametrize(
    # .svg is text (the model reads it as source), so it must NOT be an image
    "path",
    ["app.py", "index.html", "styles.css", "logo.svg", "notes", "data.json"],
)
def test_is_image_false(path):
    assert is_image(path) is False


def test_split_image_refs():
    text_refs, image_refs = _split_image_refs(["a.py", "shot.png", "index.html"])
    assert text_refs == ["a.py", "index.html"]
    assert image_refs == ["shot.png"]


def test_read_refs_routes_images_to_vision_and_text_to_disk(
    tmp_path, monkeypatch, fake_vision, screenshot
):
    (tmp_path / "a.py").write_text("print('hello a')\n", encoding="utf-8")
    a = AgentCore(session_id="pytest_vision_readrefs")

    ctx = a._read_refs(["a.py", "shot.png"])

    assert "hello a" in ctx  # text ref read from disk, unchanged
    assert "COLOR PALETTE" in ctx  # image ref replaced by its description
    assert "### shot.png (image description)" in ctx
    assert len(fake_vision.instances[0].calls) == 1


def test_image_ref_is_never_the_write_target(screenshot):
    """An @image says what to build FROM, not which file to write."""
    a = AgentCore(session_id="pytest_vision_target")
    assert a._resolve_ref(["shot.png"]) is None
    assert a._resolve_ref(["shot.png", "index.html"]) == "index.html"
    # ...and the filename is dropped from the text, so _extract_filename can't
    # pick it up either.
    assert _strip_at_refs("build a site like this @shot.png") == (
        "build a site like this "
    )
    assert _strip_at_refs("edit @app.py please") == "edit app.py please"


# ---------------------------------------------------------------------------
# 2. _describe_image against a mocked Ollama
# ---------------------------------------------------------------------------


def test_describe_image_sends_prompt_and_base64(fake_vision, screenshot):
    out = _describe_image(screenshot)

    assert out == _DESCRIPTION.strip()

    llm = fake_vision.instances[0]
    # Its own instance, on the VISION model — not the coding model's settings.
    assert llm.kwargs["model"] == settings.vision_model
    assert llm.kwargs["num_ctx"] == settings.vision_num_ctx

    (message,) = llm.calls[0]
    text_block, image_block = message.content
    assert text_block["type"] == "text"
    assert "LAYOUT:" in text_block["text"]  # the prompt resource, not a literal
    assert image_block["type"] == "image_url"
    url = image_block["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert url.split(",", 1)[1] == base64.b64encode(_PNG_BYTES).decode("ascii")


def test_large_image_is_downscaled_before_send(fake_vision, tmp_path, monkeypatch):
    """A high-res screenshot is capped to max_image_dimension, ratio preserved.

    Without this the VL model tokenizes it at native resolution and Ollama
    silently truncates the prompt to vision_num_ctx — the bug this guards.
    """
    from PIL import Image

    monkeypatch.chdir(tmp_path)
    big = tmp_path / "big.png"
    Image.new("RGB", (4000, 2000), (200, 30, 30)).save(big)

    assert _describe_image(big) == _DESCRIPTION.strip()

    (message,) = fake_vision.instances[0].calls[0]
    _text, image_block = message.content
    b64 = image_block["image_url"]["url"].split(",", 1)[1]
    sent = Image.open(io.BytesIO(base64.b64decode(b64)))
    limit = settings.max_image_dimension
    assert max(sent.size) == limit
    assert sent.size == (limit, limit // 2)  # 2:1 aspect ratio preserved


def test_small_image_is_sent_untouched(fake_vision, tmp_path, monkeypatch):
    """An already-small image is forwarded byte-for-byte — no needless re-encode."""
    from PIL import Image

    monkeypatch.chdir(tmp_path)
    small = tmp_path / "small.png"
    Image.new("RGB", (100, 80), (10, 20, 30)).save(small)
    raw = small.read_bytes()

    _describe_image(small)

    (message,) = fake_vision.instances[0].calls[0]
    _text, image_block = message.content
    b64 = image_block["image_url"]["url"].split(",", 1)[1]
    assert base64.b64decode(b64) == raw


def test_undecodable_bytes_fall_back_to_the_original(fake_vision, screenshot):
    """_PNG_BYTES is not a real image — downscaling must degrade, not crash."""
    _describe_image(screenshot)

    (message,) = fake_vision.instances[0].calls[0]
    _text, image_block = message.content
    b64 = image_block["image_url"]["url"].split(",", 1)[1]
    assert base64.b64decode(b64) == _PNG_BYTES  # sent as-is, no exception


def test_downscaling_disabled_sends_the_original(fake_vision, tmp_path, monkeypatch):
    from PIL import Image

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "max_image_dimension", 0)
    big = tmp_path / "big.png"
    Image.new("RGB", (4000, 2000), (200, 30, 30)).save(big)
    raw = big.read_bytes()

    _describe_image(big)

    (message,) = fake_vision.instances[0].calls[0]
    _text, image_block = message.content
    b64 = image_block["image_url"]["url"].split(",", 1)[1]
    assert base64.b64decode(b64) == raw


def test_describe_image_reports_progress(fake_vision, screenshot):
    notes: list[str] = []
    _describe_image(screenshot, on_status=notes.append)
    assert any("Analyzing shot.png" in n for n in notes)
    assert any("Done" in n for n in notes)


def test_describe_image_is_memoized_per_file(fake_vision, screenshot):
    a = AgentCore(session_id="pytest_vision_memo")
    first = a._read_refs(["shot.png"])
    second = a._read_refs(["shot.png"])
    assert first == second
    # One vision call, not two — each one swaps the model Ollama has loaded.
    assert len(fake_vision.instances) == 1
    assert len(fake_vision.instances[0].calls) == 1


# ---------------------------------------------------------------------------
# 3. Integration: image ref -> text context -> generated file
# ---------------------------------------------------------------------------


async def test_chat_builds_from_screenshot(
    tmp_path, monkeypatch, fake_vision, screenshot
):
    a = AgentCore(session_id="pytest_vision_chat")
    monkeypatch.setattr(a.planner, "classify", lambda msg: "code_generation")
    a._llm_direct = ScriptedLLM(
        ["FILENAME: index.html\n<html><body><h1>Site</h1></body></html>"]
    )

    answer, trace = await a.chat("Build a website like this @shot.png")

    # A real file was generated — not a crash, and not a write onto the .png
    assert (tmp_path / "index.html").is_file()
    assert not (tmp_path / "shot.png").read_bytes() != _PNG_BYTES
    assert "index.html" in answer

    # ...and the coding model saw the vision description as plain context.
    prompt = "\n".join(str(m.content) for m in a._llm_direct.messages[0])
    assert "COLOR PALETTE" in prompt
    assert "Reference image: shot.png" in prompt


@pytest.mark.parametrize(
    "message",
    [
        # No target noun for _wants_file_op to match — the noun IS the image,
        # and the ref is stripped out of the text. These used to dead-end on
        # _direct_answer, which printed the page into the terminal and wrote
        # nothing (the bug this parametrization exists to catch).
        "build this @shot.png",
        "make this @shot.png",
        "create this @shot.png",
        "recreate this @shot.png",
        "build me this @shot.png",
        "make this design @shot.png",
        "replicate @shot.png",
        "clone this @shot.png",
        "turn this into a webpage @shot.png",
        "code this up @shot.png",
    ],
)
async def test_chat_writes_a_file_for_a_bare_build_request(
    tmp_path, monkeypatch, fake_vision, screenshot, message
):
    a = AgentCore(session_id="pytest_vision_bare")
    monkeypatch.setattr(a.planner, "classify", lambda msg: "code_generation")
    a._llm_direct = ScriptedLLM(["FILENAME: index.html\n<html><body>x</body></html>"])

    answer, trace = await a.chat(message)

    assert (tmp_path / "index.html").is_file(), f"{message!r} wrote no file"
    assert [t for t in trace if t["tool"] == "write_file"]
    prompt = "\n".join(str(m.content) for m in a._llm_direct.messages[0])
    assert "COLOR PALETTE" in prompt


@pytest.mark.parametrize(
    "message",
    [
        "what does this show",
        "what is the color palette here",
        "describe this layout",
        "explain the grid used here",
        "is this a dark theme",
        "does this copy their homepage",  # a build verb, but still a question
    ],
)
def test_wants_image_build_false_for_questions(message):
    assert _wants_image_build(message) is False


async def test_chat_question_about_an_image_still_answers(
    tmp_path, monkeypatch, fake_vision, screenshot
):
    """No build verb → a question about the image, answered, nothing written."""
    a = AgentCore(session_id="pytest_vision_question")
    monkeypatch.setattr(a.planner, "classify", lambda msg: "simple_qa")
    a._llm_direct = ScriptedLLM(["It is a dark landing page with three cards."])

    answer, trace = await a.chat("what does @shot.png show?")

    assert "three cards" in answer
    assert trace == []
    assert not list(tmp_path.glob("*.html"))
    prompt = "\n".join(str(m.content) for m in a._llm_direct.messages[0])
    assert "COLOR PALETTE" in prompt  # the description still reaches the model


async def test_chat_describes_the_screenshot_once_per_subtask_run(
    tmp_path, monkeypatch, fake_vision, screenshot
):
    """Every sub-task of a compound build sees the screenshot, one vision call."""
    a = AgentCore(session_id="pytest_vision_subtasks")
    monkeypatch.setattr(a.planner, "classify", lambda msg: "code_generation")
    a._llm_direct = ScriptedLLM(
        [
            "FILENAME: index.html\n<html><body>home</body></html>",
            "FILENAME: about.html\n<html><body>about</body></html>",
        ]
    )

    await a.chat("create index.html like this @shot.png, and create about.html too")

    assert (tmp_path / "index.html").is_file()
    assert (tmp_path / "about.html").is_file()
    assert len(fake_vision.instances) == 1
    assert len(fake_vision.instances[0].calls) == 1
    for messages in a._llm_direct.messages:
        assert "COLOR PALETTE" in "\n".join(str(m.content) for m in messages)


# ---------------------------------------------------------------------------
# 4. Graceful degradation — every failure is non-fatal
# ---------------------------------------------------------------------------


class BoomVisionLLM:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def invoke(self, messages):
        raise RuntimeError("model 'qwen2.5-vl:7b' not found, try pulling it first")


class EmptyVisionLLM:
    def __init__(self, **kwargs):
        pass

    def invoke(self, messages):
        return SimpleNamespace(content="   ")


@pytest.mark.parametrize("fake", [BoomVisionLLM, EmptyVisionLLM])
def test_describe_image_failures_return_none(monkeypatch, screenshot, fake):
    monkeypatch.setattr(vision_mod, "ChatOllama", fake)
    assert _describe_image(screenshot) is None


def test_describe_image_missing_file_returns_none(tmp_path, fake_vision):
    assert _describe_image(tmp_path / "nope.png") is None


def test_image_ref_outside_sandbox_is_refused(tmp_path, monkeypatch, fake_vision):
    """The vision read path honors the same path jail as the file tools.

    A ref that escapes sandbox_root must be skipped (non-fatal) and never reach
    the vision model — otherwise `@../secret.png` would be base64'd off-disk.
    """
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "secret.png"
    outside.write_bytes(_PNG_BYTES)
    monkeypatch.setattr(settings, "sandbox_root", project)
    monkeypatch.setattr(settings, "allow_outside_root", False)

    a = AgentCore(session_id="pytest_vision_jail")
    a._project_path = project

    assert a._describe_image_ref("../secret.png") is None
    assert not fake_vision.instances  # never constructed the model

    # --allow-outside-root lifts the jail — then it is read normally.
    monkeypatch.setattr(settings, "allow_outside_root", True)
    assert a._describe_image_ref("../secret.png") is not None


def test_describe_image_oversized_is_skipped(monkeypatch, fake_vision, screenshot):
    monkeypatch.setattr(settings, "max_image_bytes", 4)
    notes: list[str] = []
    assert _describe_image(screenshot, on_status=notes.append) is None
    assert not fake_vision.instances  # never even built the model
    assert any("exceeds" in n for n in notes)


def test_failure_hint_names_the_pull_command():
    hint = _failure_hint(RuntimeError("model not found"))
    assert f"ollama pull {settings.vision_model}" in hint
    assert "not available" in hint


async def test_chat_falls_back_to_text_only_when_vision_fails(
    tmp_path, monkeypatch, screenshot
):
    monkeypatch.setattr(vision_mod, "ChatOllama", BoomVisionLLM)
    a = AgentCore(session_id="pytest_vision_degraded")
    monkeypatch.setattr(a.planner, "classify", lambda msg: "code_generation")
    a._llm_direct = ScriptedLLM(["FILENAME: index.html\n<html>fallback</html>"])
    notes: list[str] = []
    a.status_hook = notes.append

    answer, trace = await a.chat("Build a website like this @shot.png")

    # Text-only behavior, exactly as if the image had never been referenced.
    assert (tmp_path / "index.html").is_file()
    assert "index.html" in answer
    prompt = "\n".join(str(m.content) for m in a._llm_direct.messages[0])
    assert "Reference image" not in prompt
    assert any("ollama pull" in n for n in notes)


# ---------------------------------------------------------------------------
# 5. Kill switch
# ---------------------------------------------------------------------------


def test_vision_disabled_skips_the_image_entirely(monkeypatch, fake_vision, screenshot):
    monkeypatch.setattr(settings, "vision_enabled", False)
    a = AgentCore(session_id="pytest_vision_off")

    assert a._read_refs(["shot.png"]) == ""
    assert a._image_context(["shot.png"]) == ""
    assert not fake_vision.instances  # no vision model was ever constructed


async def test_vision_disabled_still_builds_from_the_text(
    tmp_path, monkeypatch, fake_vision, screenshot
):
    monkeypatch.setattr(settings, "vision_enabled", False)
    a = AgentCore(session_id="pytest_vision_off_chat")
    monkeypatch.setattr(a.planner, "classify", lambda msg: "code_generation")
    a._llm_direct = ScriptedLLM(["FILENAME: index.html\n<html>text only</html>"])

    await a.chat("Build a website like this @shot.png")

    assert (tmp_path / "index.html").is_file()
    assert not fake_vision.instances


# ---------------------------------------------------------------------------
# 6. Regression: text @refs are untouched
# ---------------------------------------------------------------------------


def test_text_ref_injection_unchanged(tmp_path, monkeypatch, fake_vision):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.py").write_text("print('hello a')\n", encoding="utf-8")
    a = AgentCore(session_id="pytest_vision_textref")

    ctx = a._read_refs(["a.py", "missing.py"])

    assert "### a.py" in ctx
    assert "hello a" in ctx
    assert "missing.py" not in ctx
    assert not fake_vision.instances  # no vision model for a text ref


async def test_text_ref_still_pins_the_edit_target(tmp_path, monkeypatch, fake_vision):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "real.py"
    target.write_text("v = 1\n", encoding="utf-8")
    a = AgentCore(session_id="pytest_vision_pin")
    a._llm_edit = ScriptedLLM(["no blocks here"])
    a._llm_direct = ScriptedLLM(["FILENAME: real.py\nv = 2\n"])
    monkeypatch.setattr(a.planner, "classify", lambda msg: "file_edit")

    await a.chat("bump the version in @real.py")

    assert target.read_text(encoding="utf-8") == "v = 2"
    assert not fake_vision.instances
