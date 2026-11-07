"""ACT-34: one scope's append never waits for another's, and hosted Witness delivery runs behind the action.

Every append took one recorder-wide asyncio lock and one thread lock, so each session's receipts queued behind
every other session's file writes; and record_receipt awaited the hosted service's answer inline, under the
session's lock, so a slow or unreachable service slowed every action of a session that used one.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.browser.services import witness as witness_service
from app.browser.services.witness import BrowserWitnessService
from app.models import SessionRecord, WitnessRemoteState
from app.session_ownership import acting_as
from app.witness import WitnessPolicyEngine, WitnessRecorder, WitnessRemoteClient, WitnessRemoteError
from tests.test_auth_state_ownership import ALICE, build_manager

OPERATOR = {"id": "alice", "source": "header"}


def _fields(action: str = "click") -> dict:
    return {
        "profile": "normal",
        "event_type": "browser_action",
        "status": "ok",
        "action": action,
        "action_class": "write",
        "operator": OPERATOR,
    }


async def test_an_append_does_not_wait_for_another_scopes_append(tmp_path: Path, monkeypatch) -> None:
    recorder = WitnessRecorder(tmp_path)
    await recorder.startup()
    entered = threading.Event()
    release = threading.Event()
    original = WitnessRecorder._set_aside_torn_tail

    def held(path: Path, handle) -> None:
        # Runs inside the chain's thread lock and file lock: scope "a" holds both until released.
        if path.name == "a.jsonl":
            entered.set()
            release.wait(10)
        original(path, handle)

    monkeypatch.setattr(WitnessRecorder, "_set_aside_torn_tail", staticmethod(held))
    first = asyncio.create_task(recorder.record("a", **_fields()))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        second = await asyncio.wait_for(recorder.record("b", **_fields()), 5)
    finally:
        release.set()
        await first

    assert second.chain_prev_hash is None
    assert (await recorder.verify("a"))["valid"]
    assert (await recorder.verify("b"))["valid"]


async def test_concurrent_appends_to_two_scopes_keep_both_chains_whole(tmp_path: Path) -> None:
    recorder = WitnessRecorder(tmp_path)
    await recorder.startup()

    await asyncio.gather(*(recorder.record(scope, **_fields(f"act-{n}")) for n in range(15) for scope in ("a", "b")))

    for scope in ("a", "b"):
        verified = await recorder.verify(scope)
        assert verified["valid"], verified
        assert verified["receipt_count"] == 15
        # One scope's receipts in the order they were asked for: its lock is first come, first served.
        listed = await recorder.list(scope, limit=100)
        assert [item.action for item in reversed(listed)] == [f"act-{n}" for n in range(15)]
    assert recorder._scope_locks == {}  # a scope's lock is dropped once nothing holds or waits for it


@pytest.fixture
async def make_service(tmp_path: Path):
    """A witness bridge over a fake hosted service; its delivery worker is stopped after the test."""
    made: list[BrowserWitnessService] = []

    def make(record) -> tuple[BrowserWitnessService, SimpleNamespace]:
        manager = SimpleNamespace(
            settings=SimpleNamespace(witness_enabled=True),
            witness=WitnessRecorder(tmp_path / "witness"),
            witness_policy=WitnessPolicyEngine(),
            witness_remote=SimpleNamespace(enabled=True, record=record),
            auth_state=SimpleNamespace(require_encryption=False, encryption_enabled=False),
        )
        made.append(BrowserWitnessService(manager))
        return made[-1], manager

    yield make
    for service in made:
        await service.stop_delivery(timeout=1)


def _session(session_id: str = "s1") -> SimpleNamespace:
    return SimpleNamespace(
        id=session_id,
        protection_mode="normal",
        isolation_mode="shared_browser_node",
        shared_takeover_surface=True,
        shared_browser_process=True,
        witness_remote_state=WitnessRemoteState(configured=True, status="idle"),
    )


async def _record(service: BrowserWitnessService, session: SimpleNamespace, action: str = "click") -> None:
    # Bounded: recording never waits for the service, so a call that does fails here instead of hanging.
    recorded = service.record_receipt(
        session,
        event_type="browser_action",
        status="ok",
        action=action,
        action_class="write",
        target={"selector": "#go"},
    )
    await asyncio.wait_for(recorded, 2)


@pytest.fixture
def no_backoff(monkeypatch) -> None:
    monkeypatch.setattr(witness_service, "DELIVERY_BACKOFF_SECONDS", 0)


async def test_a_receipt_is_recorded_without_waiting_for_the_hosted_service(make_service) -> None:
    answer = asyncio.Event()
    sent: list[str] = []

    async def record(scope: str, payload: dict) -> dict:
        sent.append(payload["action"])
        await answer.wait()
        return {"ok": True}

    service, manager = make_service(record)
    session = _session()

    await asyncio.wait_for(_record(service, session), 2)

    assert len(await manager.witness.list("s1")) == 1  # the local chain has it first
    assert session.witness_remote_state.status == "idle"
    answer.set()
    assert await service.wait_for_delivery("s1", timeout=2)
    assert sent == ["click"]
    assert session.witness_remote_state.status == "delivered"
    assert session.witness_remote_state.last_delivered_at is not None
    await service.stop_delivery(timeout=1)


async def test_receipts_reach_the_service_in_the_order_they_were_recorded(make_service) -> None:
    sent: list[tuple[str, str]] = []

    async def record(scope: str, payload: dict) -> dict:
        await asyncio.sleep(0)
        sent.append((scope, payload["action"]))
        return {}

    service, _ = make_service(record)
    first, second = _session("s1"), _session("s2")
    for n in range(4):
        await _record(service, first, f"a{n}")
        await _record(service, second, f"b{n}")

    await service.stop_delivery(timeout=2)

    assert sent == [(scope, f"{prefix}{n}") for n in range(4) for scope, prefix in (("s1", "a"), ("s2", "b"))]


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(lambda: httpx.ConnectError("refused"), id="connect-error"),
        pytest.param(lambda: httpx.ConnectTimeout("no connection"), id="connect-timeout"),
        pytest.param(lambda: httpx.PoolTimeout("no free connection"), id="pool-timeout"),
    ],
)
async def test_a_failure_the_service_never_took_is_sent_again(make_service, no_backoff, failure) -> None:
    attempts: list[int] = []

    async def record(scope: str, payload: dict) -> dict:
        attempts.append(1)
        if len(attempts) < 3:
            raise failure()
        return {}

    service, _ = make_service(record)
    session = _session()

    await _record(service, session)
    assert await service.wait_for_delivery("s1", timeout=2)

    assert len(attempts) == 3
    assert session.witness_remote_state.status == "delivered"
    assert session.witness_remote_state.last_error is None


async def test_delivery_gives_up_after_three_attempts(make_service, no_backoff, caplog) -> None:
    attempts: list[int] = []

    async def record(scope: str, payload: dict) -> dict:
        attempts.append(1)
        raise httpx.ConnectError("refused")

    service, manager = make_service(record)
    session = _session()

    with caplog.at_level(logging.WARNING, logger="app.browser.services.witness"):
        await _record(service, session)
        assert await service.wait_for_delivery("s1", timeout=2)

    assert len(attempts) == 3
    assert session.witness_remote_state.status == "failed"
    assert session.witness_remote_state.last_error == "Hosted Witness delivery failed for click."
    assert "witness remote delivery failed for session s1 action click" in caplog.text
    assert len(await manager.witness.list("s1")) == 1  # the local chain keeps it whatever the service did


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(lambda: httpx.ReadTimeout("no answer"), id="read-timeout"),
        # An answer, even 503 or 429, does not prove the service did not record the receipt (Codex review): with no
        # id to tell a second copy by, sending it again could record it twice.
        pytest.param(lambda: WitnessRemoteError("busy", status_code=503), id="http-503"),
        pytest.param(lambda: WitnessRemoteError("slow down", status_code=429), id="http-429"),
        pytest.param(lambda: WitnessRemoteError("oops", status_code=500), id="http-500"),
        pytest.param(lambda: WitnessRemoteError("bad gateway", status_code=502), id="http-502"),
        pytest.param(lambda: WitnessRemoteError("bad request", status_code=400), id="http-400"),
        pytest.param(lambda: RuntimeError("Witness remote client is not started"), id="not-started"),
    ],
)
async def test_a_failure_after_which_the_service_may_hold_the_receipt_is_not_sent_again(
    make_service, no_backoff, failure
) -> None:
    attempts: list[int] = []

    async def record(scope: str, payload: dict) -> dict:
        attempts.append(1)
        raise failure()

    service, _ = make_service(record)
    session = _session()

    await _record(service, session)
    assert await service.wait_for_delivery("s1", timeout=2)

    assert len(attempts) == 1  # the payload has no id the service could deduplicate a second copy by
    assert session.witness_remote_state.status == "failed"


async def test_a_full_delivery_queue_fails_the_receipts_delivery_not_the_action(make_service, monkeypatch) -> None:
    monkeypatch.setattr(witness_service, "DELIVERY_QUEUE_SIZE", 1)
    answer = asyncio.Event()
    sent: list[str] = []

    async def record(scope: str, payload: dict) -> dict:
        sent.append(payload["action"])
        await answer.wait()
        return {}

    service, manager = make_service(record)
    session = _session()

    await _record(service, session, "first")  # taken by the worker, which waits for the answer
    await _record(service, session, "second")  # fills the queue
    await asyncio.wait_for(_record(service, session, "third"), 2)

    assert session.witness_remote_state.status == "failed"
    assert session.witness_remote_state.last_error == "Hosted Witness delivery queue is full; third was not delivered."
    assert len(await manager.witness.list("s1")) == 3
    answer.set()
    await service.stop_delivery(timeout=2)
    assert sent == ["first", "second"]


async def test_stopping_delivery_is_bounded_when_the_service_never_answers(make_service, caplog) -> None:
    async def record(scope: str, payload: dict) -> dict:
        await asyncio.Event().wait()
        return {}

    service, _ = make_service(record)
    session = _session()
    await _record(service, session, "first")
    await _record(service, session, "second")

    with caplog.at_level(logging.WARNING, logger="app.browser.services.witness"):
        await asyncio.wait_for(service.stop_delivery(timeout=0.2), 2)

    assert "2 receipt(s) were not delivered to the hosted Witness service before shutdown" in caplog.text
    assert await service.wait_for_delivery("s1", timeout=0.05)  # failed by the stop: none is left on its way


async def test_a_delivery_stopped_mid_send_and_those_queued_behind_it_say_so(make_service) -> None:
    # Codex and GLM reviews: the stop cancelled the receipt in flight and left the queued ones, and neither changed its
    # session's state, which kept saying idle (or the last receipt's outcome) for receipts that never went.
    async def record(scope: str, payload: dict) -> dict:
        await asyncio.Event().wait()
        return {}

    service, _ = make_service(record)
    first, second = _session("s1"), _session("s2")
    await _record(service, first, "click")  # in flight when delivery stops
    await _record(service, second, "type")  # still queued

    await asyncio.wait_for(service.stop_delivery(timeout=0.2), 2)

    assert first.witness_remote_state.status == "failed"
    assert first.witness_remote_state.last_error == "Hosted Witness delivery of click was stopped before it finished."
    assert second.witness_remote_state.status == "failed"
    assert second.witness_remote_state.last_error == "Hosted Witness delivery of type was stopped before it was sent."


async def test_a_full_queues_failure_is_not_hidden_by_the_receipts_queued_ahead_of_it(
    make_service, monkeypatch
) -> None:
    # Codex review: a full queue fails a receipt at once, and the receipts queued ahead of it, delivered later, set
    # the session's state back to delivered, so it no longer said the newest receipt never went.
    monkeypatch.setattr(witness_service, "DELIVERY_QUEUE_SIZE", 1)
    answer = asyncio.Event()

    async def record(scope: str, payload: dict) -> dict:
        await answer.wait()
        return {}

    service, _ = make_service(record)
    session = _session()
    state = session.witness_remote_state
    await _record(service, session, "first")  # taken by the worker, which waits for the answer
    await _record(service, session, "second")  # fills the queue
    await _record(service, session, "third")  # failed at once
    answer.set()
    assert await service.wait_for_delivery("s1", timeout=2)

    assert state.status == "failed"
    assert state.last_error == "Hosted Witness delivery queue is full; third was not delivered."
    assert state.last_delivered_at is not None  # the two ahead of it still count as delivered
    await _record(service, session, "fourth")  # a newer receipt's outcome is the one shown again
    assert await service.wait_for_delivery("s1", timeout=2)
    assert (state.status, state.last_error) == ("delivered", None)
    assert service._newest == {}  # forgotten once none of the session's receipts is on its way


async def test_a_receipt_recorded_while_delivery_stops_does_not_start_a_second_worker(make_service) -> None:
    # GLM review: stop_delivery dropped its worker before it drained the queue, so a receipt recorded meanwhile (a
    # session closing during shutdown) started a second worker on the same queue, which sent it beside the first.
    answer = asyncio.Event()
    sending = most_at_once = 0
    sent: list[str] = []

    async def record(scope: str, payload: dict) -> dict:
        nonlocal sending, most_at_once
        sending += 1
        most_at_once = max(most_at_once, sending)
        try:
            await answer.wait()
        finally:
            sending -= 1
        sent.append(payload["action"])
        return {}

    service, _ = make_service(record)
    session = _session()
    await _record(service, session, "first")  # taken by the worker, which waits for the answer
    stopping = asyncio.create_task(service.stop_delivery(timeout=2))
    await asyncio.sleep(0.05)  # draining the queue
    await _record(service, session, "second")
    await asyncio.sleep(0.05)
    answer.set()
    await asyncio.wait_for(stopping, 5)

    assert most_at_once == 1, "two workers sent receipts at once"
    assert sent == ["first", "second"]


def test_the_client_names_the_status_of_a_refused_answer() -> None:
    response = httpx.Response(503, json={"detail": "busy"}, request=httpx.Request("POST", "https://w.example/x"))

    with pytest.raises(WitnessRemoteError) as raised:
        WitnessRemoteClient._parse_response(response, operation="record witness receipt for scope s1")

    assert raised.value.status_code == 503
    assert str(raised.value) == "Witness remote record witness receipt for scope s1 failed with HTTP 503: busy"
    assert isinstance(raised.value, RuntimeError)  # callers that catch RuntimeError still do


class CloseRecordsTheDeliveredStateTests(unittest.IsolatedAsyncioTestCase):
    """A close waits (bounded) for its session's receipts to reach the service before it saves their state."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.manager, _ = build_manager(Path(self.tmp.name))
        self.manager.settings.max_sessions = 5
        self.sent: list[str] = []
        self.refused: set[str] = set()  # actions whose receipts the service refuses
        self.held: dict[str, asyncio.Event] = {}  # actions whose receipts it answers once their event is set

        async def record(scope: str, payload: dict) -> dict:
            action = payload["action"]
            if action in self.held:
                await self.held[action].wait()
            await asyncio.sleep(0.05)
            if action in self.refused:
                raise WitnessRemoteError("refused", status_code=400)
            self.sent.append(action)
            return {}

        self.manager.settings.witness_enabled = True  # build_manager's settings turn the witness off
        self.manager.witness_remote = SimpleNamespace(enabled=True, record=record)  # type: ignore[assignment]
        await self.manager.witness.startup()

    async def asyncTearDown(self) -> None:
        for held in self.held.values():
            held.set()
        await self.manager.witness_bridge.stop_delivery(timeout=1)
        self.tmp.cleanup()

    async def open_and_close(self) -> tuple[str, dict]:
        with acting_as(ALICE):
            session_id = (await self.manager.create_session(name="alice"))["id"]
            return session_id, await self.manager.close_session(session_id)

    async def stored_once(self, session_id: str, ready) -> SessionRecord:
        """The session's stored record once ``ready`` says so, or after two seconds whatever it says."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2
        while not ready(record := await self.manager.session_store.get(session_id)) and loop.time() < deadline:
            await asyncio.sleep(0.02)
        return record

    async def test_the_closed_record_shows_the_close_receipt_delivered(self) -> None:
        # GLM review: with the create receipt delivered too, the state said delivered before the close receipt went,
        # so a close that did not wait for it passed as well. Here only the close receipt's delivery succeeds.
        self.refused.add("create_session")

        session_id, result = await self.open_and_close()

        self.assertEqual(self.sent, ["close_session"])
        self.assertEqual(result["session"]["witness_remote"]["status"], "delivered")
        stored = await self.manager.session_store.get(session_id)
        self.assertEqual(stored.witness_remote.status, "delivered")

    async def test_a_delivery_that_ends_after_the_close_reaches_the_closed_record(self) -> None:
        # Codex review: the close saved the record with the state it had when its wait ran out, and the outcome that
        # came later changed only the closed session's object, so the record kept the earlier state for ever.
        answer = self.held["close_session"] = asyncio.Event()
        self.refused.add("close_session")

        session_id, result = await self.open_and_close()  # its wait for the close receipt runs out
        self.assertEqual(result["session"]["witness_remote"]["status"], "delivered")  # the create receipt's
        answer.set()

        stored = await self.stored_once(session_id, lambda record: record.witness_remote.status == "failed")
        self.assertEqual(stored.witness_remote.status, "failed")
        self.assertEqual(stored.witness_remote.last_error, "Hosted Witness delivery failed for close_session.")
        self.assertEqual(stored.status, "closed")

    async def test_a_delivery_stopped_at_shutdown_reaches_the_closed_record_before_the_stop_returns(self) -> None:
        # The store shuts down after the stop: a save that came later would be lost.
        self.held["close_session"] = asyncio.Event()  # never answered

        session_id, _ = await self.open_and_close()
        await self.manager.witness_bridge.stop_delivery(timeout=0.2)

        stored = await self.manager.session_store.get(session_id)
        self.assertEqual(stored.witness_remote.status, "failed")
        self.assertEqual(
            stored.witness_remote.last_error, "Hosted Witness delivery of close_session was stopped before it finished."
        )
