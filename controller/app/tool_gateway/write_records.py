"""The record of a state change the gateway makes itself: browser.set_viewport, set_cookies and set_local_storage.

These tools change a session outside the action pipeline, so they left no audit entry and no witness receipt in the
fast profile. Each call now leaves one of each, on success and on failure, written as a takeover's record is
(app.action_records): a cancellation does not cut the writes short, and what records next on the session waits for
them. A record names what changed and never a cookie or storage value, nor a hash of one: a hash of a short value can
be guessed offline. They follow the witness policy an action does, before anything touches the page, and a receipt says
what an action's would: the policy's evidence mode and concerns, and the approval a governed call ran under.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from ..action_records import write_record
from ..approvals import held_tool_call_approval
from ..browser.services.witness import BrowserWitnessService
from ..network_inspector import url_origin
from ..witness import WITNESS_POLICY_BLOCKED, WitnessApproval, WitnessPolicyOutcome

logger = logging.getLogger(__name__)

TOOL_WRITE_EVENT = "mcp_tool_write"
# A cookie's name and domain are the caller's text, which the input model does not bound.
_RECORDED_FIELD_CHARS = 200
_CANCELLED_AFTER_REACHING_PAGE = (
    "outcome unknown: the call reached the page and was cancelled. Observe the page before acting again."
)


def cookie_digest(cookie: dict[str, Any]) -> dict[str, str]:
    """A cookie as its write's record names it: its name, and its domain or its URL's origin. Never its value."""
    url = str(cookie.get("url") or "").strip()
    fields = {"name": cookie.get("name"), **({"url": url_origin(url)} if url else {"domain": cookie.get("domain")})}
    return {key: str(value).strip()[:_RECORDED_FIELD_CHARS] for key, value in fields.items()}


@dataclass(slots=True)
class RecordedWrite:
    """Set ``reached_page`` just before the write's call to the page: what its record says if the call is cancelled."""

    reached_page: bool = False


@contextlib.asynccontextmanager
async def recorded_write(
    manager: Any, session: Any, tool: str, digest: dict[str, Any], *, risk_category: str | None = None
) -> AsyncIterator[RecordedWrite]:
    """One audit entry and one witness receipt for ``tool``'s write on ``session``, however it ends.

    As the action pipeline treats an action: a call that fails records ``failed`` and the class of its error (its text
    can hold a page's content or a value), then the error goes on unchanged; a call cancelled before it reached the
    page records nothing, as nothing ran, and one cancelled after records ``error`` with an unknown outcome, as the
    page may have changed. A record that cannot be written is logged, never raised over the error being reported;
    with nothing to report, as for an action that ran, the record's own failure is raised.

    The witness policy comes first, as the pipeline's _prepare and the top of its _execute do, before anything touches
    the page: a required hosted Witness that does not answer and a policy block are refused here, recorded ``blocked``
    with that fixed text and ``reached_page`` false, and the refusal goes on.
    """
    write = RecordedWrite()
    target = {"digest": digest}
    # The witness's own classifier: a tool with no risk of its own is a write.
    action_class = BrowserWitnessService.action_class(tool, risk_category=risk_category)
    policy = manager.witness_policy.evaluate_action(
        session=manager._witness_session_context(session),
        action=manager._build_witness_action_context(
            action_name=tool, target=target, witness_context={"risk_category": risk_category}
        ),
    )

    async def record(status: str, **outcome: Any) -> None:
        # Before the writes' task starts: it can finish after the call's hold on its approval is released.
        approval = _receipt_approval(policy, reached_page=write.reached_page)

        async def writes() -> None:
            await manager.audit.append(
                event_type=TOOL_WRITE_EVENT,
                status=status,
                action=tool,
                session_id=session.id,
                details={"digest": digest, **outcome},
            )
            await manager._record_witness_receipt(
                session,
                event_type=TOOL_WRITE_EVENT,
                status=status,
                action=tool,
                action_class=action_class,
                risk_category=risk_category,
                target=target,
                outcome=policy,
                approval=approval,
                metadata=outcome,
            )

        await write_record(session, writes)

    try:
        await _refuse_by_policy(manager, session, tool, action_class, policy)
    except PermissionError as exc:
        await _record_beside(record("blocked", error=str(exc), reached_page=False), tool, exc)
        raise
    try:
        yield write
    except asyncio.CancelledError as exc:
        if write.reached_page:
            await _record_beside(record("error", error=_CANCELLED_AFTER_REACHING_PAGE, reached_page=True), tool, exc)
        raise
    except Exception as exc:
        await _record_beside(record("failed", error=type(exc).__name__, reached_page=write.reached_page), tool, exc)
        raise
    await record("ok")


async def _refuse_by_policy(
    manager: Any, session: Any, tool: str, action_class: str, policy: WitnessPolicyOutcome
) -> None:
    """What the action pipeline checks before an action (top of _execute): a required hosted Witness, the policy."""
    if action_class != "read":
        await manager._ensure_witness_remote_ready(session, action=tool)
    if policy.should_block:
        raise PermissionError(policy.block_reason or WITNESS_POLICY_BLOCKED)


def _receipt_approval(policy: WitnessPolicyOutcome, *, reached_page: bool) -> WitnessApproval:
    """The approval a receipt names: the one a governed call runs under, ``executed`` once the call reached the page and
    ``approved`` before (held_status's meaning), or, with none, only whether the policy requires one."""
    approval_id = held_tool_call_approval()
    if approval_id is None:
        return WitnessApproval(required=policy.require_approval)
    return WitnessApproval(required=True, approval_id=approval_id, status="executed" if reached_page else "approved")


async def _record_beside(recording: Any, tool: str, exc: BaseException) -> None:
    """Write the record of a call that ended in ``exc``, which then goes on: its own failure is only logged.

    A cancellation while it is written wins over an error being reported (its class is logged here, as the record
    keeps it), so a cancelled job ends cancelled; one that lands while a cancellation is being recorded is absorbed, and
    the first goes on. The writes finish behind it either way (write_record shields them). These logs carry none of
    ``exc``'s text and no traceback: that text can hold a page's content, and a traceback prints an exception's whole
    chain. A failed record is named by its own repr, which comes from the stores and not from the page.
    """
    try:
        await recording
    except asyncio.CancelledError:
        if not isinstance(exc, asyncio.CancelledError):
            logger.warning(
                "%s was cancelled while its %s was recorded; the cancellation goes on", tool, type(exc).__name__
            )
            raise
        logger.warning("cancelled again while recording the interrupted %s; the record finishes behind it", tool)
    except Exception as failure:
        logger.warning("could not record the %s that ended in %s: %r", tool, type(exc).__name__, failure)
