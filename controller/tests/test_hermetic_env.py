"""The suite's results must not depend on the developer's environment.

`Settings` reads exported variables, then a `.env` in the working directory.
`make test` runs the suite in a container whose process environment is fed from
`.env`, and a host run from `controller/` reads a `.env` there. Either way a
developer's tokens, allowed hosts or `REQUIRE_OPERATOR_ID` reached tests that
expected defaults: `test_session_ownership.py` went from passing to 7-8
failures (`400 != 404`) with `.env.example` exported.

`tests/__init__.py` clears every `Settings` alias from the environment and
stops `Settings` reading `.env`. These tests import the package in a fresh
interpreter that has both a hostile environment and a hostile `.env`, because
this process has already been cleaned.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import tests

CONTROLLER_DIR = Path(tests.__file__).resolve().parent.parent

# Reads Settings the way a test would, from a process that imported `tests`.
PROBE = """
import json
import tests
from app.config import Settings

s = Settings()
print(json.dumps({
    "api_bearer_token": s.api_bearer_token,
    "api_bearer_tokens": s.api_bearer_tokens,
    "require_operator_id": s.require_operator_id,
    "controller_allowed_hosts": s.controller_allowed_hosts,
    "api_bind_scope": s.api_bind_scope,
    "app_env": s.app_env,
    "artifact_root": s.artifact_root,
}))
"""

HOSTILE_ENV = {
    "API_BEARER_TOKENS": "alice:" + "a" * 40 + ",bob:" + "b" * 40,
    "CONTROLLER_ALLOWED_HOSTS": "example.com",
    "API_BIND_SCOPE": "exposed",
    # The second name of `app_env`'s AliasChoices.
    "ENVIRONMENT": "production",
    "ARTIFACT_ROOT": "/data/artifacts",
}

HOSTILE_DOTENV = """\
API_BEARER_TOKEN=dotenv-secret
REQUIRE_OPERATOR_ID=true
APP_ENV=staging
"""


def _settings_seen_by_a_fresh_process(cwd: Path, env_overrides: dict[str, str]) -> dict[str, object]:
    env = {**os.environ, **env_overrides, "PYTHONPATH": str(CONTROLLER_DIR)}
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_exported_variables_do_not_change_settings(tmp_path: Path) -> None:
    seen = _settings_seen_by_a_fresh_process(tmp_path, HOSTILE_ENV)

    assert seen["api_bearer_tokens"] == ""
    assert seen["api_bind_scope"] == "loopback"
    assert seen["app_env"] == "development"
    assert "testserver" in str(seen["controller_allowed_hosts"]).split(",")
    assert "auto-browser-tests-" in str(seen["artifact_root"])


def test_a_dotenv_in_the_working_directory_does_not_change_settings(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(HOSTILE_DOTENV, encoding="utf-8")

    seen = _settings_seen_by_a_fresh_process(tmp_path, {})

    assert seen["api_bearer_token"] is None
    assert seen["require_operator_id"] is False
    assert seen["app_env"] == "development"


def test_results_are_the_same_with_and_without_a_hostile_environment(tmp_path: Path) -> None:
    clean_dir = tmp_path / "clean"
    hostile_dir = tmp_path / "hostile"
    clean_dir.mkdir()
    hostile_dir.mkdir()
    (hostile_dir / ".env").write_text(HOSTILE_DOTENV, encoding="utf-8")

    clean = _settings_seen_by_a_fresh_process(clean_dir, {})
    hostile = _settings_seen_by_a_fresh_process(hostile_dir, HOSTILE_ENV)

    # Each run makes its own throwaway data root; everything else must match.
    clean.pop("artifact_root")
    hostile.pop("artifact_root")
    assert hostile == clean
