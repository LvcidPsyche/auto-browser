"""A corrupt persona file reaches REST and MCP callers as one fixed, path-free message.

The file's path and the OS error text belong in the server log only: over HTTP the error used to answer
400 "Invalid request", 409 "Conflict" or a bare 500, and over MCP it sent the path to the agent.
"""

from __future__ import annotations

import copy
import pickle
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.cron_service import CronService
from app.models import McpToolCallRequest
from app.proxy_personas import (
    PROXY_PERSONA_FILE_UNREADABLE,
    ProxyPersonaDuplicateError,
    ProxyPersonaFileError,
    ProxyPersonaStore,
)
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

    async def fork_session(session_id, **kwargs):
        # The fork's create_session reads the source's (or the named) persona the same way.
        store.resolve_proxy(kwargs["proxy_persona"] or "a")

    manager = SimpleNamespace(create_session=create_session, fork_session=fork_session)
    app = FastAPI()
    app.include_router(create_operations_router(manager=manager, proxy_store=store, cron_service=Mock()))
    app.include_router(create_sessions_router(manager=manager))
    return TestClient(app, raise_server_exceptions=False)


def _assert_fixed(response, corrupt_path: Path) -> None:
    assert response.status_code == 503
    assert response.json() == {"detail": PROXY_PERSONA_FILE_UNREADABLE}
    assert str(corrupt_path.parent) not in response.text
    assert "personas.json" not in response.text


@pytest.mark.parametrize("error_type", [ProxyPersonaFileError, ProxyPersonaDuplicateError])
def test_the_fixed_errors_survive_copy_and_pickle(error_type: type[Exception]):
    original = error_type()
    pickled = pickle.loads(pickle.dumps(original))  # noqa: S301 - bytes this test just made
    for clone in (copy.copy(original), copy.deepcopy(original), pickled):
        assert type(clone) is error_type
        assert str(clone) == str(original)


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
        # B-fix-6 (NAV-04): the fork route answered this 409 Conflict (ProxyPersonaFileError is a RuntimeError).
        ("post", "/sessions/s1/fork?proxy_persona=a", None),
    ],
)
def test_rest_answers_503_with_the_fixed_text(client: TestClient, corrupt_path: Path, verb, path, body):
    response = client.request(verb.upper(), path, json=body)
    _assert_fixed(response, corrupt_path)
    assert corrupt_path.read_text(encoding="utf-8") == CORRUPT


def _resolving_manager(store: ProxyPersonaStore) -> SimpleNamespace:
    """A manager whose create_session reads the persona the way BrowserManager does."""

    async def create_session(**kwargs):
        store.resolve_proxy(kwargs["proxy_persona"])

    return SimpleNamespace(
        create_session=create_session,
        settings=SimpleNamespace(force_governed_approvals=False, autonomous_approvals=False),
    )


def _gateway(store: ProxyPersonaStore, *, cron_service=None) -> McpToolGateway:
    return McpToolGateway(
        manager=_resolving_manager(store),
        orchestrator=SimpleNamespace(),
        job_queue=SimpleNamespace(),
        tool_profile="full",
        cron_service=cron_service,
        share_manager=Mock(),
        proxy_store=store,
        vision_targeter=Mock(),
    )


def test_the_cron_webhook_trigger_answers_503_with_the_fixed_text(tmp_path: Path, corrupt_path: Path):
    store = ProxyPersonaStore(corrupt_path)
    manager = _resolving_manager(store)
    cron = CronService(tmp_path / "crons.json", job_queue=Mock(), manager=manager)
    app = FastAPI()
    app.include_router(create_operations_router(manager=manager, proxy_store=store, cron_service=cron))
    client = TestClient(app, raise_server_exceptions=False)
    job = client.post("/crons", json={"name": "j", "goal": "g", "proxy_persona": "a", "webhook_enabled": True}).json()

    response = client.post(f"/crons/{job['id']}/trigger", json={"webhook_key": job["webhook_key"]})

    _assert_fixed(response, corrupt_path)
    assert cron._active_runs == {}


async def test_mcp_create_session_with_a_persona_gets_the_fixed_text(corrupt_path: Path):
    response = await _gateway(ProxyPersonaStore(corrupt_path)).call_tool(
        McpToolCallRequest(name="browser.create_session", arguments={"proxy_persona": "a"})
    )

    assert response.isError
    assert PROXY_PERSONA_FILE_UNREADABLE in response.content[0].text
    assert str(corrupt_path.parent) not in response.content[0].text


async def test_mcp_cron_trigger_of_a_persona_job_gets_the_fixed_text(tmp_path: Path, corrupt_path: Path):
    store = ProxyPersonaStore(corrupt_path)
    cron = CronService(tmp_path / "crons.json", job_queue=Mock(), manager=_resolving_manager(store))
    job = await cron.create_job(name="j", goal="g", proxy_persona="a")

    response = await _gateway(store, cron_service=cron).call_tool(
        McpToolCallRequest(name="browser.trigger_cron_job", arguments={"job_id": job["id"]})
    )

    assert response.isError
    assert PROXY_PERSONA_FILE_UNREADABLE in response.content[0].text
    assert str(corrupt_path.parent) not in response.content[0].text
    assert cron._active_runs == {}


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("browser.list_proxy_personas", {}),
        ("browser.create_proxy_persona", {"name": "z", "server": "http://z:1"}),
        ("browser.delete_proxy_persona", {"name": "a"}),
    ],
)
async def test_mcp_gets_the_same_fixed_text(corrupt_path: Path, tool: str, arguments: dict):
    settings = SimpleNamespace(
        force_governed_approvals=False, autonomous_approvals=False, api_admin_operator_ids=frozenset()
    )
    gateway = McpToolGateway(
        manager=SimpleNamespace(settings=settings),
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
