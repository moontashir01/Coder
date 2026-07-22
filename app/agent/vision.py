"""Vision pipeline — turn a referenced image into text the coding model can use.

The vision model (`settings.vision_model`, e.g. `qwen2.5-vl:7b`) is a
**translator**, never a participant: `_describe_image` reads an image off disk,
asks the model for a structured UI description, and hands back plain text.
Everything downstream — `chat()`, `_multi_file_flow`, `_file_op_flow`,
`_build_messages` — sees ordinary text context and has no idea an image was
involved. That encapsulation is the whole point of this module, so keep it to
this one responsibility.

Failure is always non-fatal. A missing model, an unreadable/oversized file, or
an empty answer returns None, and the caller proceeds as if the image had never
been referenced (text-only). The image ref is an enhancement, not a dependency.
"""

import base64
import io
import logging
from pathlib import Path
from typing import Callable

from langchain_core.messages import HumanMessage
from langchain_ollama import ChatOllama

from config.settings import settings

logger = logging.getLogger(__name__)

# Ollama wants raw base64; langchain_ollama's content-block converter accepts it
# as a data: URI and splits the payload off the comma itself. The subtype only
# has to be plausible — the server sniffs the actual format.
_MIME_SUBTYPE = {
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".webp": "webp",
    ".gif": "gif",
    ".bmp": "bmp",
}

# Below this the model answered with a refusal, a stray token, or nothing
# useful — treat it as no description rather than poisoning the build prompt.
_MIN_DESCRIPTION_CHARS = 40

_FALLBACK_PROMPT = (
    "You are a UI analyst. Describe this screenshot in precise detail that a "
    "frontend developer can use to recreate it: layout, navigation, color "
    "palette (as hex), typography, components, content, and overall style."
)


def is_image(path: str | Path) -> bool:
    """True if the path names an image the vision model should describe.

    Extension-only on purpose: it is decided before the file is opened (and for
    refs that don't exist yet). `.svg` is deliberately NOT in the default list —
    it is text, so the existing text-ref path reads it as source.
    """
    suffix = Path(str(path)).suffix.lower()
    return suffix in {e.lower() for e in settings.image_extensions}


def _load_prompt() -> str:
    """The extraction prompt, from app/resources/prompts (see settings.prompts_dir)."""
    try:
        return (settings.prompts_dir / "vision_describe.md").read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("vision prompt not readable (%s) — using the built-in", e)
        return _FALLBACK_PROMPT


def _get_vision_llm() -> ChatOllama:
    """A ChatOllama bound to the VISION model.

    Deliberately its own instance, created per call: the agent's `self.llm` is
    the coding model and must keep its own model name and num_ctx. Ollama swaps
    the loaded model for us — no preloading or keep-alive juggling here.
    """
    return ChatOllama(
        model=settings.vision_model,
        base_url=settings.ollama_base_url,
        temperature=0.1,
        # The description is short, so a small window is plenty — and it keeps
        # the KV cache small on a machine that has to fit a 7B VL model.
        num_ctx=settings.vision_num_ctx,
        client_kwargs={"timeout": settings.llm_request_timeout_seconds},
    )


def _response_text(resp: object) -> str:
    """Flatten a ChatOllama response to text (content may be blocks, not a str)."""
    content = getattr(resp, "content", "") or ""
    if isinstance(content, str):
        return content.strip()
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "\n".join(parts).strip()


