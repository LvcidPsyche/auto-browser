"""browser.get_local_storage reads the page's storage in the controller's isolated world, and its answer is bounded.

It ran in the page's own JavaScript world, so a page that replaced Storage.prototype.getItem answered for its storage
with what it chose: 20 million characters in a probe, all of them returned. A page's storage can also hold more than a
tool answer may carry with no override at all (Chromium keeps megabytes in each store, and a key can be as long as a
value), so the answer is bounded too: keys, values and each entry's overhead together, to MAX_TOOL_RESULT_CHARS, with
truncated: true when anything was left out. Real Chromium; the gateway's own handler, on a session the real
BrowserManager opened.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models import McpToolCallRequest
from app.tool_gateway import McpToolGateway
from app.tool_gateway.gateway import STORAGE_ENTRY_OVERHEAD_CHARS
from app.tool_inputs import MAX_TOOL_RESULT_CHARS, GetStorageInput
from tests._chromium import requires_chromium
from tests.test_allowed_hosts_navigation import (  # noqa: F401 - _shared_chromium is the module's Chromium fixture
    _shared_chromium,
    browser_env,
)

pytestmark = [requires_chromium, pytest.mark.asyncio(loop_scope="module")]

PAST_THE_BOUND = MAX_TOOL_RESULT_CHARS + 10
# Small entries whose keys and values alone fit the bound, and do not with each entry's overhead counted.
MANY_ENTRIES = 150_000
MANY_KEY_LENGTH = 6
assert MANY_ENTRIES * MANY_KEY_LENGTH < MAX_TOOL_RESULT_CHARS < MANY_ENTRIES * (MANY_KEY_LENGTH + 1)

# A page that answers for its storage in its own world: getItem gives a value past the bound whatever is asked, and
# window.sessionStorage is a stand-in that does the same. What each store really holds is one short entry.
HOSTILE = (
    "<title>hostile</title><script>"
    "localStorage.setItem('a', '1'); sessionStorage.setItem('a', '1');"
    f"const big = 'x'.repeat({PAST_THE_BOUND});"
    "Storage.prototype.getItem = function () { return big; };"
    "Object.defineProperty(window, 'sessionStorage', { get() { return { a: big, getItem: () => big }; } });"
    "</script>"
)
# Native entries, no override: what isolation alone does not bound.
BIG_VALUE = (
    "<title>big value</title><script>"
    f"localStorage.setItem('big', 'v'.repeat({PAST_THE_BOUND}));"
    f"sessionStorage.setItem('big', 'v'.repeat({PAST_THE_BOUND}));"
    "</script>"
)
BIG_KEY = f"<title>big key</title><script>localStorage.setItem('k'.repeat({PAST_THE_BOUND}), '1');</script>"
MANY = (
    "<title>many</title><script>"
    f"for (let i = 0; i < {MANY_ENTRIES}; i++) "
    f"localStorage.setItem(String(i).padStart({MANY_KEY_LENGTH}, '0'), '');"
    "</script>"
)


def _gateway(manager) -> McpToolGateway:
    manager.ensure_session_accessible = AsyncMock(return_value=None)
    # The storage pack is outside the default profile.
    return McpToolGateway(
        manager=manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace(), tool_profile="full"
    )


def _answer_chars(entries: dict[str, str | None]) -> int:
    """What the bound counts: each entry's key, value and overhead."""
    return sum(len(key) + len(value or "") + STORAGE_ENTRY_OVERHEAD_CHARS for key, value in entries.items())


async def _read(tmp_path: Path, page: str, storage_type: str, key: str | None = None) -> dict:
    async with browser_env(tmp_path, {"/page": page}) as env:
        session_id = await env.create("/page")
        return await _gateway(env.manager)._get_local_storage(
            GetStorageInput(session_id=session_id, storage_type=storage_type, key=key)
        )


@pytest.mark.parametrize("storage_type", ["local", "session"])
async def test_a_page_that_answers_for_its_storage_in_its_own_world_is_not_heard(
    tmp_path: Path, storage_type: str
) -> None:
    one = await _read(tmp_path, HOSTILE, storage_type, key="a")
    every = await _read(tmp_path, HOSTILE, storage_type)

    assert one["value"] == "1", f"{len(one['value'])} characters"
    assert every["storage"] == {"a": "1"}, f"{_answer_chars(every['storage'])} characters"
    assert one["truncated"] is False and every["truncated"] is False


@pytest.mark.parametrize("storage_type", ["local", "session"])
async def test_a_value_past_the_bound_is_cut_to_it_and_said_to_be(tmp_path: Path, storage_type: str) -> None:
    one = await _read(tmp_path, BIG_VALUE, storage_type, key="big")
    every = await _read(tmp_path, BIG_VALUE, storage_type)

    assert _answer_chars({"big": one["value"]}) == MAX_TOOL_RESULT_CHARS and one["truncated"] is True
    assert one["value"] == "v" * len(one["value"])
    assert _answer_chars(every["storage"]) == MAX_TOOL_RESULT_CHARS and every["truncated"] is True
    assert every["storage"]["big"] == one["value"]


async def test_a_key_past_the_bound_is_left_out_and_said_to_be(tmp_path: Path) -> None:
    every = await _read(tmp_path, BIG_KEY, "local")

    assert _answer_chars(every["storage"]) <= MAX_TOOL_RESULT_CHARS and every["truncated"] is True


async def test_many_small_entries_count_their_overhead_against_the_bound(tmp_path: Path) -> None:
    every = await _read(tmp_path, MANY, "local")

    assert _answer_chars(every["storage"]) <= MAX_TOOL_RESULT_CHARS, f"{len(every['storage'])} entries"
    assert len(every["storage"]) < MANY_ENTRIES and every["truncated"] is True


async def test_a_storage_the_document_may_not_use_is_reported_as_the_browser_says(tmp_path: Path) -> None:
    """about:blank has no origin to keep storage for. The isolated world reports a script that threw with a hint
    about elements ("Observe again before acting on this element."); the read reports the browser's own reason."""
    async with browser_env(tmp_path, {}) as env:
        session_id = (await env.manager.create_session(name="t"))["id"]

        response = await _gateway(env.manager)._call_tool(
            McpToolCallRequest(name="browser.get_local_storage", arguments={"session_id": session_id})
        )

    assert response.isError and response.structuredContent["code"] == "browser_error", response
    assert "SecurityError" in response.structuredContent["error"], response
    assert "Observe again" not in response.content[0].text
