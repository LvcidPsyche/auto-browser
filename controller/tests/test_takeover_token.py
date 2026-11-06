"""The takeover token: a second secret, held by the person, that GET /sessions/{id}/takeover-link demands (NAV-14b).

The bearer token is the agents' token, so an agent that can make its own HTTP calls read any isolated session's
password link with it. The takeover token is loaded or generated at startup next to the API token, and nothing
agent-facing reads it.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

import pytest

from app.api_token import ensure_takeover_token
from app.config import Settings
from app.runtime_policy import MIN_PRODUCTION_BEARER_TOKEN_LENGTH

REPO = Path(__file__).resolve().parents[2]
SENTINEL = "takeover-sentinel-" + "t" * MIN_PRODUCTION_BEARER_TOKEN_LENGTH
API_SENTINEL = "api-sentinel-" + "a" * MIN_PRODUCTION_BEARER_TOKEN_LENGTH
LOGGER = "app.api_token"


def make_settings(tmp_path: Path, **overrides: str) -> Settings:
    values = {"TAKEOVER_TOKEN_FILE": str(tmp_path / "takeover-token"), "API_TOKEN_FILE": str(tmp_path / "api-token")}
    return Settings(_env_file=None, **{**values, **overrides})


def test_the_settings_keep_the_token_out_of_repr_and_dumps(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, TAKEOVER_TOKEN=SENTINEL)
    assert settings.takeover_token == SENTINEL
    assert SENTINEL not in repr(settings)
    assert SENTINEL not in str(settings.model_dump())
    assert Settings.model_fields["takeover_token_file"].default == "/data/takeover-token"


def test_a_whitespace_only_takeover_token_is_unset(tmp_path: Path) -> None:
    assert make_settings(tmp_path, TAKEOVER_TOKEN="   ").takeover_token is None


@pytest.mark.parametrize("scope", ["loopback", "exposed"])
def test_the_first_start_generates_a_private_file_and_the_next_reuses_it(
    tmp_path: Path, scope: str, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "takeover-token"
    settings = make_settings(tmp_path, API_BIND_SCOPE=scope)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        ensure_takeover_token(settings)
    token = settings.takeover_token
    assert token and len(token) >= MIN_PRODUCTION_BEARER_TOKEN_LENGTH
    assert path.read_text(encoding="utf-8").strip() == token
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert token not in caplog.text

    again = make_settings(tmp_path, API_BIND_SCOPE=scope)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        ensure_takeover_token(again)
    assert again.takeover_token == token
    assert "Using the takeover token" in caplog.text  # the reuse logs a fingerprint, never the token
    assert token not in caplog.text


def test_tokenless_loopback_still_gets_a_takeover_token(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, API_BIND_SCOPE="loopback", API_ALLOW_TOKENLESS_LOOPBACK="true")
    ensure_takeover_token(settings)
    assert settings.takeover_token


def test_a_configured_token_wins_and_the_file_is_neither_read_nor_written(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, TAKEOVER_TOKEN=SENTINEL)
    ensure_takeover_token(settings)
    assert settings.takeover_token == SENTINEL
    assert not (tmp_path / "takeover-token").exists()


def test_a_configured_token_wins_over_an_existing_file_which_is_left_as_it_was(tmp_path: Path) -> None:
    # W2-3: the test above starts with no file, so a regression that preferred an existing file passed.
    path = tmp_path / "takeover-token"
    on_disk = "file-sentinel-" + "f" * MIN_PRODUCTION_BEARER_TOKEN_LENGTH + "\n"
    path.write_text(on_disk, encoding="utf-8")
    settings = make_settings(tmp_path, TAKEOVER_TOKEN=SENTINEL)
    ensure_takeover_token(settings)
    assert settings.takeover_token == SENTINEL
    assert path.read_text(encoding="utf-8") == on_disk


def test_a_short_configured_token_refuses_startup(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, TAKEOVER_TOKEN="short-sentinel")
    with pytest.raises(RuntimeError, match="TAKEOVER_TOKEN") as raised:
        ensure_takeover_token(settings)
    assert "short-sentinel" not in str(raised.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"API_BEARER_TOKEN": SENTINEL},
        {"API_BEARER_TOKENS": f"alice:{API_SENTINEL},bob:{SENTINEL}"},
    ],
)
def test_a_configured_token_equal_to_an_api_credential_refuses_startup(tmp_path: Path, overrides: dict) -> None:
    settings = make_settings(tmp_path, TAKEOVER_TOKEN=SENTINEL, **overrides)
    with pytest.raises(RuntimeError, match="TAKEOVER_TOKEN") as raised:
        ensure_takeover_token(settings)
    assert SENTINEL not in str(raised.value)


def test_an_unwritable_file_warns_and_leaves_no_token(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    settings = make_settings(tmp_path, TAKEOVER_TOKEN_FILE=str(blocker / "takeover-token"))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        ensure_takeover_token(settings)  # does not raise: the controller starts
    assert settings.takeover_token is None
    assert any(r.levelno == logging.WARNING and "503" in r.getMessage() for r in caplog.records), caplog.text


def test_an_unusable_file_warns_and_is_not_overwritten(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "takeover-token"
    path.write_text("too-short\n", encoding="utf-8")
    settings = make_settings(tmp_path)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        ensure_takeover_token(settings)
    assert settings.takeover_token is None
    assert path.read_text(encoding="utf-8") == "too-short\n"
    assert "too-short" not in caplog.text


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes: Windows applies the folder's ACL")
def test_w2_23_a_file_whose_mode_cannot_be_tightened_fails_closed_and_says_why(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A root-owned 0444 secret mount reads fine, but its mode cannot be changed: the warning said the file must
    # "hold a usable token (or can be written)", which it did.
    from unittest.mock import patch

    from app import api_token

    path = tmp_path / "takeover-token"
    path.write_text(SENTINEL + "\n", encoding="utf-8")
    path.chmod(0o644)
    settings = make_settings(tmp_path)
    with (
        patch.object(api_token.os, "fchmod", side_effect=PermissionError(1, "Operation not permitted")),
        caplog.at_level(logging.WARNING, logger=LOGGER),
    ):
        ensure_takeover_token(settings)
    assert settings.takeover_token is None
    assert "mode could not be tightened" in caplog.text
    assert "can be written" not in caplog.text
    assert SENTINEL not in caplog.text


def test_a_file_token_equal_to_an_api_credential_is_not_used(tmp_path: Path) -> None:
    (tmp_path / "takeover-token").write_text(SENTINEL + "\n", encoding="utf-8")
    settings = make_settings(tmp_path, API_BEARER_TOKEN=SENTINEL)
    ensure_takeover_token(settings)
    assert settings.takeover_token is None


def test_the_lifespan_loads_the_takeover_token_after_the_api_token() -> None:
    text = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    assert text.index("ensure_api_credential(settings)") < text.index("ensure_takeover_token(settings)")


@pytest.mark.skipif(not (REPO / "client").is_dir(), reason="the agent-facing packages are not shipped in the image")
def test_no_agent_facing_package_reads_the_takeover_token() -> None:
    for package in ("client", "integrations/langchain", "packaging/auto-browser-mcp"):
        for path in (REPO / package).rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for needle in ("TAKEOVER_TOKEN", "takeover-token", "takeover_token", "X-Takeover-Token"):
                assert needle not in text, f"{path} names {needle}"


@pytest.mark.parametrize(
    "configured",
    [
        "takeover-sentinel-with a-space-" + "s" * MIN_PRODUCTION_BEARER_TOKEN_LENGTH,
        "takeover-sentinel-ümlaut-" + "u" * MIN_PRODUCTION_BEARER_TOKEN_LENGTH,
        "takeover-sentinel-tab\t-" + "t" * MIN_PRODUCTION_BEARER_TOKEN_LENGTH,
    ],
)
def test_w2_21_a_configured_token_outside_printable_ascii_refuses_startup_without_naming_it(
    tmp_path: Path, configured: str
) -> None:
    # The file's rule now holds for TAKEOVER_TOKEN too: Starlette decodes headers as latin-1 and curl sends UTF-8, so
    # a non-ASCII token could never be presented, and a space cannot survive the header's whitespace stripping.
    settings = make_settings(tmp_path, TAKEOVER_TOKEN=configured)
    with pytest.raises(RuntimeError, match="printable ASCII") as raised:
        ensure_takeover_token(settings)
    assert configured not in str(raised.value)
    assert "sentinel" not in str(raised.value)


@pytest.mark.parametrize("root", ["ARTIFACT_ROOT", "UPLOAD_ROOT"])
def test_w2_21_a_token_file_inside_a_served_or_uploadable_root_refuses_startup(tmp_path: Path, root: str) -> None:
    # /artifacts serves ARTIFACT_ROOT and uploads land in UPLOAD_ROOT: a token there can be read or replaced.
    served = tmp_path / "served"
    settings = make_settings(tmp_path, **{root: str(served), "TAKEOVER_TOKEN_FILE": str(served / "x" / ".." / "tok")})
    with pytest.raises(RuntimeError, match=root):
        ensure_takeover_token(settings)
    assert not (served / "tok").exists()


# Every data root the API reads, lists, exports or mounts (W4-1): AUTH_ROOT, for one, is packaged by
# GET /auth-profiles/{name}/export.
DATA_ROOT_SETTINGS = [
    "ARTIFACT_ROOT",
    "UPLOAD_ROOT",
    "AUTH_ROOT",
    "APPROVAL_ROOT",
    "AUDIT_ROOT",
    "WITNESS_ROOT",
    "SESSION_STORE_ROOT",
    "JOB_STORE_ROOT",
    "HARNESS_ROOT",
    "MEMORY_ROOT",
    "MESH_IDENTITY_DIR",
    "ISOLATED_TUNNEL_INFO_ROOT",
]


def data_layout(data: Path) -> dict[str, str]:
    """The shipped layout under ``data`` (/data in the image), so the fixed mount paths resolve under it too."""
    return {
        "ARTIFACT_ROOT": str(data / "artifacts"),
        "UPLOAD_ROOT": str(data / "uploads"),
        "AUTH_ROOT": str(data / "auth"),
        "APPROVAL_ROOT": str(data / "approvals"),
        "AUDIT_ROOT": str(data / "audit"),
        "WITNESS_ROOT": str(data / "witness"),
        "SESSION_STORE_ROOT": str(data / "sessions"),
        "JOB_STORE_ROOT": str(data / "jobs"),
        "HARNESS_ROOT": str(data / "harness"),
        "MEMORY_ROOT": str(data / "memory"),
        "MESH_IDENTITY_DIR": str(data / "mesh" / "identity"),
        "ISOLATED_TUNNEL_INFO_ROOT": str(data / "tunnels" / "sessions"),
        "BROWSER_WS_ENDPOINT_FILE": str(data / "browser-profile" / "browser-ws-endpoint.txt"),
    }


@pytest.mark.parametrize("root", DATA_ROOT_SETTINGS)
def test_w4_1_a_token_file_inside_any_data_root_refuses_startup(tmp_path: Path, root: str) -> None:
    data = tmp_path / "data"
    inside = Path(data_layout(data)[root]) / "demo" / "takeover-token"
    settings = make_settings(tmp_path, **data_layout(data), TAKEOVER_TOKEN_FILE=str(inside))
    with pytest.raises(RuntimeError, match=root):
        ensure_takeover_token(settings)
    assert not inside.exists()


@pytest.mark.skipif(os.name == "nt", reason="creating a symlink on Windows needs a privilege CI's runner lacks")
@pytest.mark.parametrize("root", ["AUTH_ROOT", "MEMORY_ROOT"])
def test_w4_1_a_token_file_reached_through_a_symlinked_parent_is_refused(tmp_path: Path, root: str) -> None:
    data = tmp_path / "data"
    layout = data_layout(data)
    Path(layout[root]).mkdir(parents=True)
    link = tmp_path / "elsewhere"
    link.symlink_to(layout[root], target_is_directory=True)
    settings = make_settings(tmp_path, **layout, TAKEOVER_TOKEN_FILE=str(link / "takeover-token"))
    with pytest.raises(RuntimeError, match=root):
        ensure_takeover_token(settings)
    assert not (Path(layout[root]) / "takeover-token").exists()


@pytest.mark.parametrize(
    ("relative", "named"),
    [
        ("browser-profile/takeover-token", "BROWSER_WS_ENDPOINT_FILE"),
        ("downloads/takeover-token", "downloads"),
        ("browser-sessions/abc123/profile/takeover-token", "browser-sessions"),
        ("tunnels/takeover-token", "ISOLATED_TUNNEL_INFO_ROOT"),
    ],
)
def test_w4_1_a_token_file_in_a_directory_mounted_into_a_container_refuses_startup(
    tmp_path: Path, relative: str, named: str
) -> None:
    # browser-node mounts browser-profile and downloads, each isolated session gets browser-sessions/<id>/, and
    # reverse-ssh mounts the tunnels directory: a compromised container could read or replace a token there.
    data = tmp_path / "data"
    inside = data / relative
    settings = make_settings(tmp_path, **data_layout(data), TAKEOVER_TOKEN_FILE=str(inside))
    with pytest.raises(RuntimeError, match=named):
        ensure_takeover_token(settings)
    assert not inside.exists()


def test_w4_1_the_default_token_file_in_the_data_root_itself_is_accepted(tmp_path: Path) -> None:
    data = tmp_path / "data"
    settings = make_settings(tmp_path, **data_layout(data), TAKEOVER_TOKEN_FILE=str(data / "takeover-token"))
    ensure_takeover_token(settings)
    assert settings.takeover_token
    assert (data / "takeover-token").read_text(encoding="utf-8").strip() == settings.takeover_token
