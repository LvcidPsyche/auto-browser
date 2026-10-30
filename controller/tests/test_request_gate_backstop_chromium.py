"""What the pre-request gate cannot stop is still refused after the fact, with the gate on (decision 8).

Chromium sends no request the gate sees for a `blob:` navigation (step 0, m4), so a refused iframe that builds a
blob document and opens it, or sends the top page to it, commits a document of the refused origin. NAV-07 (the
guard on each commit) must still refuse it, with the gate attached. The other after-the-fact paths are in
tests/test_request_gate_hardening_chromium.py (a same-site worker's popup) and
tests/test_request_gate_attach_chromium.py (prefetch and bfcache in an attach mode).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.test_request_gate_chromium import (  # noqa: F401 - the module's shared Chromium
    REFUSED,
    _shared_chromium,
    eventually,
    gated,
    pytestmark,
)

AUDIT_WAIT_SECONDS = 5.0
FRAME = """<!doctype html><title>blob frame</title><script>
function mk() { return URL.createObjectURL(new Blob(['<!doctype html><title>BLOB</title>BLOB'], {type: 'text/html'})); }
</script>
<button id="open" onclick="window.open(mk())">open</button>
<button id="top" onclick="top.location = mk()">top</button>"""


async def caught_after_the_fact(env, session_id: str) -> list[dict]:
    """The NAV-07 refusals audited for the session (source "page"), waiting for the guard's task."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + AUDIT_WAIT_SECONDS
    while loop.time() < deadline:
        events = await env.manager.list_audit_events(session_id=session_id, event_type="navigation_blocked")
        refused = [event["details"] for event in events if event["details"].get("source") == "page"]
        if refused:
            return refused
        await asyncio.sleep(0.05)
    return []


async def _frame(env, session_id: str):
    page = env.manager.sessions[session_id].page
    await eventually(lambda: any(frame.url.endswith("/blob-frame") for frame in page.frames))
    return next(frame for frame in page.frames if frame.url.endswith("/blob-frame"))


@pytest.mark.parametrize("button", ["#open", "#top"], ids=["window_open", "top_location"])
async def test_a_refused_iframes_blob_document_is_refused_after_it_commits(tmp_path: Path, button: str) -> None:
    async with gated(tmp_path) as env:
        env.server.pages["/blob-frame"] = FRAME
        env.server.pages["/blob-top"] = f'<iframe src="{env.server.url(REFUSED, "/blob-frame")}"></iframe>'
        session_id = await env.create("/blob-top")
        session = env.manager.sessions[session_id]
        frame = await _frame(env, session_id)

        await frame.click(button)

        refused = await caught_after_the_fact(env, session_id)
        assert refused == [{"url": env.server.url(REFUSED, ""), "source": "page"}]  # the blob's inner origin
        await eventually(lambda: not any(page.url.startswith("blob:") for page in session.context.pages))
        assert env.gate().blocked_count(session.gate_context_id) == 0, "the gate saw it: this path is not a gap"