def _prepare_image(raw: bytes, suffix: str) -> tuple[bytes, str]:
    """Downscale `raw` so its longest edge fits `settings.max_image_dimension`.

    Returns `(bytes_to_send, mime_subtype)`. Qwen2.5-VL tokenizes at ~native
    resolution and Ollama SILENTLY truncates the prompt to `vision_num_ctx`
    rather than erroring, so a high-res screenshot loses its lower half with no
    warning and the model describes only what survived — a wrong build the
    40-char guard can't catch. Capping the long edge bounds the token count (and
    speeds the call / shrinks the KV cache on the 8 GB-VRAM machine this targets).

    Best-effort, matching the module's rule that vision is an enhancement, never
    a dependency: if downscaling is disabled, the image already fits, or anything
    goes wrong (Pillow missing, undecodable bytes), the original bytes are sent
    untouched. A resized image is re-encoded to PNG — lossless, so the sharp text
    in a UI screenshot survives, and Ollama sniffs the real format regardless.
    """
    limit = settings.max_image_dimension
    fallback_subtype = _MIME_SUBTYPE.get(suffix, "png")
    if limit <= 0:
        return raw, fallback_subtype
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as img:
            longest = max(img.size)
            if longest <= limit:
                return raw, fallback_subtype
            scale = limit / longest
            new_size = (
                max(1, round(img.width * scale)),
                max(1, round(img.height * scale)),
            )
            resized = img.convert("RGB").resize(new_size, Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, format="PNG")
            logger.debug("downscaled image %s -> %s", img.size, new_size)
            return buf.getvalue(), "png"
    except Exception as e:
        logger.warning("could not downscale image (%s) — sending it as-is", e)
        return raw, fallback_subtype


def _describe_image(
    path: str | Path,
    on_status: Callable[[str], None] | None = None,
) -> str | None:
    """Describe the image at `path` as structured text, or None on any problem.

    `on_status` receives short user-facing progress/warning lines (the REPL
    shows them); it is optional so library/test callers can ignore them.
    """
    if not settings.vision_enabled:
        return None

    p = Path(path)

    try:
        size = p.stat().st_size
    except OSError as e:
        logger.warning("image ref %s is unreadable: %s", p, e)
        _say(on_status, f"[vision] Skipped {p.name} — cannot read the file")
        return None

    if size == 0:
        _say(on_status, f"[vision] Skipped {p.name} — the file is empty")
        return None
    if size > settings.max_image_bytes:
        logger.warning("image ref %s is %d bytes — over the cap", p, size)
        _say(
            on_status,
            f"[vision] Skipped {p.name} — {size // 1_000_000} MB exceeds the "
            f"{settings.max_image_bytes // 1_000_000} MB limit",
        )
        return None

    try:
        raw = p.read_bytes()
    except Exception as e:
        logger.warning("could not read image %s: %s", p, e)
        _say(on_status, f"[vision] Skipped {p.name} — cannot read the file")
        return None

    _say(on_status, f"[vision] Analyzing {p.name} ...")
    data, subtype = _prepare_image(raw, p.suffix.lower())
    b64 = base64.b64encode(data).decode("ascii")
    message = HumanMessage(
        content=[
            {"type": "text", "text": _load_prompt()},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/{subtype};base64,{b64}"},
            },
        ]
    )

    try:
        resp = _get_vision_llm().invoke([message])
    except Exception as e:
        logger.warning("vision call failed for %s: %s", p, e)
        _say(on_status, _failure_hint(e))
        return None

    description = _response_text(resp)
    if len(description) < _MIN_DESCRIPTION_CHARS:
        logger.warning("vision model returned nothing usable for %s", p)
        _say(on_status, f"[vision] {p.name} produced no usable description — skipping")
        return None

    _say(on_status, "[vision] Done — extracted layout description")
    return description


def _failure_hint(error: Exception) -> str:
    """User-facing line for a failed vision call — name the fix when we know it."""
    text = str(error).lower()
    if "not found" in text or "pull" in text or "no such model" in text:
        return (
            f"Vision model '{settings.vision_model}' not available. "
            f"Pull it with: ollama pull {settings.vision_model}"
        )
    return f"[vision] Could not analyze the image ({error}) — continuing without it"


def _say(on_status: Callable[[str], None] | None, message: str) -> None:
    """Emit a status line; a broken hook must never break the pipeline."""
    if on_status is None:
        return
    try:
        on_status(message)
    except Exception:
        logger.debug("status hook raised", exc_info=True)
