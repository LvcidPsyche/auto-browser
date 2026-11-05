"""Two raw keys that name one persona (a hand edit: "a " beside "a") never let a save or delete pick for the operator.

`_load` takes the last such entry in file order. A save used to rewrite the file with sorted keys, so saving any
persona could change which entry won; and a save or delete of the name removed every entry that normalized to it,
including a shadowed one another operator owns. A save could also overwrite an entry the store ignores (a typo in
"server") under the exact name, whoever owned it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models import McpToolCallRequest, OperatorIdentity
from app.proxy_personas import PROXY_PERSONA_DUPLICATE_ENTRIES, ProxyPersonaDuplicateError, ProxyPersonaStore
from app.routes.operations import create_operations_router
from app.session_ownership import acting_as
from app.tool_gateway import McpToolGateway

ALICE = OperatorIdentity(id="alice", source="token")
TWO_FOR_A = {"a ": {"server": "http://first:1", "owner": "bob"}, "a": {"server": "http://second:1"}}


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "personas.json"


def _write(path: Path, content: dict) -> str:
    path.write_text(json.dumps(content), encoding="utf-8")
    return path.read_text(encoding="utf-8")


def test_saving_another_persona_keeps_the_file_order_so_the_same_entry_still_wins(path: Path):
    _write(path, {"a ": {"server": "http://first:1"}, "a": {"server": "http://second:1"}})
    store = ProxyPersonaStore(path)
    assert store.get_persona("a")["server"] == "http://second:1"

    store.set_persona("b", server="http://b:1")

    assert list(json.loads(path.read_text(encoding="utf-8"))) == ["a ", "a", "b"]
    assert store.get_persona("a")["server"] == "http://second:1"


def test_a_save_of_a_name_with_two_entries_is_refused_and_logged_not_told(path: Path, caplog):
    before = _write(path, TWO_FOR_A)

    with (
        caplog.at_level(logging.ERROR, logger="app.proxy_personas"),
        pytest.raises(ProxyPersonaDuplicateError) as caught,
    ):
        ProxyPersonaStore(path).set_persona("a", server="http://new:1")

    assert str(caught.value) == PROXY_PERSONA_DUPLICATE_ENTRIES
    assert path.read_text(encoding="utf-8") == before
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "'a '" in logged and str(path) in logged


def test_a_delete_of_a_name_with_two_entries_is_refused_and_the_shadowed_entry_survives(path: Path):
    before = _write(path, TWO_FOR_A)

    with acting_as(ALICE), pytest.raises(ProxyPersonaDuplicateError):
        ProxyPersonaStore(path).delete_persona("a")

    assert path.read_text(encoding="utf-8") == before


def test_an_ignored_entry_another_operator_owns_under_the_exact_name_is_not_overwritten(path: Path):
    before = _write(path, {"a": {"serber": "http://typo:1", "owner": "bob"}})

    with acting_as(ALICE), pytest.raises(PermissionError):
        ProxyPersonaStore(path).set_persona("a", server="http://alice:1")

    assert path.read_text(encoding="utf-8") == before


def test_an_ignored_entry_the_caller_may_use_is_replaced(path: Path):
    _write(path, {"a": {"serber": "http://typo:1"}})

    with acting_as(ALICE):
        ProxyPersonaStore(path).set_persona("a", server="http://fixed:1")

    assert json.loads(path.read_text(encoding="utf-8"))["a"]["server"] == "http://fixed:1"


@pytest.mark.parametrize(
    ("verb", "url", "body"),
    [("POST", "/proxy-personas", {"name": "a", "server": "http://new:1"}), ("DELETE", "/proxy-personas/a", None)],
)
def test_rest_answers_409_with_the_fixed_text(path: Path, verb: str, url: str, body):
    before = _write(path, TWO_FOR_A)
    app = FastAPI()
    app.include_router(
        create_operations_router(manager=SimpleNamespace(), proxy_store=ProxyPersonaStore(path), cron_service=Mock())
    )

    response = TestClient(app, raise_server_exceptions=False).request(verb, url, json=body)

    assert (response.status_code, response.json()) == (409, {"detail": PROXY_PERSONA_DUPLICATE_ENTRIES})
    assert path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("browser.create_proxy_persona", {"name": "a", "server": "http://new:1"}),
        ("browser.delete_proxy_persona", {"name": "a"}),
    ],
)
async def test_mcp_gets_the_same_fixed_text(path: Path, tool: str, arguments: dict):
    before = _write(path, TWO_FOR_A)
    gateway = McpToolGateway(
        manager=SimpleNamespace(settings=SimpleNamespace(force_governed_approvals=False, autonomous_approvals=False)),
        orchestrator=SimpleNamespace(),
        job_queue=SimpleNamespace(),
        tool_profile="full",
        cron_service=SimpleNamespace(trigger_job=AsyncMock()),
        share_manager=Mock(),
        proxy_store=ProxyPersonaStore(path),
        vision_targeter=Mock(),
    )

    response = await gateway.call_tool(McpToolCallRequest(name=tool, arguments=arguments))

    assert response.isError
    assert PROXY_PERSONA_DUPLICATE_ENTRIES in response.content[0].text
    assert str(path.parent) not in response.content[0].text
    assert path.read_text(encoding="utf-8") == before
