"""An /artifacts request neither blocks the event loop nor turns a filesystem error into a 500.

SessionArtifactFiles looked the path up itself, ahead of Starlette's
StaticFiles.get_response, and did it on the event loop. Starlette runs that lookup in a
thread and maps its errors (401 for PermissionError, 404 for a name that is too long or
holds a null byte), so by calling it first the mount lost both: every artifact request
did a blocking realpath and stat on the loop, and a path the filesystem refused gave a
500 with a traceback where the baseline gave a 404.
"""

from __future__ import annotations

import errno
import logging
import os
import threading
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.responses import PlainTextResponse

from app.app_factory import SessionArtifactFiles
from app.session_ownership import acting_as
from tests.test_artifacts_traversal_ownership import ALICE, BOB, SECRET, StubManager, build_client

SCOPE = {"type": "http", "method": "GET", "headers": []}


@pytest.fixture()
def client(tmp_path: Path):
    yield from build_client(tmp_path / "artifacts")


def make_files(tmp_path: Path) -> SessionArtifactFiles:
    root = tmp_path / "artifacts"
    (root / "sess-alice").mkdir(parents=True)
    (root / "sess-alice" / "trace.zip").write_bytes(SECRET)
    return SessionArtifactFiles(directory=str(root), manager=StubManager())


# ── the lookup is off the event loop ────────────────────────────────────────


async def test_no_path_is_resolved_on_the_event_loop(tmp_path: Path) -> None:
    files = make_files(tmp_path)
    loop_thread = threading.get_ident()
    resolved_on: list[int] = []
    real_realpath = os.path.realpath

    def spy(path, *args, **kwargs):
        resolved_on.append(threading.get_ident())
        return real_realpath(path, *args, **kwargs)

    with patch("os.path.realpath", side_effect=spy), acting_as(ALICE):
        response = await files.get_response("sess-alice/trace.zip", SCOPE)

    assert response.status_code == 200
    assert resolved_on, "the spy saw no lookup at all"
    assert loop_thread not in resolved_on


# ── a filesystem error is a 404 ─────────────────────────────────────────────

ERRORS = {
    "permission denied": PermissionError(errno.EACCES, "denied"),
    "name too long": OSError(errno.ENAMETOOLONG, "File name too long"),
    "i/o error": OSError(errno.EIO, "Input/output error"),
    "null byte": ValueError("embedded null byte"),
}


@pytest.mark.parametrize("error", ERRORS.values(), ids=ERRORS.keys())
def test_a_path_the_filesystem_refuses_is_a_404(client, error) -> None:
    client.identity.return_value = ALICE

    with patch.object(SessionArtifactFiles, "lookup_path", side_effect=error):
        response = client.get("/artifacts/sess-alice/trace.zip")

    assert response.status_code == 404


def test_a_null_byte_in_the_path_is_a_404(client) -> None:
    client.identity.return_value = ALICE

    assert client.get("/artifacts/sess-alice/trace%00.zip").status_code == 404


def test_a_name_far_too_long_is_a_404(client) -> None:
    client.identity.return_value = ALICE

    assert client.get(f"/artifacts/{'a' * 5000}/trace.zip").status_code == 404
    assert client.get(f"/artifacts/sess-alice/{'a' * 5000}").status_code == 404


def test_an_unexpected_filesystem_error_is_logged_but_a_bad_name_is_not(client, caplog) -> None:
    client.identity.return_value = ALICE

    with caplog.at_level(logging.WARNING, logger="app.app_factory"):
        with patch.object(SessionArtifactFiles, "lookup_path", side_effect=ERRORS["null byte"]):
            client.get("/artifacts/sess-alice/trace.zip")
        with patch.object(SessionArtifactFiles, "lookup_path", side_effect=ERRORS["name too long"]):
            client.get("/artifacts/sess-alice/trace.zip")
        assert caplog.records == []
        with patch.object(SessionArtifactFiles, "lookup_path", side_effect=ERRORS["permission denied"]):
            client.get("/artifacts/sess-alice/trace.zip")

    assert len(caplog.records) == 1
    assert "denied" in caplog.records[0].getMessage()


def refuse_naming_the_file(path: str):
    """Fail like Windows does for a name it cannot parse (EINVAL), with the name in the error."""
    raise OSError(errno.EINVAL, "Invalid argument", path)


def test_a_logged_path_is_capped_so_a_client_cannot_write_huge_log_lines(client, caplog) -> None:
    client.identity.return_value = ALICE
    long_name = "a" * 5000

    with caplog.at_level(logging.WARNING, logger="app.app_factory"):
        with patch.object(SessionArtifactFiles, "lookup_path", side_effect=refuse_naming_the_file):
            response = client.get(f"/artifacts/{long_name}")

    assert response.status_code == 404
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "aaaa" in message  # still says which request it was
    assert "Invalid argument" in message
    assert "a" * 300 not in message  # neither the requested path nor the path the error carries
    assert len(message) < 600


def test_a_short_path_is_logged_whole(client, caplog) -> None:
    client.identity.return_value = ALICE

    with caplog.at_level(logging.WARNING, logger="app.app_factory"):
        with patch.object(SessionArtifactFiles, "lookup_path", side_effect=refuse_naming_the_file):
            client.get("/artifacts/trace.zip")

    message = caplog.records[0].getMessage()
    assert message.count("trace.zip") == 2  # the path asked for, and the one in the error
    assert "..." not in message


# ── the owner check still comes before the parent's response ────────────────


async def test_another_operators_file_never_reaches_the_parent_response(tmp_path: Path) -> None:
    files = make_files(tmp_path)
    parent = AsyncMock(return_value=PlainTextResponse("served"))

    with patch.object(StaticFiles, "get_response", new=parent):
        with acting_as(BOB), pytest.raises(HTTPException) as refused:
            await files.get_response("sess-alice/trace.zip", SCOPE)
        assert refused.value.status_code == 404
        assert parent.await_count == 0

        with acting_as(ALICE):
            response = await files.get_response("sess-alice/trace.zip", SCOPE)
        assert response.body == b"served"
        assert parent.await_count == 1


def test_the_owner_is_still_served_end_to_end(client) -> None:
    client.identity.return_value = ALICE

    response = client.get("/artifacts/sess-alice/trace.zip")

    assert (response.status_code, response.content) == (200, SECRET)
