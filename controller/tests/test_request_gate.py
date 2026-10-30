"""The pre-request gate (app/browser/request_gate.py), driven through a fake CDP session.

The gate pauses every Document request in the browser and fails a page's main-frame request in a
context the controller owns when ALLOWED_HOSTS refuses its URL, before Chromium sends it. These tests
cover each branch of that decision: allowed and refused URLs, pages, iframes and frames the gate does not
know, redirect hops, the cross-origin POST hop rule of attach modes, a handler that raises, and the gate's
session detaching. tests/test_request_gate_chromium.py checks the same rules against real Chromium.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlsplit

import pytest

from app.browser import request_gate
from app.browser.request_gate import (
    BLOCKED_ERROR_REASON,
    BLOCKS_KEPT,
    FETCH_PATTERNS,
    REDIRECT_ORIGIN_SETTING,
    REDIRECT_ORIGIN_SWITCH,
    RequestGate,
    RequestGateUnavailable,
)
from tests._fake_cdp import FakeCDPSession, new_context_id, page_session_for

pytestmark = pytest.mark.asyncio

ALLOWED = "https://allowed.example"
REFUSED = "https://refused.example"
NO_TARGET = Exception("Protocol error (Target.getTargetInfo): No target with given id found")


def refused(url: str) -> bool:
    return (urlsplit(url).hostname or "").startswith("refused")


class Harness:
    """A gate on a fake browser session, with one owned context whose first page is a known target."""

    def __init__(self, **options) -> None:
        self.cdp = FakeCDPSession()
        self.browser = SimpleNamespace(new_browser_cdp_session=AsyncMock(return_value=self.cdp))
        self.lost: list[RequestGate] = []
        self.gate = RequestGate(
            self.browser,
            url_refused=options.pop("url_refused", refused),
            attach_mode=options.pop("attach_mode", False),
            origin_switch_declared=options.pop("origin_switch_declared", False),
            on_lost=self.lost.append,
            on_block=options.pop("on_block", None),
        )
        self.requests = 0

    async def start(self) -> None:
        await self.gate.attach()
        self.owned, self.page = await self.owned_context()

    async def owned_context(self) -> tuple[str, str]:
        context_id, target_id = new_context_id(), f"page-{self.requests}-{id(self)}"
        context = SimpleNamespace(
            new_cdp_session=AsyncMock(return_value=page_session_for(context_id, target_id)), on=Mock()
        )
        self.target(target_id, "page", context_id)
        assert await self.gate.register_context(context, object()) == context_id
        return context_id, target_id

    def target(self, target_id: str, kind: str, context_id: str) -> None:
        self.cdp.emit(
            "Target.targetCreated",
            {"targetInfo": {"targetId": target_id, "type": kind, "browserContextId": context_id, "url": ""}},
        )

    async def pause(
        self,
        url: str,
        *,
        frame: str | None = None,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        redirected_from: str | None = None,
    ) -> tuple[str, dict]:
        self.requests += 1
        request_id = f"interception-job-{self.requests}.0"
        event = {
            "requestId": request_id,
            "request": {"url": url, "method": method, "headers": headers or {}},
            "frameId": frame or self.page,
            "resourceType": "Document",
            "networkId": str(self.requests),
        }
        if redirected_from is not None:
            event["redirectedRequestId"] = redirected_from
        self.cdp.emit("Fetch.requestPaused", event)
        method_sent, params = await self.cdp.wait_for_reply(request_id)
        return method_sent, {**params, "requestId": request_id}

    def lookups(self) -> list[dict]:
        return [params for method, params in self.cdp.sent if method == "Target.getTargetInfo"]


@pytest.fixture
async def harness() -> Harness:
    h = Harness()
    await h.start()
    return h


# ── attaching ────────────────────────────────────────────────────────────────


async def test_attach_discovers_targets_before_it_pauses_documents() -> None:
    h = Harness()
    await h.gate.attach()

    assert [method for method, _ in h.cdp.sent] == ["Target.setDiscoverTargets", "Fetch.enable"]
    assert h.cdp.sent[0][1] == {"discover": True}
    assert h.cdp.sent[1][1] == {"patterns": FETCH_PATTERNS}
    assert FETCH_PATTERNS == [{"urlPattern": "*", "resourceType": "Document", "requestStage": "Request"}]
    assert h.gate.live


async def test_a_gate_that_cannot_enable_fetch_is_not_live_and_says_so() -> None:
    h = Harness()
    h.cdp.replies["Fetch.enable"] = RuntimeError("Fetch.enable: not allowed")

    with pytest.raises(RequestGateUnavailable, match="not allowed"):
        await h.gate.attach()

    assert not h.gate.live
    assert h.cdp.detached
    assert h.lost == [], "a gate that never attached was reported lost"


# ── the decision ─────────────────────────────────────────────────────────────


async def test_a_refused_main_frame_request_of_an_owned_page_is_failed(harness: Harness) -> None:
    method, params = await harness.pause(f"{REFUSED}/secret?q=1")

    assert method == "Fetch.failRequest"
    assert params["errorReason"] == BLOCKED_ERROR_REASON == "BlockedByClient"
    [block] = harness.gate.blocks(harness.owned)
    assert (block.target_id, block.origin, block.reason) == (harness.page, REFUSED, "off_list")
    assert harness.gate.blocked_count(harness.owned) == 1


async def test_an_allowed_main_frame_request_continues(harness: Harness) -> None:
    method, _ = await harness.pause(f"{ALLOWED}/page")

    assert method == "Fetch.continueRequest"
    assert harness.gate.blocks(harness.owned) == []


async def test_a_known_page_target_is_judged_without_a_lookup(harness: Harness) -> None:
    await harness.pause(f"{REFUSED}/")

    assert harness.lookups() == []


async def test_a_page_in_a_context_the_controller_did_not_create_continues(harness: Harness) -> None:
    """The operator's own tabs under CDP_CONNECT_URL, and every context other code made."""
    harness.target("human-tab", "page", "default-context")

    method, _ = await harness.pause(f"{REFUSED}/", frame="human-tab")

    assert method == "Fetch.continueRequest"


