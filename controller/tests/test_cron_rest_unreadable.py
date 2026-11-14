"""An unreadable cron store answers the REST cron routes 503 with the fixed, path-free text (it was a plain 500)."""

from __future__ import annotations

import copy
import errno
import pickle
from pathlib import Path
from unittest.mock import Mock, patch

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
    pytest.param("post", "/crons/abc/webhook-key/rotate", None, id="rotate"),
    pytest.param("delete", "/crons/abc/webhook-key", None, id="revoke"),
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


def test_the_error_survives_copy_and_pickle():
    original = CronStoreUnreadableError()
    pickled = pickle.loads(pickle.dumps(original))  # noqa: S301 - bytes this test just made
    for clone in (copy.copy(original), copy.deepcopy(original), pickled):
        assert type(clone) is CronStoreUnreadableError
        assert str(clone) == str(original)


@pytest.fixture(params=["bad-json", "a-list", "permission-denied"])
def unusable_store(request, tmp_path: Path):
    """A store each raise site of _load refuses: corrupt, JSON that is not an object, and a read the OS denies."""
    store_path = tmp_path / "secret-dir" / "crons.json"
    store_path.parent.mkdir()
    if request.param == "permission-denied":
        # Patched, since CI runs as root, which a real permission would not stop.
        store_path.write_text("{}", encoding="utf-8")
        denied = PermissionError(errno.EACCES, "Permission denied", str(store_path))
        real_stat, real_read_text = Path.stat, Path.read_text

        def stat(self, *args, **kwargs):
            if self == store_path:
                raise denied
            return real_stat(self, *args, **kwargs)

        def read_text(self, *args, **kwargs):
            if self == store_path:
                raise denied
            return real_read_text(self, *args, **kwargs)

        with patch.object(Path, "stat", stat), patch.object(Path, "read_text", read_text):
            yield store_path
    else:
        store_path.write_text("{bad json" if request.param == "bad-json" else "[]", encoding="utf-8")
        yield store_path


@pytest.mark.parametrize(("method", "url", "body"), ROUTES)
def test_each_cron_route_answers_503_for_an_unreadable_store(unusable_store: Path, method: str, url: str, body):
    response = _client(unusable_store).request(method, url, json=body)

    assert response.status_code == 503
    assert response.json() == {"detail": CRON_STORE_UNREADABLE}
    assert str(unusable_store.parent.parent) not in response.text
    assert "secret-dir" not in response.text
