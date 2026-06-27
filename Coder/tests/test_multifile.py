"""Tests for multi-file planning, the extension guard, and multi-file orchestration.

All offline: the LLM is a scripted fake, file writes go to tmp_path.
"""

from types import SimpleNamespace

import pytest

from app.agent.core import _extension_guard


class ScriptedLLM:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    def invoke(self, messages):
        out = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        return SimpleNamespace(content=out)


def test_extension_guard_css():
    g = _extension_guard("styles.css")
    assert "CSS" in g
    assert "JavaScript" in g or "JS" in g  # tells the model NOT to emit JS


def test_extension_guard_js():
    g = _extension_guard("script.js")
    assert "JavaScript" in g


def test_extension_guard_unknown_is_empty():
    assert _extension_guard("notes.txt") == ""