async def test_an_unknown_frame_that_resolves_to_an_owned_page_is_failed(harness: Harness) -> None:
    """A popup whose targetCreated has not been delivered yet: the lookup finds the page."""
    harness.cdp.replies["Target.getTargetInfo"] = lambda params: {
        "targetInfo": {"targetId": params["targetId"], "type": "page", "browserContextId": harness.owned}
    }

    method, _ = await harness.pause(f"{REFUSED}/", frame="popup-not-yet-announced")

    assert method == "Fetch.failRequest"
    assert harness.lookups() == [{"targetId": "popup-not-yet-announced"}]


async def test_an_unknown_frame_that_resolves_to_an_unowned_page_continues(harness: Harness) -> None:
    harness.cdp.replies["Target.getTargetInfo"] = lambda params: {
        "targetInfo": {"targetId": params["targetId"], "type": "page", "browserContextId": "someone-else"}
    }

    method, _ = await harness.pause(f"{REFUSED}/", frame="their-popup")

    assert method == "Fetch.continueRequest"


async def test_an_out_of_process_iframe_of_a_refused_host_continues(harness: Harness) -> None:
    """Iframes of other hosts stay allowed (decision 4)."""
    harness.target("oopif-1", "iframe", harness.owned)
    harness.cdp.replies["Target.getTargetInfo"] = {
        "targetInfo": {"targetId": "oopif-1", "type": "iframe", "browserContextId": harness.owned}
    }

    method, _ = await harness.pause(f"{REFUSED}/embed", frame="oopif-1")

    assert method == "Fetch.continueRequest"
    assert harness.lookups() == [{"targetId": "oopif-1"}], "an iframe is looked up, never assumed"


async def test_an_in_process_iframe_continues(harness: Harness) -> None:
    """An in-process iframe is no target at all: Chromium answers "No target with given id"."""
    harness.cdp.replies["Target.getTargetInfo"] = NO_TARGET

    method, _ = await harness.pause(f"{REFUSED}/embed", frame="same-process-frame")

    assert method == "Fetch.continueRequest"


async def test_a_failed_lookup_fails_a_refused_request(harness: Harness) -> None:
    """Any other lookup error cannot rule out an owned page (decision 7)."""
    harness.cdp.replies["Target.getTargetInfo"] = RuntimeError("Target closed")

    method, _ = await harness.pause(f"{REFUSED}/", frame="unknown-frame")

    assert method == "Fetch.failRequest"
    assert harness.gate.unattributed_blocks == 1


async def test_a_failed_lookup_still_continues_an_allowed_request(harness: Harness) -> None:
    harness.cdp.replies["Target.getTargetInfo"] = RuntimeError("Target closed")

    method, _ = await harness.pause(f"{ALLOWED}/", frame="unknown-frame")

    assert method == "Fetch.continueRequest"


async def test_a_request_with_no_frame_is_judged_as_a_possible_page(harness: Harness) -> None:
    harness.requests += 1
    harness.cdp.emit(
        "Fetch.requestPaused",
        {"requestId": "no-frame", "request": {"url": f"{REFUSED}/", "method": "GET", "headers": {}}},
    )

    method, _ = await harness.cdp.wait_for_reply("no-frame")

    assert method == "Fetch.failRequest"


async def test_a_target_of_another_type_in_an_owned_context_is_judged_like_a_page(harness: Harness) -> None:
    harness.cdp.replies["Target.getTargetInfo"] = {
        "targetInfo": {"targetId": "odd", "type": "other", "browserContextId": harness.owned}
    }

    method, _ = await harness.pause(f"{REFUSED}/", frame="odd")

    assert method == "Fetch.failRequest"


async def test_a_destroyed_target_is_looked_up_again(harness: Harness) -> None:
    harness.cdp.emit("Target.targetDestroyed", {"targetId": harness.page})
    harness.cdp.replies["Target.getTargetInfo"] = NO_TARGET

    method, _ = await harness.pause(f"{REFUSED}/")

    assert method == "Fetch.continueRequest"
    assert harness.lookups() == [{"targetId": harness.page}]


# ── redirect hops ────────────────────────────────────────────────────────────


async def test_every_redirect_hop_is_judged_on_its_own(harness: Harness) -> None:
    first, params = await harness.pause(f"{ALLOWED}/r/302")
    hop, _ = await harness.pause(f"{REFUSED}/landing", redirected_from=params["requestId"])

    assert (first, hop) == ("Fetch.continueRequest", "Fetch.failRequest")


async def test_an_allowed_hop_continues(harness: Harness) -> None:
    _, params = await harness.pause(f"{ALLOWED}/r/302")
    hop, _ = await harness.pause(f"{ALLOWED}/landing", redirected_from=params["requestId"])

    assert hop == "Fetch.continueRequest"


# ── cross-origin POST hops in attach modes (decision 10) ─────────────────────

ORIGIN = {"Origin": ALLOWED, "Content-Type": "application/x-www-form-urlencoded"}
OTHER_ALLOWED = "https://www.allowed.example"


async def _post_hop(h: Harness, *, to: str, method: str = "POST", headers: dict | None = None) -> tuple[str, dict]:
    _, params = await h.pause(f"{ALLOWED}/r/307", method="POST", headers=ORIGIN)
    return await h.pause(
        to, method=method, headers=ORIGIN if headers is None else headers, redirected_from=params["requestId"]
    )


async def _attach_mode(**options) -> Harness:
    h = Harness(attach_mode=True, **options)
    await h.start()
    return h


async def test_attach_mode_fails_a_cross_origin_post_hop_and_names_the_fix() -> None:
    h = await _attach_mode()

    method, _ = await _post_hop(h, to=f"{OTHER_ALLOWED}/target")

    assert method == "Fetch.failRequest"
    [block] = h.gate.blocks(h.owned)
    assert (block.origin, block.reason) == (OTHER_ALLOWED, "cross_origin_post_redirect")
    assert REDIRECT_ORIGIN_SWITCH in block.explanation and REDIRECT_ORIGIN_SETTING in block.explanation


async def test_attach_mode_continues_a_same_origin_post_hop() -> None:
    h = await _attach_mode()

    method, _ = await _post_hop(h, to=f"{ALLOWED}/target")

    assert method == "Fetch.continueRequest"


async def test_attach_mode_continues_a_cross_origin_get_hop() -> None:
    h = await _attach_mode()

    method, _ = await _post_hop(h, to=f"{OTHER_ALLOWED}/target", method="GET")

    assert method == "Fetch.continueRequest"


