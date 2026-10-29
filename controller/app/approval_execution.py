"""How an execution holds and spends an approval: the hold, the refusals around it, and the give-back.

The store (approvals.ApprovalStore) keeps the records; this is the side an action runs under:
held_for_execution claims an approval for the action run in its block, effect_started marks the
moment the action may change something, and some endings before that give the approval back, a
refusal with a note saying so.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import Any, TypeVar

from fastapi import HTTPException

from .action_errors import BrowserActionError, SessionNotFoundError
from .models import ApprovalDecider, ApprovalOrigin, ApprovalRecord, ApprovalStatus

# The attribute a refusal carries the notes of the approvals given back with it in.
_GIVEN_BACK_NOTES = "approvals_given_back"

_T = TypeVar("_T")

logger = logging.getLogger(__name__)


class ExecutionHold:
    """One execution's claim on an approval, shared with every task the execution runs in.

    A task starts with a copy of its parent's context (asyncio.create_task, and
    asyncio.wait_for on Python 3.11), so the context variable below hands a
    child the holder's very object, and revoking it on release reaches the
    child too. A copied set of ids outlived the holder's release, and a
    straggling child still passed as part of the execution.
    """

    __slots__ = (
        "active",
        "approval_id",
        "claim_cancelled",
        "effect",
        "gives_back_on_its_own",
        "may_give_back",
        "origin",
        "refusal",
        "rejection",
        "stopped",
    )

    def __init__(self, approval_id: str, origin: ApprovalOrigin = "action") -> None:
        self.approval_id = approval_id
        # An action approval covers one operation; a tool call's may run several
        # (harness.start_convergence runs a whole attempt loop under one).
        self.origin = origin
        # Whether a refusal before the effect gives the approval back (held_for_execution):
        # only then can an operator's reject still stop what holds it.
        self.may_give_back = False
        self.active = True
        # Set just before the action could first change anything, and never cleared.
        self.effect = False
        # What the action pipeline raised before any change, when it refused.
        self.refusal: Exception | None = None
        # An operator's reject (comment, decided_via) that arrived after the claim and
        # before the effect: effect_started then refuses, and the give-back applies it.
        self.rejection: tuple[str | None, ApprovalDecider] | None = None
        # Set when effect_started refused the action on that rejection: an approve that
        # withdraws it afterwards is told the action was already refused, not that it may start.
        self.stopped = False
        # Set when the caller was cancelled while the claim was written (claim_execution): the
        # block then ends as that cancellation, before anything ran.
        self.claim_cancelled = False
        # Set when the block has ended and is given back: whether its ending gives back without
        # the waiting reject (the recorded refusal, a cancellation). If that reject is withdrawn
        # before the write, the give-back writes only when this is true (ApprovalStore.give_back).
        self.gives_back_on_its_own = False


# The current task's holds, by approval id. An approved action passes through
# several layers that each check the approval (the MCP gateway, the
# orchestrator, execute_decision, the upload service); the first one to claim
# owns the execution and the layers inside it must not claim again.
_HELD_APPROVALS: ContextVar[Mapping[str, ExecutionHold]] = ContextVar("held_approvals", default=MappingProxyType({}))


def _active_hold(approval_id: str) -> ExecutionHold | None:
    hold = _HELD_APPROVALS.get().get(approval_id)
    return hold if hold is not None and hold.active else None


class ApprovalRefusal(PermissionError):
    """effect_started's refusal, with the code the agent and the trail are given for it.

    It reached them as the pipeline's "Action blocked by policy", so an agent
    could not tell that its approval was spent, or rejected, from a policy block.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def effect_started() -> None:
    """Mark that the action run under the current task's holds may now change something.

    Called just before the change begins: by the action pipeline before an
    operation, and by an upload just before each transfer. From then on the
    approvals held for the action stay spent, whatever fails after.

    Refused (ApprovalRefusal, before any change) in a task that still carries a
    hold its owner has released: a child that entered the execution and waited
    acted after the approval was given back, and the retry acted again. Refused
    too for a second operation under one action approval, which children of the
    holder could otherwise run on the same claim, and for an approval an
    operator rejected while its action waited: the rejection stops the action.
    """
    holds = tuple(_HELD_APPROVALS.get().values())
    for hold in holds:
        if not hold.active:
            raise ApprovalRefusal(
                f"approval {hold.approval_id} was released before this action began", code="approval_released"
            )
        if hold.rejection is not None:
            hold.stopped = True
            raise ApprovalRefusal(
                f"approval {hold.approval_id} was rejected by an operator before its action began",
                code="approval_rejected",
            )
        if hold.effect and hold.origin == "action":
            raise ApprovalRefusal(
                f"approval {hold.approval_id} has already started its action; it covers one action",
                code="approval_already_used",
            )
    for hold in holds:
        hold.effect = True


