"""Every Gemini CLI call is given GEMINI_CLI_SYSTEM_SETTINGS_PATH, naming the settings file the controller ships with
its code, which declares no tool: no core tool (tools.core is an allowlist), no skill, and none of the CLI's three
agents (it has no allowlist for agents). The controller's own path wins over one passed through.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import Settings
from app.providers import gemini_adapter
from app.providers.gemini_adapter import GeminiAdapter

SHIPPED = Path(gemini_adapter.__file__).resolve().with_name("gemini_system_settings.json")
NO_TOOL = {
    "tools": {"core": []},
    "skills": {"enabled": False},
    "agents": {
        "overrides": {
            "codebase_investigator": {"enabled": False},
            "cli_help": {"enabled": False},
            "generalist": {"enabled": False},
        }
    },
}


def _environment(**settings: str) -> dict[str, str]:
    return GeminiAdapter(Settings(_env_file=None, GEMINI_AUTH_MODE="cli", **settings)).cli_environment()


def test_the_shipped_settings_declare_no_tool() -> None:
    assert json.loads(SHIPPED.read_text(encoding="utf-8")) == NO_TOOL


def test_every_gemini_cli_call_is_given_the_shipped_settings(tmp_path: Path) -> None:
    env = _environment(CLI_HOME=str(tmp_path / "cli-home"))

    assert env.get("GEMINI_CLI_SYSTEM_SETTINGS_PATH") == str(SHIPPED)


def test_the_controllers_own_path_wins_over_one_passed_through(tmp_path: Path) -> None:
    with patch.dict(os.environ, {"GEMINI_CLI_SYSTEM_SETTINGS_PATH": str(tmp_path / "elsewhere.json")}):
        env = _environment(
            CLI_HOME=str(tmp_path / "cli-home"), CLI_PROVIDER_ENV_PASSTHROUGH="GEMINI_CLI_SYSTEM_SETTINGS_PATH"
        )

    assert env.get("GEMINI_CLI_SYSTEM_SETTINGS_PATH") == str(SHIPPED)


def test_the_file_is_in_the_package_data() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    if not pyproject.is_file():
        pytest.skip("no pyproject.toml beside the tests")
    globs = tomllib.loads(pyproject.read_text(encoding="utf-8"))["tool"]["setuptools"]["package-data"]["app"]
    app = Path(gemini_adapter.__file__).resolve().parents[1]
    shipped = {path.relative_to(app).as_posix() for pattern in globs for path in app.glob(pattern)}
    assert "providers/gemini_system_settings.json" in shipped