async def test_attach_mode_continues_a_post_hop_without_an_origin_header() -> None:
    h = await _attach_mode()

    method, _ = await _post_hop(h, to=f"{OTHER_ALLOWED}/target", headers={"Content-Type": "text/plain"})

    assert method == "Fetch.continueRequest"


async def test_attach_mode_fails_a_post_hop_whose_previous_url_it_never_saw() -> None:
    h = await _attach_mode()

    method, _ = await h.pause(f"{ALLOWED}/target", method="POST", headers=ORIGIN, redirected_from="unseen")

    assert method == "Fetch.failRequest"


async def test_a_declared_switch_lets_the_cross_origin_post_hop_continue() -> None:
    h = await _attach_mode(origin_switch_declared=True)

    method, _ = await _post_hop(h, to=f"{OTHER_ALLOWED}/target")

    assert method == "Fetch.continueRequest"


async def test_a_launched_browser_continues_the_cross_origin_post_hop(harness: Harness) -> None:
    """Playwright launches its browsers with the switch already off."""
    method, _ = await _post_hop(harness, to=f"{OTHER_ALLOWED}/target")

    assert method == "Fetch.continueRequest"


async def test_attach_mode_leaves_the_post_hops_of_unowned_pages_alone() -> None:
    h = await _attach_mode()
    h.target("human-tab", "page", "default-context")

    _, params = await h.pause(f"{ALLOWED}/r/307", frame="human-tab", method="POST", headers=ORIGIN)
    method, _ = await h.pause(
        f"{OTHER_ALLOWED}/target", frame="human-tab", method="POST", headers=ORIGIN, redirected_from=params["requestId"]
    )

    assert method == "Fetch.continueRequest"


# ── a handler that raises ────────────────────────────────────────────────────


async def test_a_handler_error_fails_a_refused_request(harness: Harness) -> None:
    harness.cdp.replies["Target.getTargetInfo"] = {"unexpected": "shape"}  # KeyError in the handler

    method, _ = await harness.pause(f"{REFUSED}/", frame="unknown-frame")

    assert method == "Fetch.failRequest"
    assert harness.gate.handler_errors == 1


async def test_a_handler_error_continues_an_allowed_request(harness: Harness) -> None:
    harness.cdp.replies["Target.getTargetInfo"] = {"unexpected": "shape"}

    method, _ = await harness.pause(f"{ALLOWED}/", frame="unknown-frame")

    assert method == "Fetch.continueRequest"


async def test_a_matcher_that_raises_fails_the_request() -> None:
    def broken(url: str) -> bool:
        raise ValueError("unparseable")

    h = Harness(url_refused=broken)
    await h.start()

    method, _ = await h.pause("https://[::1/")

    assert method == "Fetch.failRequest"


async def test_a_reply_that_fails_is_logged_not_raised(harness: Harness, caplog) -> None:
    harness.cdp.replies["Fetch.failRequest"] = RuntimeError("Invalid InterceptionId.")

    method, _ = await harness.pause(f"{REFUSED}/")

    assert method == "Fetch.failRequest"
    assert harness.gate.blocks(harness.owned), "the block is recorded even when Chromium had dropped the request"
    [record] = [r for r in caplog.records if r.name == "app.browser.request_gate"]
    assert record.levelname == "WARNING" and "Invalid InterceptionId." in record.getMessage()


# ── ownership ────────────────────────────────────────────────────────────────


async def test_register_context_reads_the_first_pages_context_id() -> None:
    h = Harness()
    await h.gate.attach()
    session = page_session_for("ctx-from-page", "first-page")
    context = SimpleNamespace(new_cdp_session=AsyncMock(return_value=session), on=Mock())
    page = object()

    context_id = await h.gate.register_context(context, page)

    assert context_id == "ctx-from-page"
    context.new_cdp_session.assert_awaited_once_with(page)
    assert [method for method, _ in session.sent] == ["Target.getTargetInfo"]
    assert session.detached
    assert h.gate.owns("ctx-from-page")


async def test_a_context_whose_page_cannot_be_read_is_refused() -> None:
    h = Harness()
    await h.gate.attach()
    session = FakeCDPSession({"Target.getTargetInfo": RuntimeError("Target closed")})
    context = SimpleNamespace(new_cdp_session=AsyncMock(return_value=session), on=Mock())

    with pytest.raises(RequestGateUnavailable, match="Target closed"):
        await h.gate.register_context(context, object())


async def test_a_forgotten_context_is_no_longer_owned(harness: Harness) -> None:
    context_id, target_id = await harness.owned_context()
    await harness.pause(f"{REFUSED}/", frame=target_id)
    assert harness.gate.blocked_count(context_id) == 1

    harness.gate.forget_context(context_id)

    assert not harness.gate.owns(context_id)
    assert harness.gate.blocks(context_id) == []
    assert harness.gate.blocked_count(context_id) == 0


# ── detaching ────────────────────────────────────────────────────────────────


async def test_a_gate_session_that_detaches_on_its_own_is_reported_lost(harness: Harness) -> None:
    harness.cdp.emit("close", harness.cdp)

    assert not harness.gate.live
    assert harness.lost == [harness.gate]


async def test_a_deliberate_detach_is_not_a_loss(harness: Harness) -> None:
    await harness.gate.detach()

    assert harness.cdp.detached
    assert not harness.gate.live
    assert harness.lost == []


async def test_a_lost_gate_registers_no_more_contexts(harness: Harness) -> None:
    harness.cdp.emit("close", harness.cdp)
    context = SimpleNamespace(new_cdp_session=AsyncMock(return_value=page_session_for("late")), on=Mock())

    with pytest.raises(RequestGateUnavailable):
        await harness.gate.register_context(context, object())


async def test_a_context_registered_while_the_gate_detaches_is_refused() -> None:
    h = Harness()
    await h.gate.attach()

    def detach_midway(params: dict) -> dict:
        h.cdp.emit("close", h.cdp)
        return {"targetInfo": {"targetId": "p", "type": "page", "browserContextId": "c"}}

    context = SimpleNamespace(
        new_cdp_session=AsyncMock(return_value=FakeCDPSession({"Target.getTargetInfo": detach_midway})), on=Mock()
    )

    with pytest.raises(RequestGateUnavailable):
        await h.gate.register_context(context, object())
    assert not h.gate.owns("c")


# ── checkpoint 1 corrections ─────────────────────────────────────────────────


