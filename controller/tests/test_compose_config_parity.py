"""docker-compose defaults must not drift from config.py defaults.

`docker-compose.yml` hardcoded `${OPENAI_MODEL:-gpt-4.1-mini}` etc. while
`config.py` had moved on to `gpt-5-mini`. Nothing tied the two together, so
Docker deployments — the primary deploy path — silently ran different, older
models than pip installs and than anything CI tested. Worse, it is a delayed
failure: nobody notices until the provider retires the old model id and Docker
users start getting 404s that local runs never reproduce.

This is the same one-thing-two-sources class the repo already guards for
version strings (check_version_parity.py) and Playwright pins
(check_playwright_pins.py).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "docker-compose.yml"

# The controller image COPYs only `app/` and `tests/`, so docker-compose.yml is
# genuinely absent when the suite runs inside the container (the `controller-tests`
# CI job). This is a repo-layout check, so skipping there is correct — and it
# still runs in both `host-tests` jobs, which execute from a real checkout.
pytestmark = pytest.mark.skipif(
    not COMPOSE.is_file(),
    reason="docker-compose.yml is not shipped inside the controller image",
)

# compose env key -> Settings attribute holding the same default.
MODEL_KEYS = {
    "OPENAI_MODEL": "openai_model",
    "CLAUDE_MODEL": "claude_model",
    "GEMINI_MODEL": "gemini_model",
    "XAI_MODEL": "xai_model",
    "DEEPSEEK_MODEL": "deepseek_model",
    "MINIMAX_MODEL": "minimax_model",
}


def _compose_default(key: str) -> str | None:
    """Extract `X` from a `KEY: ${KEY:-X}` line, or None if there is no default."""
    text = COMPOSE.read_text(encoding="utf-8")
    match = re.search(rf"^\s*{re.escape(key)}:\s*\$\{{{re.escape(key)}:-(.*?)\}}\s*$", text, re.MULTILINE)
    if match is None:
        return None
    return match.group(1).strip()


def test_at_least_one_model_key_is_present_in_compose() -> None:
    """Guard the guard.

    A renamed key or a moved compose file would make every case below pass
    vacuously, so assert the parser actually finds something to compare.
    """
    found = {key for key in MODEL_KEYS if _compose_default(key) is not None}
    assert found, f"no {'/'.join(sorted(MODEL_KEYS))} defaults parsed from {COMPOSE}"


@pytest.mark.parametrize("key,attr", sorted(MODEL_KEYS.items()))
def test_compose_model_default_matches_settings(key: str, attr: str) -> None:
    compose_value = _compose_default(key)
    if compose_value in (None, ""):
        # No hardcoded default in compose means config.py is the single source,
        # which is the preferred shape — nothing to drift.
        return

    settings_value = getattr(Settings(_env_file=None), attr)
    assert compose_value == settings_value, (
        f"docker-compose.yml pins {key}={compose_value!r} but config.py defaults "
        f"{attr}={settings_value!r}. Docker deployments would silently run a "
        f"different model than pip installs and than CI tests."
    )