def refused_before_effect(exc: Exception) -> None:
    """Record that the action pipeline raised ``exc`` before the action changed anything.

    The layer that claimed the approval gives it back when this is what ends
    its execution (held_for_execution); any other ending leaves it spent.
    """
    for hold in _HELD_APPROVALS.get().values():
        if hold.active and not hold.effect:
            hold.refusal = exc


def held_status(approval_id: str) -> ApprovalStatus | None:
    """What became of ``approval_id`` for the action the current task runs, if it runs under it.

    ``executed`` once the action began its change, ``approved`` before (a refusal
    then gives it back), or ``rejected`` when an operator's reject waits on it (the
    give-back applies it); None when the current task does not hold it: an id the
    caller passed that nothing claimed (an action that needs no approval, or one
    already spent or rejected) says nothing about the approval.
    """
    hold = _active_hold(approval_id)
    if hold is None:
        return None
    if hold.effect:
        return "executed"
    return "rejected" if hold.rejection is not None else "approved"


def held_action_approval() -> str | None:
    """The action approval the current task's execution holds, for its receipts; None if it holds none.

    A governed call spent through governed_approval_id passes no approval_id to
    the action, and its receipt said that no approval was required. Only a live
    hold counts: a task still carrying a released one runs under nothing, and a
    tool call's approval is the call's, not the action's.
    """
    for approval_id, hold in _HELD_APPROVALS.get().items():
        if hold.active and hold.origin == "action":
            return approval_id
    return None


class ApprovalRequiredError(HTTPException):
    def __init__(self, approval: ApprovalRecord, message: str | None = None):
        self.approval = approval
        # approval_id is what the caller passes back once an operator approves;
        # tool descriptions promise it, so it is a top-level key rather than
        # only a field of the nested record.
        self.payload = {
            "status": "approval_required",
            "message": message or f"{approval.kind} actions require human approval",
            "approval_id": approval.id,
            "approval": approval.model_dump(),
        }
        super().__init__(status_code=409, detail=self.payload)


def spent_refusal(approval: ApprovalRecord) -> PermissionError:
    """The refusal of an approval that is spent: when, that its action may have taken effect, and what then.

    The check comes before the way to ask again: told only to repeat the call, an agent whose action had gone
    through (then failed a later step) asked for a second payment or send, as the pipeline's after-change note
    warns against.
    """
    return PermissionError(
        f"approval {approval.id} was spent when its action started (executed_at {approval.executed_at}), so the "
        "action may have taken effect: observe the page, or check the outcome, before acting again. Only to do it "
        "again, repeat the call without approval_id, which asks an operator for a new approval"
    )


def executed_refusal(approval: ApprovalRecord, claim: ExecutionHold | None) -> PermissionError:
    """The refusal of an executed approval: spent, or claimed by an action that has not begun.

    Told it was spent while the claim still waited, an agent asked for a new
    approval; the claim was then refused before its effect and given back, and
    one action had two live approvals.
    """
    if claim is not None and claim.may_give_back and not claim.effect:
        return PermissionError(
            f"approval {approval.id} is claimed by an action that has not begun: wait for that call's answer; "
            "if the action is refused before it begins, the approval comes back and that answer says so; "
            "do not ask for a new approval"
        )
    return spent_refusal(approval)


class ServerStateRefusal(PermissionError):
    """A decision refused with why, in words that name only what the server knows.

    Never anything an agent wrote, so the approve and reject routes answer a 409
    with it instead of a bare "Conflict": an approval its action holds
    (ClaimedApprovalError), or one whose arguments or text the controller no
    longer has (ApprovalStore._refuse_what_is_no_longer_whole).
    """


class ClaimedApprovalError(ServerStateRefusal):
    """A decision on an approval whose action holds it, refused with why.

    Its text names the approval, when it was claimed and what holds it.
    """


