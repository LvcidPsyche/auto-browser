"""/artifacts serves a session's files only to the operator who may use that session,
whatever spelling of the path reaches the file.

SessionArtifactFiles used to take the session id from the first component of the
path as typed. A leading "../" survives Starlette's normalisation and lets the
lookup re-enter the artifact root under its own name, so
`/artifacts/%2e%2e/artifacts/<session>/trace.zip` named the "session" ".." (which
nobody owns) while reaching another operator's file. The owner is now judged by
the file the path resolves to.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.app_factory import SessionArtifactFiles
from app.models import OperatorIdentity

ALICE = OperatorIdentity(id="alice", source="token")
BOB = OperatorIdentity(id="bob", source="token")
SECRET = b"alice-trace-bytes"


class StubManager:
    owners = {"sess-alice": "alice", "sess-bob": "bob"}

    async def session_owner(self, session_id: str) -> str | None:
        return self.owners.get(session_id)


def build_client(root: Path):
    for session_id, content in (("sess-alice", SECRET), ("sess-bob", b"bob-bytes")):
        (root / session_id).mkdir(parents=True)
        (root / session_id / "trace.zip").write_bytes(content)
    app = FastAPI()
    app.mount("/artifacts", SessionArtifactFiles(directory=str(root), manager=StubManager()), name="artifacts")
    # The HTTP client runs in another thread, where acting_as() does not reach.
    with patch("app.session_ownership.get_current_operator") as identity, TestClient(app) as client:
        identity.return_value = BOB
        client.identity = identity  # type: ignore[attr-defined]
        yield client


@pytest.fixture()
def client(tmp_path: Path):
    # The root's own name is "artifacts", as in the default /data/artifacts.
    yield from build_client(tmp_path / "artifacts")


def test_the_owner_is_served(client) -> None:
    client.identity.return_value = ALICE

    response = client.get("/artifacts/sess-alice/trace.zip")

    assert (response.status_code, response.content) == (200, SECRET)


def test_an_operator_is_served_their_own_session_by_a_dotted_spelling(client) -> None:
    client.identity.return_value = ALICE

    response = client.get("/artifacts/sess-alice/./trace.zip")

    assert (response.status_code, response.content) == (200, SECRET)


@pytest.mark.parametrize(
    "path",
    [
        "/artifacts/sess-alice/trace.zip",
        "/artifacts/%2e%2e/artifacts/sess-alice/trace.zip",
        "/artifacts/%2E%2E/artifacts/sess-alice/trace.zip",
        "/artifacts/%2e%2e/%2e%2e/%2e%2e/artifacts/sess-alice/trace.zip",
        "/artifacts/sess-bob/%2e%2e/sess-alice/trace.zip",
        "/artifacts/./sess-alice/trace.zip",
        "/artifacts//sess-alice/trace.zip",
        "/artifacts/..%5Cartifacts%5Csess-alice/trace.zip",
    ],
)
def test_another_operator_never_gets_the_bytes(client, path: str) -> None:
    response = client.get(path)

    assert SECRET not in response.content
    assert response.status_code == 404


def test_the_hole_does_not_depend_on_the_root_being_named_artifacts(tmp_path: Path) -> None:
    # A deployment with ARTIFACT_ROOT=/srv/store re-enters through that name.
    for client in build_client(tmp_path / "store"):
        response = client.get("/artifacts/%2e%2e/store/sess-alice/trace.zip")

        assert SECRET not in response.content
        assert response.status_code == 404
        client.identity.return_value = ALICE
        owner = client.get("/artifacts/%2e%2e/store/sess-alice/trace.zip")
        assert (owner.status_code, owner.content) == (200, SECRET)


def test_a_path_that_reaches_nothing_is_a_plain_404(client) -> None:
    for path in ("/artifacts/no-such/file", "/artifacts/%2e%2e/%2e%2e/etc/passwd", "/artifacts/sess-alice/nope.zip"):
        client.identity.return_value = ALICE

        assert client.get(path).status_code == 404


def test_a_link_inside_one_session_to_another_is_judged_by_its_target(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    for client in build_client(root):
        link = root / "sess-bob" / "alice-link"
        try:
            os.symlink(root / "sess-alice", link, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("this host cannot create symlinks")

        response = client.get("/artifacts/sess-bob/alice-link/trace.zip")

        assert SECRET not in response.content
        assert response.status_code == 404
