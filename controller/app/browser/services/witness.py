from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

import httpx

from ...audit import get_current_operator
from ...models import BrowserActionDecision, SessionRecord, WitnessRemoteState
from ...utils import spawn_background_task, utc_now
from ...witness import (
    WitnessActionContext,
    WitnessApproval,
    WitnessEvidence,
    WitnessPolicyOutcome,
    WitnessSessionContext,
)

if TYPE_CHECKING:
    from ...browser_manager import BrowserSession

logger = logging.getLogger(__name__)

# Hosted Witness delivery runs behind the action that recorded the receipt, never in its path: the action awaited the
# service's answer under its session's lock. One FIFO queue, bounded so an unreachable service cannot hold every
# receipt in memory, and one worker, so a scope's receipts reach the service in the order its chain holds them.
DELIVERY_QUEUE_SIZE = 1000
DELIVERY_ATTEMPTS = 3
DELIVERY_BACKOFF_SECONDS = 0.5
# How long a close waits for its session's receipts to reach the service before it saves their delivery state (about
# what the inline delivery of its close receipt took at worst), and how long shutdown lets the queue drain. Whatever
# is left is logged; the local chain holds it either way.
CLOSE_DELIVERY_WAIT_SECONDS = 1.0
SHUTDOWN_DELIVERY_WAIT_SECONDS = 3.0


@dataclass
class _Delivery:
    session: Any
    action: str
    payload: dict[str, Any]
    done: asyncio.Future[None]
    seq: int


def _never_taken(exc: Exception) -> bool:
    """Whether a failed delivery certainly left the service without the receipt, so sending it again cannot record
    it twice: no connection was made. After a timeout or any answer, a 429 or 503 included, the service may hold it
    (a proxy can answer for a service that recorded it), and the payload carries no id it could tell a second copy
    by."""
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))


# The witness context (risk, approval) of the action being dispatched, handed to the pipeline that records its
# receipt. It belongs to the calling task, not to the session: two actions on one session overlap while one waits
# for the session lock, and a field on the session let one's cleanup wipe the other's context.
_ACTION_WITNESS_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar("action_witness_context", default=None)


@contextmanager
def action_witness_context(context: dict[str, Any]) -> Iterator[None]:
    token = _ACTION_WITNESS_CONTEXT.set(context)
    try:
        yield
    finally:
        _ACTION_WITNESS_CONTEXT.reset(token)


