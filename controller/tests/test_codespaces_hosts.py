"""The Codespaces overlay must let the controller answer on its forwarded *.app.github.dev host."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.middleware.hosts import host_is_allowed

COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.codespaces.yml"
DEFAULT = re.compile(r"^\s*CONTROLLER_ALLOWED_HOSTS:\s*\$\{CONTROLLER_ALLOWED_HOSTS:-([^}]*)\}", re.M)


@pytest.mark.skipif(not COMPOSE.is_file(), reason="repo checkout not available inside the controller image")
def test_the_overlay_default_accepts_the_forwarded_host_and_still_refuses_others() -> None:
    match = DEFAULT.search(COMPOSE.read_text(encoding="utf-8"))
    assert match, "docker-compose.codespaces.yml must set a CONTROLLER_ALLOWED_HOSTS default"
    patterns = match.group(1).split(",")

    assert host_is_allowed("my-codespace-8000.app.github.dev", patterns)
    assert host_is_allowed("localhost:8000", patterns)
    assert not host_is_allowed("my-codespace-8000.app.github.dev.evil.test", patterns)
    assert not host_is_allowed("evil.test", patterns)
