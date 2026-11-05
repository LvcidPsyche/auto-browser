"""An unreadable cron store answers the REST cron routes 503 with the fixed, path-free text (it was a plain 500)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.cron_service import CRON_STORE_UNREADABLE, CronService, CronStoreUnreadableError
from app.routes.operations import create_operations_router

CREATE_BODY = {"name": "nightly", "goal": "check the page", "webhook_enabled": True}
ROUTES = [
    pytest.param("get", "/crons", None, id="list"),
    pytest.param("post", "/crons", CREATE_BODY, id="create"),
    pytest.param("get", "/crons/abc", None, id="get"),
    pytest.param("delete", "/crons/abc", None, id="delete"),
    pytest.param("post", "/crons/abc/trigger", {"webhook_key": "k"}, id="trigger"),
]


def _client(store_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_operations_router(manager=Mock(), proxy_store=Mock(), cron_service=CronService(store_path))
    )
    return TestClient(app, raise_server_exceptions=False)


def test_the_error_is_a_runtime_error_with_the_fixed_text():
    assert issubclass(CronStoreUnreadableError, RuntimeError)
    assert str(CronStoreUnreadableError()) == CRON_STORE_UNREADABLE


@pytest.mark.parametrize(("method", "url", "body"), ROUTES)
def test_each_cron_route_answers_503_for_an_unreadable_store(tmp_path: Path, method: str, url: str, body):
    store_path = tmp_path / "secret-dir" / "crons.json"
    store_path.parent.mkdir()
    store_path.write_text("{bad json", encoding="utf-8")

    response = _client(store_path).request(method, url, json=body)

    assert response.status_code == 503
    assert response.json() == {"detail": CRON_STORE_UNREADABLE}
    assert str(tmp_path) not in response.text
    assert "secret-dir" not in response.text