def _lenient(url: str) -> bool:
    """A matcher that never raises, so an unparseable URL reaches the hop rule."""
    return "://refused" in url


async def test_an_unparseable_previous_url_counts_as_a_cross_origin_post_hop() -> None:
    h = await _attach_mode(url_refused=_lenient)
    _, params = await h.pause("https://[::1/form", method="POST", headers=ORIGIN)

    method, _ = await h.pause(
        f"{OTHER_ALLOWED}/target", method="POST", headers=ORIGIN, redirected_from=params["requestId"]
    )

    assert method == "Fetch.failRequest"
    [block] = h.gate.blocks(h.owned)
    assert block.reason == "cross_origin_post_redirect"
    assert h.gate.handler_errors == 0, "the hop rule raised instead of judging the hop"


async def test_an_unparseable_hop_url_counts_as_a_cross_origin_post_hop() -> None:
    h = await _attach_mode(url_refused=_lenient)
    _, params = await h.pause(f"{ALLOWED}/r/307", method="POST", headers=ORIGIN)

    method, _ = await h.pause("https://[::1/target", method="POST", headers=ORIGIN, redirected_from=params["requestId"])

    assert method == "Fetch.failRequest"
    assert [block.reason for block in h.gate.blocks(h.owned)] == ["cross_origin_post_redirect"]
    assert h.gate.handler_errors == 0


async def test_an_attach_mode_gate_without_the_switch_warns_once(caplog) -> None:
    h = Harness(attach_mode=True)

    with caplog.at_level("WARNING", logger="app.browser.request_gate"):
        await h.gate.attach()

    [record] = [r for r in caplog.records if r.name == "app.browser.request_gate"]
    assert record.levelname == "WARNING"
    text = record.getMessage()
    assert REDIRECT_ORIGIN_SWITCH in text and REDIRECT_ORIGIN_SETTING in text
    assert "operator's own tabs" in text


@pytest.mark.parametrize(("attach_mode", "declared"), [(False, False), (True, True)])
async def test_no_switch_warning_when_it_does_not_apply(caplog, attach_mode: bool, declared: bool) -> None:
    h = Harness(attach_mode=attach_mode, origin_switch_declared=declared)

    with caplog.at_level("WARNING", logger="app.browser.request_gate"):
        await h.gate.attach()

    assert [r for r in caplog.records if r.name == "app.browser.request_gate"] == []


async def test_the_block_is_on_record_at_the_reply_and_audited_and_logged_only_after_it(caplog) -> None:
    """R6: whoever reads after Playwright reports the failure finds the block; the audit and logs wait for the reply."""
    audited: list = []
    h = Harness(on_block=audited.append)
    await h.start()
    seen_at_reply: list = []

    unknown = "unknown-frame"

    def fail_request(params: dict) -> dict:
        seen_at_reply.append(
            (
                h.gate.page_blocked_count(unknown),
                h.gate.page_blocked_count(h.page),
                h.gate.blocked_count(h.owned),
                len(audited),
                caplog.text,
            )
        )
        return {}

    h.cdp.replies["Fetch.failRequest"] = fail_request
    h.cdp.replies["Target.getTargetInfo"] = RuntimeError("Target.getTargetInfo: boom")

    with caplog.at_level("WARNING", "app.browser.request_gate"):
        await h.pause(f"{REFUSED}/", frame=unknown)
        await asyncio.sleep(0)

        [(unknown_count, _page_count, _context_count, audits, logged)] = seen_at_reply
        assert unknown_count == 1, "the unknown frame's block was not on record when Chromium got its answer"
        assert audits == 0, "the audit of this block ran before the reply"
        assert "could not classify" not in logged, "logged before the reply"
        assert "could not classify" in caplog.text, "never logged"
        seen_at_reply.clear()
        del h.cdp.replies["Target.getTargetInfo"]
        await h.pause(f"{REFUSED}/2")

    [(_unknown_count, page_count, context_count, audits, _logged)] = seen_at_reply
    assert (page_count, context_count) == (1, 1), "the block was not on record when Chromium got its answer"
    assert audits == 1, "the audit of this block ran before the reply"
    assert len(audited) == 2


async def test_a_block_listener_that_raises_is_logged_and_the_gate_keeps_answering(caplog) -> None:
    def broken(_block) -> None:
        raise RuntimeError("listener broke")

    h = Harness(on_block=broken)
    await h.start()

    with caplog.at_level("ERROR", "app.browser.request_gate"):
        first, _ = await h.pause(f"{REFUSED}/")
        await asyncio.sleep(0)
        second, _ = await h.pause(f"{ALLOWED}/")

    assert (first, second) == ("Fetch.failRequest", "Fetch.continueRequest")
    assert "the block listener failed" in caplog.text


async def test_the_hop_url_map_forgets_its_oldest_entry(monkeypatch) -> None:
    monkeypatch.setattr(request_gate, "REQUEST_URLS_KEPT", 2)
    h = await _attach_mode()
    _, oldest = await h.pause(f"{ALLOWED}/r/307?n=1", method="POST", headers=ORIGIN)
    await h.pause(f"{ALLOWED}/r/307?n=2", method="POST", headers=ORIGIN)
    _, newest = await h.pause(f"{ALLOWED}/r/307?n=3", method="POST", headers=ORIGIN)

    forgotten, _ = await h.pause(f"{ALLOWED}/a", method="POST", headers=ORIGIN, redirected_from=oldest["requestId"])
    kept, _ = await h.pause(f"{ALLOWED}/b", method="POST", headers=ORIGIN, redirected_from=newest["requestId"])

    assert (forgotten, kept) == ("Fetch.failRequest", "Fetch.continueRequest")


async def test_blocks_keep_the_last_fifty_and_the_count_keeps_counting(harness: Harness) -> None:
    for n in range(BLOCKS_KEPT + 5):
        await harness.pause(f"{REFUSED}/{n}")

    blocks = harness.gate.blocks(harness.owned)
    assert len(blocks) == BLOCKS_KEPT == 50
    assert harness.gate.blocked_count(harness.owned) == BLOCKS_KEPT + 5
    assert blocks[0].at <= blocks[-1].at


# ── review findings (X1-X5) ──────────────────────────────────────────────────


