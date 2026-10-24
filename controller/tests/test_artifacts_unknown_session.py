"""With named credentials, /artifacts serves only what belongs to a session that has a record.

The owner check reads the owner from the session record, and an unknown session has
none, so `may_use(None)` let anything through: a directory under the artifact root with
no record (a session whose record is gone, a failed create, a file dropped into the
root) was served to every credential. Nothing in the controller writes there, but
failing closed costs nothing once operators can be told apart. A deployment with no
named credential has no second operator to protect from, and is left as it was.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.app_factory import SessionArtifactFiles, build_controller_services, create_controller_app
from app.auth_policy import has_named_credentials
from app.browser_manager import BrowserManager
from app.models import OperatorIdentity
from app.session_ownership import acting_as
from tests.test_browser_manager_create_session import FakeBrowser, FakeContext, FakePage, _settings

ALICE = OperatorIdentity(id="alice", source="token")
BOB = OperatorIdentity(id="bob", source="token")
ANONYMOUS = OperatorIdentity(id="anonymous", source="anonymous")
ALICE_BYTES = b"alice-trace"
GHOST_BYTES = b"no-record-here"


class StubManager:
    """Sessions with a record: alice's, bob's and one nobody owns."""

    owners = {"sess-alice": "alice", "sess-bob": "bob", "sess-open": None}

    async def session_owner(self, session_id: str) -> str | None:
        return self.owners.get(session_id)

    async def session_owner_lookup(self, session_id: str) -> tuple[bool, str | None]:
        return session_id in self.owners, self.owners.get(session_id)


def serve(root: Path, *, require_known_session: bool):
    for session_id in ("sess-alice", "sess-bob", "sess-open", "ghost"):
        (root / session_id).mkdir(parents=True)
        (root / session_id / "trace.zip").write_bytes(GHOST_BYTES if session_id == "ghost" else ALICE_BYTES)
    (root / "notes.txt").write_bytes(GHOST_BYTES)
    app = FastAPI()
    app.mount(
        "/artifacts",
        SessionArtifactFiles(directory=str(root), manager=StubManager(), require_known_session=require_known_session),
        name="artifacts",
    )
    # The HTTP client runs in another thread, where acting_as() does not reach.
    with patch("app.session_ownership.get_current_operator") as identity, TestClient(app) as client:
        identity.return_value = ALICE
        client.identity = identity  # type: ignore[attr-defined]
        yield client


@pytest.fixture()
def named(tmp_path: Path):
    yield from serve(tmp_path / "artifacts", require_known_session=True)


@pytest.fixture()
def single(tmp_path: Path):
    yield from serve(tmp_path / "artifacts", require_known_session=False)


# ── named credentials: a path with no session record is a 404 ───────────────


@pytest.mark.parametrize("operator", [ALICE, BOB, ANONYMOUS], ids=["alice", "bob", "anonymous"])
@pytest.mark.parametrize(
    "path",
    [
        "/artifacts/ghost/trace.zip",
        "/artifacts/notes.txt",
        "/artifacts/%2e%2e/artifacts/ghost/trace.zip",
        "/artifacts/sess-alice/%2e%2e/ghost/trace.zip",
    ],
)
def test_a_path_with_no_session_record_is_not_served_to_anyone(named, operator, path: str) -> None:
    named.identity.return_value = operator

    response = named.get(path)

    assert response.status_code == 404
    assert GHOST_BYTES not in response.content


def test_sessions_that_have_a_record_are_served_as_before(named) -> None:
    named.identity.return_value = ALICE
    assert named.get("/artifacts/sess-alice/trace.zip").content == ALICE_BYTES
    assert named.get("/artifacts/sess-bob/trace.zip").status_code == 404

    named.identity.return_value = BOB
    assert named.get("/artifacts/sess-alice/trace.zip").status_code == 404
    assert named.get("/artifacts/sess-bob/trace.zip").status_code == 200


def test_a_recorded_session_nobody_owns_is_still_open_to_every_credential(named) -> None:
    for operator in (ALICE, BOB, ANONYMOUS):
        named.identity.return_value = operator

        assert named.get("/artifacts/sess-open/trace.zip").content == ALICE_BYTES


def test_a_path_that_reaches_nothing_is_still_a_plain_404(named) -> None:
    assert named.get("/artifacts/sess-alice/nope.zip").status_code == 404
    assert named.get("/artifacts/no-such/file").status_code == 404


# ── a deployment without named credentials is left as it was ────────────────


def test_a_deployment_without_named_credentials_still_serves_unrecorded_directories(single) -> None:
    single.identity.return_value = ANONYMOUS

    assert single.get("/artifacts/ghost/trace.zip").content == GHOST_BYTES
    assert single.get("/artifacts/notes.txt").content == GHOST_BYTES


# ── the real manager: live, closed and unrecorded sessions ──────────────────


@pytest.fixture
async def real(tmp_path: Path):
    manager = BrowserManager(_settings(tmp_path))
    manager._settle = AsyncMock()  # type: ignore[method-assign]
    manager._maybe_provision_session_tunnel = AsyncMock()  # type: ignore[method-assign]
    browser = FakeBrowser(FakeContext(FakePage()))
    browser.new_context = AsyncMock(side_effect=lambda **_kwargs: FakeContext(FakePage()))
    manager.browser = browser  # type: ignore[assignment]
    manager._acquire_session_browser = AsyncMock(return_value=(browser, None))  # type: ignore[method-assign]
    with acting_as(ALICE):
        closed_id = (await manager.create_session(name="done"))["id"]
        await manager.close_session(closed_id)
        live_id = (await manager.create_session(name="running"))["id"]
    root = Path(manager.settings.artifact_root)
    ghost_id = "0123456789ab"
    for session_id in (closed_id, live_id, ghost_id):
        (root / session_id).mkdir(parents=True, exist_ok=True)
        (root / session_id / "trace.zip").write_bytes(GHOST_BYTES if session_id == ghost_id else ALICE_BYTES)

    app = FastAPI()
    app.mount(
        "/artifacts",
        SessionArtifactFiles(directory=str(root), manager=manager, require_known_session=True),
        name="artifacts",
    )
    with patch("app.session_ownership.get_current_operator") as identity, TestClient(app) as client:
        identity.return_value = ALICE
        yield SimpleNamespace(client=client, identity=identity, closed=closed_id, live=live_id, ghost=ghost_id)


def test_live_and_closed_sessions_are_served_to_their_owner_and_a_session_without_a_record_to_no_one(real) -> None:
    assert real.client.get(f"/artifacts/{real.live}/trace.zip").content == ALICE_BYTES
    assert real.client.get(f"/artifacts/{real.closed}/trace.zip").content == ALICE_BYTES
    assert real.client.get(f"/artifacts/{real.ghost}/trace.zip").status_code == 404

    real.identity.return_value = BOB
    assert real.client.get(f"/artifacts/{real.live}/trace.zip").status_code == 404
    assert real.client.get(f"/artifacts/{real.closed}/trace.zip").status_code == 404
    assert real.client.get(f"/artifacts/{real.ghost}/trace.zip").status_code == 404


# ── the mount is strict exactly when operators can be told apart ────────────


@asynccontextmanager
async def _lifespan(_app):
    yield


def mounted_artifacts(tmp_path: Path, **credentials: str) -> SessionArtifactFiles:
    settings = _settings(tmp_path).model_copy(update=credentials)
    application = create_controller_app(
        services=build_controller_services(settings, version="test"), version="test", lifespan=_lifespan
    )
    return next(route.app for route in application.routes if getattr(route, "name", None) == "artifacts")


@pytest.mark.parametrize(
    ("credentials", "strict"),
    [
        ({"api_bearer_tokens": "alice:token-a,bob:token-b"}, True),
        ({"api_bearer_tokens": "alice:token-a", "api_bearer_token": "shared"}, True),
        ({"api_bearer_token": "shared"}, False),
        ({}, False),
    ],
    ids=["named", "named and shared", "shared only", "none"],
)
def test_the_app_mounts_artifacts_strictly_only_with_named_credentials(tmp_path, credentials, strict) -> None:
    settings = SimpleNamespace(**{"api_bearer_tokens": "", "api_bearer_token": None, **credentials})

    assert has_named_credentials(settings) is strict
    assert mounted_artifacts(tmp_path, **credentials).require_known_session is strict
