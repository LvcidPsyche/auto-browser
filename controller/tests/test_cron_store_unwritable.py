"""A cron store its JSON writer cannot write back meets every save with an error its caller handles.

The JSON reader takes what the writer cannot write: a value nested deeper than the writer goes (the reader of Python
3.12 and later goes deeper than the writer, which recurses), or a run_count of 4,300 nines, which loads and is one
digit too long to print once a run adds one. The save raised RecursionError or ValueError, which the start, a refused
run's mark and the routes did not take, and the trigger took the one and not the other: the start stopped, and a
route or a trigger whose run was queued answered 500. The save now raises CronStoreUnwritableError, a
CronStoreUnreadableError, whatever the writer gave up on.

Where the writer gives up on depth is the interpreter's, so the RecursionError is stood in for; the 4,300 digits are
real.
"""

from __future__ import annotations

import json
import logging
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import cron_service as cron_service_module
from app.cron_service import OWNER_CREDENTIAL_REMOVED, CronService, CronStoreUnreadableError
from app.models import McpToolCallRequest
from app.routes.operations import create_operations_router
from app.session_ownership import as_system
from app.tool_gateway import McpToolGateway

# A run_count the JSON reader takes, as long as Python prints an integer: plus one, the writer raises ValueError.
FOUR_THOUSAND_THREE_HUNDRED_NINES = "9" * 4300


def _writer_gives_up():
    """The store's JSON writer, as Python's gives up on a value nested deeper than it goes: in the store's module only."""

    def dumps(*_args: Any, **_kwargs: Any) -> str:
        raise RecursionError("maximum recursion depth exceeded while encoding a JSON object")

    return patch.object(cron_service_module, "json", SimpleNamespace(loads=json.loads, dumps=dumps))


def _job(job_id: str, **fields: Any) -> dict[str, Any]:
    return {"id": job_id, "name": "n", "goal": "g", "enabled": True, "schedule": None, "owner": None, **fields}


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "secret-dir" / "crons.json"


def _write(store_path: Path, text: str) -> bytes:
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(text, encoding="utf-8")
    return store_path.read_bytes()


def _run_fakes() -> tuple[SimpleNamespace, SimpleNamespace]:
    manager = SimpleNamespace(
        settings=SimpleNamespace(api_bearer_tokens="", api_bearer_token=None),
        create_session=AsyncMock(return_value={"id": "session-1"}),
        close_session=AsyncMock(),
        audit=SimpleNamespace(append=AsyncMock()),
    )
    return manager, SimpleNamespace(enqueue_run=AsyncMock(return_value={"id": "agent-job-1"}))


def test_the_error_is_an_unreadable_store_with_a_fixed_text_of_its_own():
    error = cron_service_module.CronStoreUnwritableError()

    assert isinstance(error, CronStoreUnreadableError)
    assert str(error) == cron_service_module.CRON_STORE_UNWRITABLE
    assert "CRON_STORE_PATH" in str(error)


async def test_the_start_with_a_legacy_key_still_starts(store_path: Path, caplog):
    before = _write(store_path, json.dumps({"j1": _job("j1", webhook_key="k" * 64)}))

    with (
        patch("app.cron_service._APSCHEDULER_AVAILABLE", False),
        _writer_gives_up(),
        caplog.at_level(logging.ERROR, logger="app.cron_service"),
    ):
        await CronService(store_path).startup()

    assert store_path.read_bytes() == before
    logged = [record.getMessage() for record in caplog.records]
    assert any("1 job(s) still hold their webhook key in plaintext" in line for line in logged), logged
    assert any(str(store_path) in line and "RecursionError" in line for line in logged), logged


