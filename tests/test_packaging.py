"""Tier 3 #9 — packaging: pyproject.toml, console entrypoint, version.

Offline checks only: the TOML is well-formed, the `coder` script points at the
Typer app, the version has a single source (app.__version__), and `--version`
works without Ollama (eager callback exits before test_connection).
"""
import tomllib
from pathlib import Path

_ROOT = Path(__file__).parent.parent


def _pyproject() -> dict:
    with open(_ROOT / "pyproject.toml", "rb") as f:
        return tomllib.load(f)


def test_pyproject_defines_coder_entrypoint():
    data = _pyproject()
    assert data["project"]["name"] == "coder"
    assert data["project"]["scripts"]["coder"] == "main:app"


def test_version_single_source():
    import app

    data = _pyproject()
    assert "version" in data["project"]["dynamic"]
    assert (
        data["tool"]["setuptools"]["dynamic"]["version"]["attr"] == "app.__version__"
    )
    assert isinstance(app.__version__, str)
    assert app.__version__.count(".") == 2


def test_pyproject_pins_tree_sitter():
    # CLAUDE.md gotcha: tree-sitter 0.25.x breaks tree-sitter-languages 1.10.2
    # silently (token-window fallback). The pin must survive packaging.
    deps = _pyproject()["project"]["dependencies"]
    assert any(d.replace(" ", "") == "tree-sitter==0.21.3" for d in deps)


def test_cli_version_flag_works_offline():
    from typer.testing import CliRunner

    import app
    import main

    result = CliRunner().invoke(main.app, ["--version"])

    assert result.exit_code == 0
    assert app.__version__ in result.output


# ---------------------------------------------------------------------------
# Step 13 / D1 — bundled resources ship as package data
# ---------------------------------------------------------------------------


def test_bundled_resources_live_in_app_package():
    """Prompts/skills/default-MCP-config are inside the app package so a wheel
    ships them; settings resolves to that location."""
    from config.settings import settings

    assert (settings.prompts_dir / "system.md").is_file()
    assert (settings.skills_dir / "example_skill" / "SKILL.md").is_file()
    assert settings.mcp_config.is_file()
    # ...and that location is under app/resources, not the repo root.
    assert settings.prompts_dir.parent.name == "resources"
    assert settings.prompts_dir.parent.parent.name == "app"


def test_pyproject_declares_resource_package_data():
    data = _pyproject()
    pkg_data = data["tool"]["setuptools"]["package-data"]
    globs = pkg_data.get("app", [])
    assert any("resources" in g for g in globs), globs