def _claim_conflict(
    approval: ApprovalRecord, claim: ExecutionHold | None, *, decided_via: ApprovalDecider, status: ApprovalStatus
) -> ClaimedApprovalError:
    """Why a decision cannot change ``approval``, which its action has claimed."""
    if claim is None:
        return ClaimedApprovalError(
            f"approval {approval.id} has already been spent: its action started (executed_at {approval.executed_at})"
        )
    if not claim.may_give_back:
        return ClaimedApprovalError(
            f"approval {approval.id} has already been spent on the tool call that is running: a decision cannot stop it"
        )
    if claim.effect:
        return ClaimedApprovalError(
            f"approval {approval.id} has already been spent: its action has begun, and a decision cannot stop it"
        )
    if decided_via == "agent":
        if status == "approved":
            return ClaimedApprovalError(
                f"approval {approval.id} has already been claimed by its action: "
                "only an operator can approve it while the action has not begun"
            )
        return ClaimedApprovalError(
            f"approval {approval.id} has already been claimed by its action: "
            "only an operator can reject it before the action begins"
        )
    return ClaimedApprovalError(
        f"approval {approval.id} has already been claimed by its action, which has not begun: "
        "it is approved for that action"
    )


def _report_given_back(refusal: Exception, approval: ApprovalRecord, *, expired: bool) -> None:
    """Tell the agent, in the refusal it gets, what became of the approval it ran under.

    It was told nothing: it asked again without the id, the operator approved a
    second approval for the same action, and the agent could run both. Every
    layer's note is kept (approvals_given_back, innermost first): the outer
    layer's used to replace the inner one's. Any exception carries them, for a
    caller that answers with its own shape (given_back_notes).
    """
    if approval.status == "rejected":
        status = "rejected"
        message = (
            f"approval {approval.id} was rejected by an operator while its action waited, "
            "and the action was refused before it began"
        )
    elif expired:
        status = "expired"
        message = (
            f"approval {approval.id} expired while its action waited, and the action was refused before it "
            "began: repeat the call without approval_id to ask for a new approval"
        )
    elif isinstance(refusal, SessionNotFoundError):
        # Not "retry once the cause is gone": a closed session never comes back, and an approval is bound to
        # the session it was asked for in.
        status = "approved"
        message = (
            f"approval {approval.id} is still approved, but its session has closed, so nothing can run it: to do "
            "the action, repeat the call in a live session without approval_id, which asks for a new approval"
        )
    else:
        status = "approved"
        message = (
            f"approval {approval.id} is still approved: the action was refused before it began. Retry with this "
            "approval_id once the cause is gone; asking again would queue a second approval for the same action"
        )
    note = {"approval_id": approval.id, "status": status, "message": message}
    notes = [*getattr(refusal, _GIVEN_BACK_NOTES, ()), note]
    setattr(refusal, _GIVEN_BACK_NOTES, notes)
    if isinstance(refusal, BrowserActionError):
        refusal.details.update(given_back_notes(refusal))
    elif isinstance(refusal, ApprovalRequiredError):
        refusal.payload.update(given_back_notes(refusal))


def given_back_notes(exc: BaseException) -> dict[str, Any]:
    """The notes of the approvals given back with ``exc``, to add to an answer; {} when none was.

    ``approval_given_back`` is the outermost layer's (the one the caller passed
    as governed_approval_id, when there are two), ``approvals_given_back`` every one.
    """
    notes = getattr(exc, _GIVEN_BACK_NOTES, None)
    if not notes:
        return {}
    return {"approval_given_back": notes[-1], "approvals_given_back": list(notes)}


def _gives_back(hold: ExecutionHold, ended: BaseException | None) -> bool:
    """Whether a block that ended so (None: it returned) gives its approval back.

    Only a block that may give back, with nothing begun: on the refusal recorded
    before the effect, on a cancellation, and on any ending while an operator's
    reject waits on the hold. Another exception with no reject waiting leaves
    the approval spent.
    """
    if not hold.may_give_back or hold.effect:
        return False
    return hold.rejection is not None or _gives_back_on_its_own(hold, ended)


def _gives_back_on_its_own(hold: ExecutionHold, ended: BaseException | None) -> bool:
    """Whether the ending alone gives the approval back, without an operator's reject waiting on it.

    Its guard repeats _gives_back's, which both callers pass first, so it stays correct if called alone.
    """
    if not hold.may_give_back or hold.effect:
        return False
    return isinstance(ended, asyncio.CancelledError) or (ended is not None and ended is hold.refusal)


