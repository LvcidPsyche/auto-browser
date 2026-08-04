"""The raw CDP allowlist is a security boundary, not a convenience list.

`/sessions/{id}/cdp/raw` is gated solely by `_ALLOWED_CDP_COMMANDS`, and that
list contained `Runtime.evaluate` and `Network.getCookies` while being described
as "safe" (reported privately 2026-06-17).

Sessions reuse stored auth profiles, so arbitrary JavaScript in the page context
is cookie theft and acting as the logged-in user on every site a profile is
authenticated to — plus `fetch()`-based SSRF to internal and cloud-metadata
addresses, which bypasses the navigation allowlist entirely.

These tests pin the boundary so a future "just add one more command" cannot
quietly reopen it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.cdp.passthrough import _ALLOWED_CDP_COMMANDS

# Commands that must never be reachable through the raw passthrough, with the
# reason each is disqualifying.
FORBIDDEN = {
    "Runtime.evaluate": "arbitrary JavaScript in an authenticated page context",
    "Runtime.callFunctionOn": "arbitrary JavaScript in an authenticated page context",
    "Runtime.compileScript": "arbitrary JavaScript in an authenticated page context",
    "Network.getCookies": "returns cookies for all origins, including HttpOnly",
    "Network.getAllCookies": "returns cookies for all origins, including HttpOnly",
    "Network.setCookie": "forges credentials",
    "Page.navigate": "bypasses the navigation allowlist",
    "Browser.grantPermissions": "escalates page capabilities",
    "Fetch.enable": "allows request interception and rewriting",
}


@pytest.mark.parametrize("command", sorted(FORBIDDEN))
def test_dangerous_cdp_commands_are_not_allowlisted(command: str) -> None:
    assert command not in _ALLOWED_CDP_COMMANDS, (
        f"{command} is reachable through /sessions/{{id}}/cdp/raw — {FORBIDDEN[command]}. "
        "This allowlist is the only gate on that endpoint."
    )


def test_allowlist_is_read_only_introspection() -> None:
    """Every entry must be a getter-shaped read, not an action.

    A positive check as well as the negative one above: a command that is
    dangerous but not on the FORBIDDEN list should still fail here.
    """
    # Verb prefixes that denote a read. `query` is included because
    # DOM.querySelector locates a node without mutating anything; it is the only
    # non-`get` read currently on the list.
    allowed_prefixes = ("get", "resolve", "describe", "query")
    for command in _ALLOWED_CDP_COMMANDS:
        _, _, method = command.partition(".")
        assert method.startswith(allowed_prefixes), (
            f"{command} is not read-only introspection. The raw CDP passthrough may "
            f"only expose getters; anything that acts on the page belongs behind a "
            f"governed tool with an audit trail."
        )


def test_allowlist_is_not_empty() -> None:
    """Guard the guard — an empty list would make both checks above vacuous."""
    assert len(_ALLOWED_CDP_COMMANDS) >= 5


COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.codespaces.yml"


@pytest.mark.skipif(not COMPOSE.is_file(), reason="repo checkout not available inside the controller image")
def test_codespaces_overlay_requires_a_bearer_token() -> None:
    """The one shipped config that binds 0.0.0.0 must not run unauthenticated.

    Compose's `:?` form fails the run when the variable is unset, which is the
    behaviour we want: refuse to start rather than silently publish an
    unauthenticated control plane driving a browser that holds stored logins.
    """
    text = COMPOSE.read_text(encoding="utf-8")
    assert re.search(r"API_BEARER_TOKEN:\s*\$\{API_BEARER_TOKEN:\?", text), (
        "docker-compose.codespaces.yml publishes the API on 0.0.0.0 and must "
        "require API_BEARER_TOKEN via the ${VAR:?message} form"
    )


@pytest.mark.skipif(not COMPOSE.is_file(), reason="repo checkout not available inside the controller image")
def test_codespaces_overlay_does_not_publish_passwordless_vnc() -> None:
    """x11vnc runs -nopw, so raw VNC is only safe on a loopback bind."""
    text = COMPOSE.read_text(encoding="utf-8")
    published = re.findall(r'"0\.0\.0\.0:[^"]*:(\d+)"', text)
    assert "5900" not in published, (
        "raw VNC (5900) must not be published on 0.0.0.0 — x11vnc runs -nopw, "
        "so the port is unauthenticated by design. noVNC (6080) covers takeover."
    )