class ClosingContext:
    """A context whose "close" handlers run when the test closes it, as Playwright's do."""

    def __init__(self, context_id: str, target_id: str) -> None:
        self.session = page_session_for(context_id, target_id)
        self.handlers: list = []

    async def new_cdp_session(self, _page):
        return self.session

    def on(self, event: str, handler) -> None:
        if event == "close":
            self.handlers.append(handler)

    def closed(self) -> None:
        for handler in self.handlers:
            handler(self)


async def _closing_context(h: Harness) -> tuple[ClosingContext, str, str]:
    context_id, target_id = new_context_id(), f"closing-page-{id(h)}"
    context = ClosingContext(context_id, target_id)
    h.target(target_id, "page", context_id)
    assert await h.gate.register_context(context, object()) == context_id
    return context, context_id, target_id


async def test_a_close_during_attach_leaves_the_gate_dead_and_says_so() -> None:
    """X1: the session closing between setDiscoverTargets and Fetch.enable must not leave a live-looking gate."""
    h = Harness()

    def close_midway(params: dict) -> dict:
        h.cdp.emit("close", h.cdp)
        return {}

    h.cdp.replies["Fetch.enable"] = close_midway

    with pytest.raises(RequestGateUnavailable, match="closed while"):
        await h.gate.attach()

    assert not h.gate.live
    assert h.lost == []


async def test_a_context_stays_owned_until_it_has_closed(harness: Harness) -> None:
    """X2: a page still navigating while its context closes is gated; ownership ends with the close event."""
    context, context_id, target_id = await _closing_context(harness)

    method, _ = await harness.pause(f"{REFUSED}/during-close", frame=target_id)
    assert method == "Fetch.failRequest"
    assert harness.gate.owns(context_id)

    context.closed()

    assert not harness.gate.owns(context_id)


async def test_a_context_whose_close_never_happened_stays_owned(harness: Harness) -> None:
    """X2: a close that raised never fires "close": its refused requests are still failed."""
    _, context_id, target_id = await _closing_context(harness)

    method, _ = await harness.pause(f"{REFUSED}/after-a-failed-close", frame=target_id)

    assert method == "Fetch.failRequest"
    assert harness.gate.owns(context_id)


async def test_a_replacement_gate_keeps_the_lost_gates_contexts() -> None:
    """X3 (c): what survived containment stays gated by the gate attached after the loss."""
    from app.browser.request_gate import RequestGates

    sessions: list[FakeCDPSession] = []

    async def new_session() -> FakeCDPSession:
        sessions.append(FakeCDPSession())
        return sessions[-1]

    browser = SimpleNamespace(new_browser_cdp_session=new_session)
    lost: list = []
    gates = RequestGates(
        url_refused=refused,
        enabled=lambda: True,
        origin_switch_declared=lambda: False,
        default_attach_mode=lambda: False,
        on_lost=lost.append,
    )
    first = await gates.require(browser)
    context = ClosingContext("survivor", "survivor-page")
    await first.register_context(context, object())
    sessions[0].emit("close", sessions[0])

    second = await gates.require(browser)
    second._on_target({"targetInfo": {"targetId": "survivor-page", "type": "page", "browserContextId": "survivor"}})
    sessions[1].emit(
        "Fetch.requestPaused",
        {
            "requestId": "r1",
            "request": {"url": f"{REFUSED}/", "method": "GET", "headers": {}},
            "frameId": "survivor-page",
        },
    )
    method, _ = await sessions[1].wait_for_reply("r1")

    assert second is not first and lost == [first]
    assert second.owns("survivor") and method == "Fetch.failRequest"
    context.closed()
    assert not second.owns("survivor"), "the inherited context outlived its close"


async def test_the_previous_url_is_known_before_the_reply_goes_out() -> None:
    """X4: the next hop's verdict needs the URL even if that hop is judged before this reply returns."""
    h = await _attach_mode()
    seen: list[bool] = []

    def continue_request(params: dict) -> dict:
        seen.append(params["requestId"] in h.gate._request_urls)
        return {}

    h.cdp.replies["Fetch.continueRequest"] = continue_request

    await h.pause(f"{ALLOWED}/r/307", method="POST", headers=ORIGIN)

    assert seen == [True]


async def test_a_lookup_that_never_answers_is_a_failed_lookup(monkeypatch) -> None:
    """X5: the lookup is bounded: a hung Target.getTargetInfo fails a refused URL and continues an allowed one."""
    monkeypatch.setattr(request_gate, "LOOKUP_TIMEOUT_SECONDS", 0.05)
    h = Harness()
    await h.start()

    async def never(*_args) -> dict:
        await asyncio.Event().wait()
        return {}

    original_send = h.cdp.send

    async def send(method: str, params: dict | None = None) -> dict:
        if method == "Target.getTargetInfo":
            return await never()
        return await original_send(method, params)

    h.cdp.send = send  # type: ignore[method-assign]

    refused_reply, _ = await h.pause(f"{REFUSED}/", frame="hung-frame")
    allowed_reply, _ = await h.pause(f"{ALLOWED}/", frame="hung-frame-2")

    assert (refused_reply, allowed_reply) == ("Fetch.failRequest", "Fetch.continueRequest")
    assert h.gate.unattributed_blocks == 1


class StallingPageSession(FakeCDPSession):
    async def send(self, method: str, params: dict | None = None) -> dict:
        await asyncio.Event().wait()
        return {}


async def test_r5_reading_a_pages_target_id_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(request_gate, "TARGET_ID_TIMEOUT_SECONDS", 0.05)
    gates = request_gate.RequestGates(
        url_refused=refused,
        enabled=lambda: True,
        origin_switch_declared=lambda: False,
        default_attach_mode=lambda: False,
        on_lost=lambda _gate: None,
    )
    page = Mock(context=SimpleNamespace(new_cdp_session=AsyncMock(return_value=StallingPageSession())))

    assert await asyncio.wait_for(gates.target_id_of(page), timeout=2.0) is None


# ── round 3: blocks kept per page (R6) ───────────────────────────────────────


async def test_r6_other_pages_blocks_cannot_evict_a_pages_own(harness: Harness) -> None:
    await harness.pause(f"{REFUSED}/mine")
    popup_context = harness.owned
    for n in range(BLOCKS_KEPT + 1):
        popup = f"popup-{n}"
        harness.target(popup, "page", popup_context)
        await harness.pause(f"{REFUSED}/popup", frame=popup)

    [own] = harness.gate.page_blocks(harness.page)
    assert own.origin == REFUSED
    assert harness.gate.page_blocked_count(harness.page) == 1
    assert len(harness.gate.blocks(harness.owned)) == BLOCKS_KEPT, "the context's own history is still bounded"