async def test_a_refused_run_is_answered_and_its_mark_logged(store_path: Path, caplog):
    before = _write(store_path, json.dumps({"j1": _job("j1", owner="alice")}))
    manager, job_queue = _run_fakes()
    service = CronService(store_path, job_queue=job_queue, manager=manager)

    # As the system, as the scheduler fires it: another operator's job is no job to a caller.
    with as_system(), _writer_gives_up(), caplog.at_level(logging.ERROR, logger="app.cron_service"):
        answer = await service.trigger_job("j1")

    assert answer == {"triggered": False, "job_id": "j1", "reason": OWNER_CREDENTIAL_REMOVED}
    manager.audit.append.assert_awaited_once()
    manager.create_session.assert_not_awaited()
    assert store_path.read_bytes() == before
    logged = [record.getMessage() for record in caplog.records]
    assert any("its refusal could not be saved in last_status" in line for line in logged), logged
    assert any("that its refusal's audit event is written could not be saved" in line for line in logged), logged


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        pytest.param("post", "/crons", {"name": "n", "goal": "g", "webhook_enabled": True}, id="create"),
        pytest.param("delete", "/crons/j1", None, id="delete"),
        pytest.param("post", "/crons/j1/webhook-key/rotate", None, id="rotate"),
        pytest.param("delete", "/crons/j1/webhook-key", None, id="revoke"),
    ],
)
def test_a_routes_save_answers_503_with_the_fixed_text(store_path: Path, method: str, url: str, body):
    before = _write(store_path, json.dumps({"j1": _job("j1", webhook_key_hash="h" * 64), "j2": _job("j2")}))
    application = FastAPI()
    application.include_router(
        create_operations_router(manager=Mock(), proxy_store=Mock(), cron_service=CronService(store_path))
    )

    with _writer_gives_up():
        response = TestClient(application, raise_server_exceptions=False).request(method, url, json=body)

    assert response.status_code == 503, response.text
    assert response.json() == {"detail": cron_service_module.CRON_STORE_UNWRITABLE}
    assert "secret-dir" not in response.text
    assert store_path.read_bytes() == before


@pytest.mark.parametrize("writer", ["4,300 nines", "a value nested past the writer"])
async def test_the_trigger_whose_run_is_queued_still_answers_as_queued(store_path: Path, writer: str, caplog):
    job = json.dumps(_job("j1"))[:-1] + ', "run_count": ' + FOUR_THOUSAND_THREE_HUNDRED_NINES + "}"
    stored = {"4,300 nines": job, "a value nested past the writer": json.dumps(_job("j1"))}[writer]
    before = _write(store_path, '{"j1": ' + stored + "}")
    manager, job_queue = _run_fakes()
    service = CronService(store_path, job_queue=job_queue, manager=manager)

    stand_in = _writer_gives_up() if writer == "a value nested past the writer" else nullcontext()
    with stand_in, caplog.at_level(logging.ERROR, logger="app.cron_service"):
        answer = await service.trigger_job("j1")

    assert answer["triggered"] is True and answer["session_id"] == "session-1", answer
    job_queue.enqueue_run.assert_awaited_once()
    assert store_path.read_bytes() == before
    logged = [record.getMessage() for record in caplog.records]
    assert any(line.startswith("cron job j1: its run is queued, but the run's metadata") for line in logged), logged


async def test_an_mcp_tool_answers_the_fixed_text(store_path: Path):
    before = _write(store_path, json.dumps({"j1": _job("j1")}))
    gateway = McpToolGateway(
        manager=SimpleNamespace(settings=SimpleNamespace(force_governed_approvals=False, autonomous_approvals=False)),
        orchestrator=SimpleNamespace(),
        job_queue=SimpleNamespace(),
        tool_profile="full",
        cron_service=CronService(store_path),
        share_manager=Mock(),
        proxy_store=Mock(),
        vision_targeter=Mock(),
    )

    with _writer_gives_up():
        response = await gateway.call_tool(
            McpToolCallRequest(
                name="browser.create_cron_job", arguments={"name": "n", "goal": "g", "webhook_enabled": True}
            )
        )

    assert response.isError
    assert response.content[0].text == cron_service_module.CRON_STORE_UNWRITABLE
    assert store_path.read_bytes() == before