class BrowserWitnessService:
    """Encapsulates Witness policy context and receipt delivery helpers."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager
        # The delivery queue and its worker belong to the event loop that made them (_delivery_queue).
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[_Delivery] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._in_flight: _Delivery | None = None
        # Each session's last queued delivery: when it is done, so are the session's earlier ones.
        self._pending: dict[str, asyncio.Future[None]] = {}
        # Each receipt's place in the order they were recorded, and, for a session with deliveries on their way, the
        # newest receipt whose outcome its state shows (_settle).
        self._sequence = itertools.count()
        self._newest: dict[str, int] = {}
        # Closed sessions' records waiting for their receipts' outcomes (save_when_delivered).
        self._saving: set[asyncio.Task[None]] = set()

    def initial_remote_state(self, protection_mode: str) -> WitnessRemoteState:
        configured = bool(self.manager.settings.witness_enabled and self.manager.witness_remote.enabled)
        return WitnessRemoteState(
            configured=configured,
            required=self.remote_required_for_profile(protection_mode),
            tenant_id=self.manager.settings.witness_remote_tenant_id,
            status="idle" if configured else "disabled",
        )

    def remote_required_for_profile(self, protection_mode: str) -> bool:
        return bool(
            self.manager.settings.witness_enabled
            and protection_mode == "confidential"
            and self.manager.settings.witness_remote_required_for_confidential
        )

    async def ensure_remote_ready(self, session: "BrowserSession", *, action: str) -> None:
        if not session.witness_remote_state.required:
            return
        checked_at = utc_now()
        if not self.manager.witness_remote.enabled:
            session.witness_remote_state.status = "failed"
            session.witness_remote_state.last_checked_at = checked_at
            session.witness_remote_state.last_error = (
                "Confidential session requires hosted Witness delivery, but WITNESS_REMOTE_URL is not configured."
            )
            raise PermissionError(session.witness_remote_state.last_error)
        try:
            await self.manager.witness_remote.healthz()
        except Exception as exc:
            session.witness_remote_state.status = "failed"
            session.witness_remote_state.last_checked_at = checked_at
            session.witness_remote_state.last_error = f"Hosted Witness preflight failed before {action}."
            raise PermissionError(session.witness_remote_state.last_error) from exc
        session.witness_remote_state.status = "healthy"
        session.witness_remote_state.last_checked_at = checked_at
        session.witness_remote_state.last_error = None

    def auth_material_encryption_ready(self) -> bool:
        return bool(self.manager.auth_state.require_encryption or self.manager.auth_state.encryption_enabled)

    def session_context(self, session: "BrowserSession") -> WitnessSessionContext:
        return WitnessSessionContext(
            session_id=session.id,
            profile=session.protection_mode,  # type: ignore[arg-type]
            isolation_mode=session.isolation_mode,
            shared_takeover_surface=session.shared_takeover_surface,
            shared_browser_process=session.shared_browser_process,
            auth_state_encrypted=self.auth_material_encryption_ready(),
            operator=get_current_operator(),
        )

    async def record_receipt(
        self,
        session: "BrowserSession",
        *,
        event_type: str,
        status: str,
        action: str,
        action_class: str,
        risk_category: str | None = None,
        target: dict[str, Any] | None = None,
        outcome: WitnessPolicyOutcome | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        verification: dict[str, Any] | None = None,
        approval: WitnessApproval | None = None,
        metadata: dict[str, Any] | None = None,
        approved_action: BrowserActionDecision | None = None,
    ) -> None:
        """Record a receipt; ``approved_action`` is the action an approval receipt's approval is for (_session_policy)."""
        if not self.manager.settings.witness_enabled:
            return
        policy = outcome or self._session_policy(session, approved_action=approved_action)
        engine = self.manager.witness_policy
        scrubber = self.manager.pii_scrubber
        payload = {
            "profile": session.protection_mode,  # type: ignore[arg-type]
            "event_type": event_type,
            "status": status,
            "action": action,
            "action_class": action_class,  # type: ignore[arg-type]
            "session_id": session.id,
            "risk_category": risk_category,
            "operator": get_current_operator(),
            "approval": engine.redact_approval(approval or WitnessApproval(), evidence_mode=policy.evidence_mode),
            "target": engine.redact_target(target or {}, evidence_mode=policy.evidence_mode, scrubber=scrubber),
            "concerns": policy.concerns,
            "evidence_mode": policy.evidence_mode,
            "evidence": WitnessEvidence(
                before=before if policy.evidence_mode == "standard" else None,
                after=after if policy.evidence_mode == "standard" else None,
                verification=verification,
                artifacts={},
            ),
            "metadata": engine.redact_target(metadata or {}, evidence_mode=policy.evidence_mode, scrubber=scrubber),
        }
        recorded = await self.manager.witness.record(session.id, **payload)
        if not self.manager.witness_remote.enabled:
            return
        # The local chain holds the receipt; the hosted copy follows in the background.
        remote_payload = recorded.model_dump(
            mode="json", exclude={"receipt_id", "scope", "chain_prev_hash", "chain_hash"}
        )
        self._submit(session, action, remote_payload)

    def _submit(self, session: "BrowserSession", action: str, payload: dict[str, Any]) -> None:
        queue = self._delivery_queue()
        done = asyncio.get_running_loop().create_future()
        seq = next(self._sequence)
        try:
            queue.put_nowait(_Delivery(session, action, payload, done, seq))
        except asyncio.QueueFull:
            self._settle(session, seq, f"Hosted Witness delivery queue is full; {action} was not delivered.")
            logger.warning(
                "witness remote delivery queue is full (%d receipts): session %s action %s not delivered",
                queue.maxsize,
                session.id,
                action,
            )
            return
        self._pending[session.id] = done
        done.add_done_callback(partial(self._forget_delivered, session.id))

    def _forget_delivered(self, session_id: str, done: asyncio.Future[None]) -> None:
        if self._pending.get(session_id) is done:
            del self._pending[session_id]
            self._newest.pop(session_id, None)

    def _delivery_queue(self) -> asyncio.Queue[_Delivery]:
        """The queue, with its worker running, on the current event loop: a loop that ended took its own with it."""
        loop = asyncio.get_running_loop()
        if self._loop is not loop or self._queue is None:
            self._loop, self._queue, self._worker = loop, asyncio.Queue(DELIVERY_QUEUE_SIZE), None
            self._pending, self._newest = {}, {}
        if self._worker is None or self._worker.done():
            self._worker = spawn_background_task(self._deliver_queued(self._queue))
        return self._queue

    async def _deliver_queued(self, queue: asyncio.Queue[_Delivery]) -> None:
        while True:
            item = await queue.get()
            self._in_flight = item
            try:
                await self._deliver(item)
            except asyncio.CancelledError:  # stop_delivery: the service may or may not hold the receipt
                stopped = f"Hosted Witness delivery of {item.action} was stopped before it finished."
                self._settle(item.session, item.seq, stopped)
                raise
            finally:
                self._in_flight = None
                if not item.done.done():
                    item.done.set_result(None)
                queue.task_done()

    async def _deliver(self, item: _Delivery) -> None:
        for attempt in range(1, DELIVERY_ATTEMPTS + 1):
            attempted_at = utc_now()
            try:
                await self.manager.witness_remote.record(item.session.id, item.payload)
            except Exception as exc:
                if attempt < DELIVERY_ATTEMPTS and _never_taken(exc):
                    await asyncio.sleep(DELIVERY_BACKOFF_SECONDS * 2 ** (attempt - 1))
                    continue
                failed = f"Hosted Witness delivery failed for {item.action}."
                self._settle(item.session, item.seq, failed, attempted_at=attempted_at)
                logger.warning(
                    "witness remote delivery failed for session %s action %s: %s",
                    item.session.id,
                    item.action,
                    exc,
                )
                return
            self._settle(item.session, item.seq, None, attempted_at=attempted_at)
            return

    def _settle(self, session: Any, seq: int, error: str | None, *, attempted_at: str | None = None) -> None:
        """Show one receipt's delivery outcome in its session's state: delivered when ``error`` is None.

        The status and the error stay the newest receipt's. A full queue fails a receipt at once, before the receipts
        queued ahead of it are sent, so their outcomes arrive later: they record their attempt times, not their status.
        """
        state = session.witness_remote_state
        if attempted_at is not None:
            state.last_attempted_at = attempted_at
            if error is None:
                state.last_delivered_at = attempted_at
        if seq < self._newest.get(session.id, seq):
            return
        if session.id in self._pending:  # older receipts' outcomes may follow: kept until they have (_forget_delivered)
            self._newest[session.id] = seq
        state.status = "delivered" if error is None else "failed"
        state.last_error = error

    async def wait_for_delivery(self, session_id: str, *, timeout: float = CLOSE_DELIVERY_WAIT_SECONDS) -> bool:
        """Wait up to ``timeout`` seconds for a session's queued receipts to reach the service or fail.

        False when some are still on their way (the service is slow, or other sessions' receipts are ahead)."""
        done = self._pending.get(session_id)
        if done is None or done.done():
            return True
        if done.get_loop() is not asyncio.get_running_loop():
            return False  # queued on a loop that ended: nothing delivers it now
        try:
            await asyncio.wait_for(asyncio.shield(done), timeout)
        except TimeoutError:
            return False
        return True

    def save_when_delivered(self, session: "BrowserSession", record: SessionRecord) -> None:
        """Save a closed session's record again once the receipts still on their way when it was saved have ended.

        The close saves the state its bounded wait ended with, and an outcome that came later changed only the closed
        session's object, so the record kept the earlier state for ever."""
        done = self._pending.get(session.id)
        if done is not None and done.get_loop() is not asyncio.get_running_loop():
            return  # queued on a loop that ended: nothing delivers it now
        saving = spawn_background_task(self._save_closed_record(session, record, done))
        self._saving.add(saving)
        saving.add_done_callback(self._saving.discard)

    async def _save_closed_record(
        self, session: "BrowserSession", record: SessionRecord, done: asyncio.Future[None] | None
    ) -> None:
        if done is not None:
            await asyncio.shield(done)
        state = session.witness_remote_state
        if state == record.witness_remote:
            return
        try:
            await self.manager.session_store.upsert(record.model_copy(update={"witness_remote": state.model_copy()}))
        except Exception as exc:  # the record keeps the state the close saved
            logger.warning("could not save the hosted Witness delivery state of closed session %s: %s", session.id, exc)

    async def stop_delivery(self, *, timeout: float = SHUTDOWN_DELIVERY_WAIT_SECONDS) -> None:
        """Let queued receipts reach the service for up to ``timeout`` seconds, then stop the worker.

        The worker stays the queue's while it drains: a receipt recorded meanwhile joins its queue, where clearing it
        first let that receipt start a second worker, which sent it beside the first's. A receipt still on its way
        when the worker stops fails its delivery, so its session's state no longer says idle or delivered for it. So
        do the receipts a worker that died (an error it did not expect) left queued, and the closed records waiting
        for them are saved before the stop returns, as they are after a drain."""
        worker, queue = self._worker, self._queue
        if worker is None or queue is None or self._loop is not asyncio.get_running_loop():
            return
        if worker.done():
            if not queue.empty():
                logger.warning(
                    "witness: the hosted Witness delivery worker had stopped; %d receipt(s) still queued were not "
                    "delivered, and the local chain holds them",
                    queue.qsize(),
                )
        else:
            try:
                await asyncio.wait_for(queue.join(), timeout)
            except TimeoutError:
                left = queue.qsize() + (self._in_flight is not None)
                logger.warning(
                    "witness: %d receipt(s) were not delivered to the hosted Witness service before shutdown; "
                    "the local chain holds them",
                    left,
                )
            worker.cancel()
            await asyncio.wait({worker})
        while not queue.empty():
            item = queue.get_nowait()
            unsent = f"Hosted Witness delivery of {item.action} was stopped before it was sent."
            self._settle(item.session, item.seq, unsent)
            if not item.done.done():
                item.done.set_result(None)
            queue.task_done()
        if self._saving:  # before the session store shuts down
            await asyncio.wait(set(self._saving), timeout=timeout)

    def _session_policy(
        self, session: "BrowserSession", *, approved_action: BrowserActionDecision | None = None
    ) -> WitnessPolicyOutcome:
        """The policy of a receipt recorded with none (an approval, a takeover), with no concerns (those are the
        session's and the action's own receipts'): the session's evidence mode, which a confidential profile
        restricts, or for an approval's receipts the mode of the action it approves, as the policy engine evaluates
        that action. It used to be standard always, and then the session's mode alone, so the approval receipts of a
        sensitive or high-risk action in a normal session kept the reason (which previews the call's arguments) that
        the action's own receipt withholds."""
        engine = self.manager.witness_policy
        context = self.session_context(session)
        if approved_action is None:
            mode = engine.evaluate_session(context).evidence_mode
        else:
            # As execute_decision gives the action its witness context when it runs.
            action = self.build_action_context(
                action_name=approved_action.action,
                target={},
                witness_context={
                    "risk_category": approved_action.risk_category,
                    "sensitive_input": approved_action.sensitive,
                },
            )
            mode = engine.evaluate_action(session=context, action=action).evidence_mode
        return WitnessPolicyOutcome(profile=session.protection_mode, evidence_mode=mode)  # type: ignore[arg-type]

    async def record_session_receipt(
        self,
        session: "BrowserSession",
        *,
        action: str,
        status: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not self.manager.settings.witness_enabled:
            return
        outcome = self.manager.witness_policy.evaluate_session(self.session_context(session))
        await self.manager._record_witness_receipt(
            session,
            event_type="session",
            status=status,
            action=action,
            action_class="control",
            outcome=outcome,
            metadata=metadata,
        )

    @staticmethod
    def action_class(action_name: str, *, risk_category: str | None = None) -> str:
        if risk_category in {"payment", "account_change", "destructive"}:
            return risk_category
        if action_name == "upload":
            return "upload"
        if action_name in {"save_auth_profile", "save_storage_state"}:
            return "auth"
        if action_name in {"request_human_takeover", "close_session", "create_session"}:
            return "control"
        if action_name in {"navigate", "hover", "scroll", "wait", "reload", "go_back", "go_forward"}:
            return "read"
        return "write"

    @staticmethod
    def consume_context(session: "BrowserSession") -> dict[str, Any]:
        payload = dict(_ACTION_WITNESS_CONTEXT.get() or {})
        _ACTION_WITNESS_CONTEXT.set(None)
        return payload

    def build_action_context(
        self,
        *,
        action_name: str,
        target: dict[str, Any],
        witness_context: dict[str, Any],
    ) -> WitnessActionContext:
        risk_category = witness_context.get("risk_category")
        action_class = self.action_class(action_name, risk_category=risk_category)
        sensitive_input = bool(
            witness_context.get("sensitive_input") or target.get("text_redacted") or target.get("sensitive")
        )
        stores_auth_material = bool(
            witness_context.get("stores_auth_material") or action_name in {"save_auth_profile", "save_storage_state"}
        )
        return WitnessActionContext(
            action=action_name,
            action_class=action_class,  # type: ignore[arg-type]
            risk_category=risk_category,
            target=target,
            approval_id=(witness_context.get("approval_id") or target.get("approval_id")),
            approval_status=witness_context.get("approval_status"),
            sensitive_input=sensitive_input,
            stores_auth_material=stores_auth_material,
            runtime_requires_approval=bool(witness_context.get("runtime_requires_approval")),
        )