async def test_r6_a_lookup_error_block_is_kept_by_its_target_and_announced(harness_with_audit) -> None:
    h, audited = harness_with_audit
    h.cdp.replies["Target.getTargetInfo"] = RuntimeError("Target.getTargetInfo: boom")

    method, _ = await h.pause(f"{REFUSED}/x", frame="unclassified-page")

    assert method == "Fetch.failRequest"
    [block] = h.gate.page_blocks("unclassified-page")
    assert (block.context_id, block.target_id, block.reason) == (None, "unclassified-page", "unclassified")
    assert audited == [block]


async def test_r6_a_destroyed_target_forgets_its_blocks(harness: Harness) -> None:
    await harness.pause(f"{REFUSED}/")

    harness.cdp.emit("Target.targetDestroyed", {"targetId": harness.page})

    assert harness.gate.page_blocks(harness.page) == []
    assert harness.gate.page_blocked_count(harness.page) == 0


async def test_r6_the_pages_kept_are_capped(monkeypatch, harness: Harness) -> None:
    monkeypatch.setattr(request_gate, "PAGES_KEPT", 2)
    for n in range(3):
        harness.target(f"tab-{n}", "page", harness.owned)
        await harness.pause(f"{REFUSED}/", frame=f"tab-{n}")

    assert [harness.gate.page_blocked_count(f"tab-{n}") for n in range(3)] == [0, 1, 1]


async def test_r6_a_replacement_gate_keeps_the_pages_blocks(harness: Harness) -> None:
    await harness.pause(f"{REFUSED}/")
    replacement = RequestGate(
        SimpleNamespace(new_browser_cdp_session=AsyncMock(return_value=FakeCDPSession())),
        url_refused=refused,
        attach_mode=False,
        origin_switch_declared=False,
        on_lost=lambda _gate: None,
        inherit_from=harness.gate,
    )

    assert replacement.page_blocked_count(harness.page) == 1


@pytest.fixture
async def harness_with_audit() -> tuple[Harness, list]:
    audited: list = []
    h = Harness(on_block=audited.append)
    await h.start()
    return h, audited


async def test_r6_blocks_on_page_finds_a_lookup_error_block_by_the_pages_target() -> None:
    cdp = FakeCDPSession()
    browser = SimpleNamespace(new_browser_cdp_session=AsyncMock(return_value=cdp))
    gates = request_gate.RequestGates(
        url_refused=refused,
        enabled=lambda: True,
        origin_switch_declared=lambda: False,
        default_attach_mode=lambda: False,
        on_lost=lambda _gate: None,
    )
    await gates.require(browser)
    page = Mock(context=SimpleNamespace(new_cdp_session=AsyncMock(return_value=page_session_for("ctx", "the-page"))))
    since = gates.mark(browser)
    cdp.replies["Target.getTargetInfo"] = RuntimeError("Target.getTargetInfo: boom")

    cdp.emit(
        "Fetch.requestPaused",
        {"requestId": "r1", "request": {"url": f"{REFUSED}/", "method": "GET", "headers": {}}, "frameId": "the-page"},
    )
    await cdp.wait_for_reply("r1")

    [block] = await gates.blocks_on_page(browser, page, since=since)
    assert block.target_id == "the-page"
    assert await gates.blocks_on_page(browser, page, since=since + 1) == []


@pytest.mark.parametrize(
    ("error", "converted"),
    [
        ("Page.goto: net::ERR_BLOCKED_BY_CLIENT at https://allowed.example/r", True),
        ("Locator.click: Timeout 1000ms exceeded.", False),
    ],
)
def test_r6_only_a_blocked_by_client_failure_is_the_gates(error: str, converted: bool) -> None:
    assert request_gate.stopped_by_gate(Exception(error)) is converted


# ── round 3: R12 and R15 ─────────────────────────────────────────────────────


async def test_r12_a_page_gets_one_bypass_session_however_often_and_concurrently_asked() -> None:
    gates = request_gate.RequestGates(
        url_refused=refused,
        enabled=lambda: True,
        origin_switch_declared=lambda: False,
        default_attach_mode=lambda: False,
        on_lost=lambda _gate: None,
    )

    async def slow_session(_page) -> FakeCDPSession:
        await asyncio.sleep(0.01)
        return FakeCDPSession()

    new_cdp_session = AsyncMock(side_effect=slow_session)
    page = Mock(context=SimpleNamespace(new_cdp_session=new_cdp_session))

    first = asyncio.create_task(gates.bypass_service_workers(page))
    await asyncio.sleep(0)
    await gates.bypass_service_workers(page)  # returns only once the first call's session holds the bypass
    assert page in gates._bypass_sessions, "the second caller went on before the page was bypassed"
    await first
    await gates.bypass_service_workers(page)

    assert new_cdp_session.await_count == 1


async def test_r15_a_replacement_gate_knows_the_previous_url_of_a_hop() -> None:
    """A same-origin POST hop paused at the replacement, right after a loss, is not taken for a cross-origin one."""
    h = await _attach_mode()
    _, first = await h.pause(f"{ALLOWED}/r/307", method="POST", headers=ORIGIN)
    cdp = FakeCDPSession()
    replacement = RequestGate(
        SimpleNamespace(new_browser_cdp_session=AsyncMock(return_value=cdp)),
        url_refused=refused,
        attach_mode=True,
        origin_switch_declared=False,
        on_lost=lambda _gate: None,
        inherit_from=h.gate,
    )
    await replacement.attach()
    cdp.emit(
        "Target.targetCreated",
        {"targetInfo": {"targetId": h.page, "type": "page", "browserContextId": h.owned, "url": ""}},
    )

    cdp.emit(
        "Fetch.requestPaused",
        {
            "requestId": "hop",
            "redirectedRequestId": first["requestId"],
            "request": {"url": f"{ALLOWED}/target", "method": "POST", "headers": ORIGIN},
            "frameId": h.page,
        },
    )

    method, _ = await cdp.wait_for_reply("hop")
    assert method == "Fetch.continueRequest"


# ── round 4: the service-worker bypass (F3) ──────────────────────────────────


