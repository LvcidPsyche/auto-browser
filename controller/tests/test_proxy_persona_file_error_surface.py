"""A corrupt persona file reaches REST and MCP callers as one fixed, path-free message.

The file's path and the OS error text belong in the server log only: over HTTP the error used to answer
400 "Invalid request", 409 "Conflict" or a bare 500, and over MCP it sent the path to the agent.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models import McpToolCallRequest
from app.proxy_personas import PROXY_PERSONA_FILE_UNREADABLE, ProxyPersonaFileError, ProxyPersonaStore
from app.routes.operations import create_operations_router
from app.routes.sessions import create_sessions_router
from app.tool_gateway import McpToolGateway

CORRUPT = '{"a": {"server": "http://a:1"'


@pytest.fixture
def corrupt_path(tmp_path: Path) -> Path:
    path = tmp_path / "secret-dir" / "personas.json"
    path.parent.mkdir()
    path.write_text(CORRUPT, encoding="utf-8")
    return path


@pytest.fixture
def client(corrupt_path: Path) -> TestClient:
    store = ProxyPersonaStore(corrupt_path)

    async def create_session(**kwargs):
        store.resolve_proxy(kwargs["proxy_persona"])

    manager = SimpleNamespace(create_session=create_session)
    app = FastAPI()
    app.include_router(create_operations_router(manager=manager, proxy_store=store, cron_service=Mock()))
    app.include_router(create_sessions_router(manager=manager))
    return TestClient(app, raise_server_exceptions=False)


def _assert_fixed(response, corrupt_path: Path) -> None:
    assert response.status_code == 503
    assert response.json() == {"detail": PROXY_PERSONA_FILE_UNREADABLE}
    assert str(corrupt_path.parent) not in response.text
    assert "personas.json" not in response.text


def test_the_message_names_the_setting_not_the_path(corrupt_path: Path):
    with pytest.raises(ProxyPersonaFileError) as caught:
        ProxyPersonaStore(corrupt_path).list_personas()
    assert str(caught.value) == PROXY_PERSONA_FILE_UNREADABLE
    assert "PROXY_PERSONA_FILE" in PROXY_PERSONA_FILE_UNREADABLE
    assert str(corrupt_path) not in str(caught.value)


@pytest.mark.parametrize(
    ("verb", "path", "body"),
    [
        ("get", "/proxy-personas", None),
        ("get", "/proxy-personas/a", None),
        ("post", "/proxy-personas", {"name": "z", "server": "http://z:1"}),
        ("delete", "/proxy-personas/a", None),
        ("post", "/sessions", {"proxy_persona": "a"}),
    ],
)
def test_rest_answers_503_with_the_fixed_text(client: TestClient, corrupt_path: Path, verb, path, body):
    response = client.request(verb.upper(), path, json=body)
    _assert_fixed(response, corrupt_path)
    assert corrupt_path.read_text(encoding="utf-8") == CORRUPT


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("browser.list_proxy_personas", {}),
        ("browser.create_proxy_persona", {"name": "z", "server": "http://z:1"}),
        ("browser.delete_proxy_persona", {"name": "a"}),
    ],
)
async def test_mcp_gets_the_same_fixed_text(corrupt_path: Path, tool: str, arguments: dict):
    gateway = McpToolGateway(
        manager=SimpleNamespace(settings=SimpleNamespace(force_governed_approvals=False, autonomous_approvals=False)),
        orchestrator=SimpleNamespace(),
        job_queue=SimpleNamespace(),
        tool_profile="full",
        cron_service=SimpleNamespace(trigger_job=AsyncMock()),
        share_manager=Mock(),
        proxy_store=ProxyPersonaStore(corrupt_path),
        vision_targeter=Mock(),
    )

    response = await gateway.call_tool(McpToolCallRequest(name=tool, arguments=arguments))

    assert response.isError
    assert PROXY_PERSONA_FILE_UNREADABLE in response.content[0].text
    assert str(corrupt_path.parent) not in response.content[0].text
    assert "personas.json" not in response.content[0].text
