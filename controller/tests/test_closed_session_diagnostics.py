"""A finished session's replay script and trace are still there to read.

`GET /sessions/{id}/export-script`, `GET /sessions/{id}/trace` and the
`browser.export_script` tool resolved the session with `get_session`, which
serves live sessions only, so the script and trace.zip of a session that had
closed (when anyone wants them: trace.zip is written at close) answered 404.
They now read the owner-checked session record, which is the live summary or
the stored record, and refuses another operator's session as not found.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.action_errors import SessionNotFoundError
from app.app_factory import SessionArtifactFiles
from app.browser_manager import BrowserManager
from app.models import McpToolCallRequest, OperatorIdentity
from app.routes.session_diagnostics import create_session_diagnostics_router
from app.session_ownership import acting_as
from app.tool_gateway.gateway import McpToolGateway
from tests.test_browser_manager_create_session import FakeBrowser, FakeContext, FakePage, _settings

ALICE = OperatorIdentity(id="alice", source="token")
BOB = OperatorIdentity(id="bob", source="token")
TRACE_BYTES = b"trace-zip-bytes"


@dataclass
class Env:
    manager: BrowserManager
    gateway: McpToolGateway
    client: TestClient
    identity: object
    closed_id: str
    live_id: str


@pytest.fixture
async def env(tmp_path: Path):
    manager = BrowserManager(_settings(tmp_path))
    manager._settle = AsyncMock()  # type: ignore[method-assign]
    manager._maybe_provision_session_tunnel = AsyncMock()  # type: ignore[method-assign]
    browser = FakeBrowser(FakeContext(FakePage()))
    browser.new_context = AsyncMock(side_effect=lambda **_kwargs: FakeContext(FakePage()))
    manager.browser = browser  # type: ignore[assignment]
    manager._acquire_session_browser = AsyncMock(return_value=(browser, None))  # type: ignore[method-assign]

    with acting_as(ALICE):
        closed_id = (await manager.create_session(name="done", start_url="https://example.com/login"))["id"]
        await manager.audit.append(
            event_type="browser_action",
            status="ok",
            action="navigate",
            session_id=closed_id,
            details={"target": {"url": "https://example.com/cart"}, "verification": {}},
        )
        await manager.close_session(closed_id)
        live_id = (await manager.create_session(name="running"))["id"]
    # Closing writes trace.zip when tracing is on; these sessions run without it.
    trace_path = Path(manager.settings.artifact_root) / closed_id / "trace.zip"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_bytes(TRACE_BYTES)

    async def not_found(_request, exc: SessionNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": exc.message, "code": exc.code})

    app = FastAPI()
    app.add_exception_handler(SessionNotFoundError, not_found)
    app.include_router(create_session_diagnostics_router(manager=manager, settings=manager.settings))
    app.mount(
        "/artifacts", SessionArtifactFiles(directory=manager.settings.artifact_root, manager=manager), name="artifacts"
    )
    gateway = McpToolGateway(
        manager=manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace(), tool_profile="full"
    )
    # The HTTP client runs in another thread, where acting_as() does not reach.
    with patch("app.session_ownership.get_current_operator") as identity, TestClient(app) as client:
        identity.return_value = ALICE
        yield Env(manager, gateway, client, identity, closed_id, live_id)


async def call_tool(env: Env, operator: OperatorIdentity, session_id: str):
    env.identity.return_value = operator  # the fixture's patch decides who is acting, here as for the HTTP client
    return await env.gateway.call_tool(
        McpToolCallRequest(name="browser.export_script", arguments={"session_id": session_id})
    )


def test_the_owner_gets_a_closed_sessions_replay_script(env: Env) -> None:
    response = env.client.get(f"/sessions/{env.closed_id}/export-script")

    assert response.status_code == 200, response.text
    script = response.json()["script"]
    assert "# Start URL: https://example.com/login" in script
    assert "page.goto('https://example.com/cart')" in script


def test_the_owner_gets_a_closed_sessions_trace_and_the_artifact_it_names_is_served(env: Env) -> None:
    response = env.client.get(f"/sessions/{env.closed_id}/trace")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["trace_url"] == f"/artifacts/{env.closed_id}/trace.zip"
    assert payload["trace_size_bytes"] == len(TRACE_BYTES)
    # The route points at /artifacts, which serves a closed session's file to its owner.
    served = env.client.get(payload["trace_url"])
    assert (served.status_code, served.content) == (200, TRACE_BYTES)


def test_a_live_session_still_exports_and_has_no_trace_yet(env: Env) -> None:
    exported = env.client.get(f"/sessions/{env.live_id}/export-script")
    trace = env.client.get(f"/sessions/{env.live_id}/trace")

    assert exported.status_code == 200, exported.text
    assert (trace.status_code, trace.json()["trace_url"]) == (200, None)


@pytest.mark.parametrize("which", ["closed_id", "live_id"])
@pytest.mark.parametrize("route", ["export-script", "trace"])
def test_another_operator_gets_404_for_both_routes(env: Env, which: str, route: str) -> None:
    env.identity.return_value = BOB

    response = env.client.get(f"/sessions/{getattr(env, which)}/{route}")

    assert response.status_code == 404
    assert response.json()["code"] == "unknown_session"


@pytest.mark.parametrize("route", ["export-script", "trace"])
def test_an_unknown_session_is_404(env: Env, route: str) -> None:
    response = env.client.get(f"/sessions/no-such-session/{route}")

    assert response.status_code == 404
    assert response.json()["code"] == "unknown_session"


async def test_the_export_tool_answers_for_a_closed_session(env: Env) -> None:
    result = await call_tool(env, ALICE, env.closed_id)

    assert not result.isError, result.content[0].text
    assert "page.goto('https://example.com/cart')" in result.structuredContent["script"]


async def test_the_export_tool_refuses_another_operator_and_an_unknown_id(env: Env) -> None:
    for operator, session_id in ((BOB, env.closed_id), (BOB, env.live_id), (ALICE, "no-such-session")):
        result = await call_tool(env, operator, session_id)

        assert result.isError, (operator, session_id)
        assert result.structuredContent["code"] == "unknown_session"