async def _run_through(awaitable: Awaitable[_T]) -> tuple[_T, bool]:
    """Await ``awaitable`` to its end even if the caller is cancelled meanwhile: (its result, whether it was).

    A give-back's write runs in a worker thread, which a cancellation does not
    stop: cancelled around it, the record landed while the hold ended as spent.
    If the awaitable then fails, the caller still ends cancelled, the failure
    logged and kept as the cancellation's cause: it used to replace the
    cancellation, so a cancelled job read as failed.
    """
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            if task.cancelled():
                raise  # the awaitable's own cancellation, not its caller's
            cancelled = True
        except Exception as exc:
            if not cancelled:
                raise
            logger.warning("%r failed while its caller was cancelled: %r", awaitable, exc, exc_info=exc)
            raise asyncio.CancelledError from exc


@asynccontextmanager
async def held_for_execution(store: Any, approval_id: str, *, may_give_back: bool = False) -> AsyncIterator[None]:
    """Claim, and so spend, `approval_id` for the action run inside the block.

    Every path that executes an approved action uses this, so the claim happens
    exactly once however many layers checked the approval: the outermost layer
    owns it, and the layers it calls find the approval already held by their
    own task. In a block that may give back, the owner gives the approval back
    on the refusal the action pipeline recorded before the action changed
    anything (refused_before_effect), on a cancellation with no effect, and on
    any ending with no effect while an operator's reject waits on the hold,
    which then applies. Any other exception with no effect and no reject waiting
    leaves it spent, as does a cancellation after effect_started or outside a
    block that may give back, and a crash or a restart, after which nothing is
    left to give it back.

    ``may_give_back`` is for a block that is exactly one action call: only the
    action pipeline and the upload transfer mark an effect, so a block that does
    anything else (a governed tool's handler, which may change the page itself
    before it runs an action) would be given back on a refusal after its change.
    """
    try:
        hold = await store.claim_execution(approval_id)
    except Exception as exc:
        # Inside an outer layer's hold this is a refusal before anything ran,
        # whatever it is: an approval not approved, or one that does not exist.
        refused_before_effect(exc)
        raise
    if hold is None:
        yield
        return
    # Set before anything is awaited, so an operator's reject never sees the hold without it.
    hold.may_give_back = may_give_back
    spent = True

    async def give_back(ended: BaseException | None) -> None:
        nonlocal spent
        # Before anything is awaited: a task that entered the execution and
        # resumes while the record is written back would otherwise act on an
        # approval that is approved again, and a retry would act a second time.
        # Only that: the hold stays a live claim until it is released, so an
        # operator's decision that reads the record meanwhile is put on the hold
        # (ApprovalStore.decide) and the give-back applies it. Dropped from
        # the claims here, such a reject was refused as spent, and the retry ran.
        hold.gives_back_on_its_own = _gives_back_on_its_own(hold, ended)
        hold.active = False
        approval, cancelled = await _run_through(store.give_back(hold))
        if approval is None:
            # Given back only for a reject, withdrawn before the write: the ending alone leaves it spent.
            if cancelled:
                raise asyncio.CancelledError
            return
        # Written back: nothing after this (the audit, a cancellation) may treat it as spent.
        spent = False
        if isinstance(ended, Exception):
            _report_given_back(ended, approval, expired=store.has_expired(approval))
        _, cancelled_in_audit = await _run_through(store.record_given_back(approval))
        if cancelled or cancelled_in_audit:
            raise asyncio.CancelledError  # the caller's, held back until the record and its audit agree

    try:
        if hold.claim_cancelled:
            # The caller was cancelled while its claim was written: nothing ran, so the block ends as
            # a cancellation before the effect, and the body never starts.
            raise asyncio.CancelledError
        yield
    except (Exception, asyncio.CancelledError) as exc:
        if _gives_back(hold, exc):
            await give_back(exc)
        elif hold.claim_cancelled:
            await _run_through(store.record_cancelled_claim(hold))
        raise
    else:
        if _gives_back(hold, None):
            await give_back(None)
    finally:
        store.release_execution(hold, spent=spent)
        if spent and hold.origin == "tool_call":
            # Final: the call ran, or began to. The record keeps their size and sha256.
            await store.discard_arguments(hold.approval_id)
        if spent and hold.rejection is not None:
            await store.record_rejection_not_applied(hold)