def _gates() -> request_gate.RequestGates:
    return request_gate.RequestGates(
        url_refused=refused,
        enabled=lambda: True,
        origin_switch_declared=lambda: False,
        default_attach_mode=lambda: False,
        on_lost=lambda _gate: None,
    )


def _page(session_factory, *, closed: bool = False) -> Mock:
    return Mock(
        context=SimpleNamespace(new_cdp_session=AsyncMock(side_effect=session_factory)), is_closed=lambda: closed
    )


async def test_f3_a_bypass_session_that_closes_under_an_open_page_is_dropped_and_counted() -> None:
    gates, session = _gates(), FakeCDPSession()
    page = _page(lambda _page: session)
    await gates.bypass_service_workers(page)
    assert ("Network.enable", {"maxTotalBufferSize": 0, "maxResourceBufferSize": 0}) in session.sent

    session.emit("close", session)

    assert page not in gates._bypass_sessions
    assert gates.bypass_failures == 1
    await gates.bypass_service_workers(page)
    assert page in gates._bypass_sessions, "a later call could not set the bypass again"


async def test_f3_b_a_failure_on_a_page_that_has_closed_is_not_counted() -> None:
    gates = _gates()

    await gates.bypass_service_workers(
        _page(lambda _page: (_ for _ in ()).throw(RuntimeError("Target closed")), closed=True)
    )

    assert gates.bypass_failures == 0


async def test_f3_c_a_bypass_that_never_answers_is_bounded_for_both_callers(monkeypatch) -> None:
    monkeypatch.setattr(request_gate, "BYPASS_TIMEOUT_SECONDS", 0.05)
    gates = _gates()

    async def never(_page):
        await asyncio.Event().wait()

    page = _page(never)
    first = asyncio.create_task(gates.bypass_service_workers(page))
    await asyncio.sleep(0)

    await asyncio.wait_for(asyncio.gather(first, gates.bypass_service_workers(page)), timeout=2.0)

    assert gates.bypass_failures == 1


# ── round 4: the gate's own failure (F4), sequence numbers (F6) ──────────────


def test_f4_only_the_first_line_of_the_error_counts() -> None:
    from playwright.async_api import Error as PlaywrightError

    selector = PlaywrightError(
        'Locator.click: Timeout 1000ms exceeded.\nCall log:\n  - waiting for locator("text=net::ERR_BLOCKED_BY_CLIENT")'
    )
    stopped = PlaywrightError("Page.goto: net::ERR_BLOCKED_BY_CLIENT at https://allowed.example/r\nCall log:\n  - x")
    bare = PlaywrightError("net::ERR_BLOCKED_BY_CLIENT at https://allowed.example/r")

    assert not request_gate.stopped_by_gate(selector)
    assert request_gate.stopped_by_gate(stopped)
    assert request_gate.stopped_by_gate(bare)


async def _gates_on(cdp: FakeCDPSession) -> tuple[request_gate.RequestGates, SimpleNamespace]:
    browser = SimpleNamespace(new_browser_cdp_session=AsyncMock(return_value=cdp))
    gates = _gates()
    await gates.require(browser)
    return gates, browser


async def test_f4_the_first_pages_target_id_is_seeded_at_registration() -> None:
    gates, browser = await _gates_on(FakeCDPSession())
    context = SimpleNamespace(new_cdp_session=AsyncMock(return_value=page_session_for("ctx", "seeded")), on=Mock())
    page = Mock(context=context)
    await gates.get(browser).register_context(context, page)
    context.new_cdp_session.side_effect = RuntimeError("no page session now")

    assert await gates.target_id_of(page) == "seeded"


def _pause(cdp: FakeCDPSession, request_id: str, frame: str, url: str = f"{REFUSED}/") -> None:
    cdp.emit(
        "Fetch.requestPaused",
        {"requestId": request_id, "frameId": frame, "request": {"url": url, "method": "GET", "headers": {}}},
    )


async def test_f6_a_page_entry_evicted_and_recreated_still_reports_its_new_block(monkeypatch) -> None:
    monkeypatch.setattr(request_gate, "PAGES_KEPT", 1)
    cdp = FakeCDPSession()
    gates, browser = await _gates_on(cdp)
    context_id = new_context_id()
    for target in ("page-a", "page-b"):
        cdp.emit(
            "Target.targetCreated", {"targetInfo": {"targetId": target, "type": "page", "browserContextId": context_id}}
        )
    context = SimpleNamespace(new_cdp_session=AsyncMock(return_value=page_session_for(context_id, "page-a")), on=Mock())
    page_a = Mock(context=context)
    await gates.get(browser).register_context(context, page_a)
    _pause(cdp, "a1", "page-a")
    await cdp.wait_for_reply("a1")
    since = gates.mark(browser)
    _pause(cdp, "b1", "page-b")  # evicts page-a's entry
    await cdp.wait_for_reply("b1")

    _pause(cdp, "a2", "page-a", f"{REFUSED}/again")
    await cdp.wait_for_reply("a2")

    [block] = await gates.blocks_on_page(browser, page_a, since=since)
    assert block.origin == REFUSED and block.seq > since


async def test_f6_a_mark_needs_no_target_read_and_attributes_nothing_earlier() -> None:
    cdp = FakeCDPSession()
    gates, browser = await _gates_on(cdp)
    context_id = new_context_id()
    cdp.emit("Target.targetCreated", {"targetInfo": {"targetId": "p", "type": "page", "browserContextId": context_id}})
    context = SimpleNamespace(new_cdp_session=AsyncMock(return_value=page_session_for(context_id, "p")), on=Mock())
    page = Mock(context=context)
    await gates.get(browser).register_context(context, page)
    _pause(cdp, "earlier", "p")
    await cdp.wait_for_reply("earlier")
    context.new_cdp_session.reset_mock()

    since = gates.mark(browser)

    context.new_cdp_session.assert_not_awaited()
    assert await gates.blocks_on_page(browser, page, since=since) == []


# ── round 4: lifecycle hardening (F8) ────────────────────────────────────────


class _HangingCDP(FakeCDPSession):
    """A session whose ``hang`` method never answers, and whose detach can hang too."""

    def __init__(self, hang: str | None = None, *, detach_hangs: bool = False) -> None:
        super().__init__()
        self.hang = hang
        self.detach_hangs = detach_hangs

    async def send(self, method, params=None):
        if method == self.hang:
            self.sent.append((method, dict(params or {})))
            await asyncio.Event().wait()
        return await super().send(method, params)

    async def detach(self) -> None:
        if self.detach_hangs:
            await asyncio.Event().wait()
        await super().detach()


