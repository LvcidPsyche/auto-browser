"""A cron store that cannot be used reaches callers as one fixed, path-free text, and only a corrupt one is moved.

`_load` raised RuntimeError(f"cron store {path} is unreadable: {exc}"), which MCP callers received with the path. It
also quarantined on any error, so a passing read error (a sharing violation, a brief permission fault) moved a good,
hand-edited store aside, and a store holding JSON other than an object loaded, to fail later in its callers.
"""

from __future__ import annotations

import errno
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.cron_service import CRON_STORE_UNREADABLE, CronService
from app.models import McpToolCallRequest
from app.tool_gateway import McpToolGateway


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    path = tmp_path / "secret-dir" / "crons.json"
    path.parent.mkdir()
    return path


def _quarantined(store_path: Path) -> list[Path]:
    return sorted(store_path.parent.glob("crons.corrupt-*.json"))


def _assert_fixed(text: str, store_path: Path) -> None:
    assert "CRON_STORE_PATH" in CRON_STORE_UNREADABLE
    assert CRON_STORE_UNREADABLE in text
    assert str(store_path.parent) not in text


@pytest.mark.parametrize(
    "content",
    [b"{bad json", b"\xff\xfe not utf-8", b"[]", b'"a string"', b"null"],
    ids=["bad-json", "not-utf-8", "a-list", "a-string", "null"],
)
def test_a_corrupt_store_is_set_aside_and_refused_with_the_fixed_text(store_path: Path, content: bytes, caplog):
    store_path.write_bytes(content)

    with caplog.at_level(logging.ERROR, logger="app.cron_service"), pytest.raises(RuntimeError) as caught:
        CronService(store_path)._load()

    _assert_fixed(str(caught.value), store_path)
    assert not store_path.exists()
    [moved] = _quarantined(store_path)
    assert moved.read_bytes() == content
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert str(store_path) in logged and str(moved) in logged


def test_a_store_that_cannot_be_read_is_refused_and_left_where_it_is(store_path: Path, caplog):
    # As a folder without search permission answers: stat and open both fail with EACCES. Patched, since CI runs
    # as root, which a real permission would not stop.
    store_path.write_text('{"j1": {"id": "j1"}}', encoding="utf-8")
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
        with caplog.at_level(logging.ERROR, logger="app.cron_service"), pytest.raises(RuntimeError) as caught:
            CronService(store_path)._load()

    _assert_fixed(str(caught.value), store_path)
    assert _quarantined(store_path) == []
    assert store_path.read_text(encoding="utf-8") == '{"j1": {"id": "j1"}}'
    assert any(str(store_path) in record.getMessage() for record in caplog.records)


def test_a_missing_store_and_folder_read_as_empty(tmp_path: Path):
    assert CronService(tmp_path / "missing" / "crons.json")._load() == {}


async def test_mcp_gets_the_fixed_text(store_path: Path):
    store_path.write_text("{bad json", encoding="utf-8")
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

    response = await gateway.call_tool(McpToolCallRequest(name="browser.list_cron_jobs", arguments={}))

    assert response.isError
    _assert_fixed(response.content[0].text, store_path)