def _bare_gate(cdp: FakeCDPSession, **options) -> RequestGate:
    return RequestGate(
        SimpleNamespace(new_browser_cdp_session=AsyncMock(return_value=cdp)),
        url_refused=refused,
        attach_mode=options.pop("attach_mode", False),
        origin_switch_declared=False,
        on_lost=options.pop("on_lost", lambda _gate: None),
    )


async def test_f8a_a_cancelled_attach_detaches_its_session() -> None:
    cdp = _HangingCDP("Fetch.enable")
    attaching = asyncio.create_task(_bare_gate(cdp).attach())
    while not any(method == "Fetch.enable" for method, _ in cdp.sent):
        await asyncio.sleep(0)

    attaching.cancel()
    with pytest.raises(asyncio.CancelledError):
        await attaching

    assert cdp.detached


async def test_f8c_an_attach_that_never_answers_is_bounded_and_detached(monkeypatch) -> None:
    monkeypatch.setattr(request_gate, "ATTACH_TIMEOUT_SECONDS", 0.05)
    cdp = _HangingCDP("Target.setDiscoverTargets")

    with pytest.raises(RequestGateUnavailable, match="did not attach within"):
        await asyncio.wait_for(_bare_gate(cdp).attach(), timeout=2.0)

    assert cdp.detached


async def test_f8c_a_detach_that_never_answers_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(request_gate, "DETACH_TIMEOUT_SECONDS", 0.05)
    cdp = _HangingCDP(detach_hangs=True)
    gate = _bare_gate(cdp)
    await gate.attach()

    await asyncio.wait_for(gate.detach(), timeout=2.0)

    assert not gate.live


async def test_f8b_release_waits_for_an_attach_in_flight() -> None:
    gates, browser = await _gates_on(FakeCDPSession())
    await gates._lock.acquire()
    releasing = asyncio.create_task(gates.release(browser))
    await asyncio.sleep(0.01)

    assert gates.get(browser) is not None, "release ran while require held the lock"
    gates._lock.release()
    await releasing
    assert gates.get(browser) is None


async def test_f8e_a_page_target_known_without_its_context_is_looked_up(harness: Harness) -> None:
    harness.cdp.emit("Target.targetCreated", {"targetInfo": {"targetId": "half-known", "type": "page"}})
    harness.cdp.replies["Target.getTargetInfo"] = {
        "targetInfo": {"targetId": "half-known", "type": "page", "browserContextId": harness.owned}
    }

    method, _ = await harness.pause(f"{REFUSED}/", frame="half-known")

    assert method == "Fetch.failRequest"
    assert harness.lookups(), "judged unowned from an entry with no context"


async def test_f8f_a_lost_gates_status_carries_the_failed_reattach() -> None:
    cdp = FakeCDPSession()
    gates, browser = await _gates_on(cdp)
    cdp.emit("close", cdp)
    browser.new_browser_cdp_session.side_effect = RuntimeError("no more sessions")

    with pytest.raises(RequestGateUnavailable):
        await gates.require(browser)

    [entry] = gates.status()["gates"]
    assert entry["state"] == "lost"
    assert "no more sessions" in entry["error"]


async def test_f8h_a_lookup_error_requests_url_is_remembered_for_its_next_hop() -> None:
    harness = Harness(attach_mode=True)
    await harness.start()
    harness.cdp.replies["Target.getTargetInfo"] = RuntimeError("Target.getTargetInfo: boom")

    method, params = await harness.pause(f"{ALLOWED}/form", frame="unknown-frame")

    assert method == "Fetch.continueRequest"
    assert harness.gate._request_urls[params["requestId"]] == f"{ALLOWED}/form"


# ── round 4: bookkeeping before the reply (F13) ──────────────────────────────


@pytest.mark.parametrize("step", ["_record_block", "_note_request", "_remember_url"])
async def test_f13_bookkeeping_that_raises_still_replies_and_counts_a_handler_error(
    harness: Harness, step: str
) -> None:
    def broken(*_args, **_kwargs):
        raise RuntimeError(f"{step} broke")

    setattr(harness.gate, step, broken)

    method, _ = await harness.pause(f"{REFUSED}/")

    assert method == "Fetch.failRequest"
    assert harness.gate.handler_errors == 1


async def test_f6_a_mark_from_before_a_loss_still_finds_the_replacements_block() -> None:
    """The sequence is shared by a replacement gate: a mark taken before the loss compares with its blocks."""
    cdp = FakeCDPSession()
    gates, browser = await _gates_on(cdp)
    context_id = new_context_id()
    cdp.emit("Target.targetCreated", {"targetInfo": {"targetId": "p", "type": "page", "browserContextId": context_id}})
    context = SimpleNamespace(new_cdp_session=AsyncMock(return_value=page_session_for(context_id, "p")), on=Mock())
    page = Mock(context=context)
    await gates.get(browser).register_context(context, page)
    for n in range(3):
        _pause(cdp, f"before-{n}", "p", f"{ALLOWED}/{n}")
        await cdp.wait_for_reply(f"before-{n}")
    since = gates.mark(browser)
    cdp.emit("close", cdp)
    replacement = FakeCDPSession()
    browser.new_browser_cdp_session.return_value = replacement
    await gates.require(browser)
    replacement.emit(
        "Target.targetCreated", {"targetInfo": {"targetId": "p", "type": "page", "browserContextId": context_id}}
    )

    _pause(replacement, "after", "p")
    await replacement.wait_for_reply("after")

    [block] = await gates.blocks_on_page(browser, page, since=since)
    assert block.seq > since


async def test_f3c_a_waiter_on_a_setup_that_never_settles_is_bounded(monkeypatch) -> None:
    """The waiter's own bound, apart from the first caller's: a pending setup that never settles."""
    monkeypatch.setattr(request_gate, "BYPASS_TIMEOUT_SECONDS", 0.05)
    gates = _gates()
    page = _page(lambda _page: FakeCDPSession())
    gates._bypass_pending[page] = asyncio.get_running_loop().create_future()

    await asyncio.wait_for(gates.bypass_service_workers(page), timeout=2.0)
