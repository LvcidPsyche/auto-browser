from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from .approval_backends import ApprovalStoreBackend, FileApprovalStore, SQLiteApprovalStore
from .approval_display import UNBOUND_FIELDS, approval_provenance

# The execution side moved to approval_execution.py; importers keep reaching it here.
from .approval_execution import (
    _HELD_APPROVALS,
    ApprovalRefusal,
    ApprovalRequiredError,
    ClaimedApprovalError,
    ExecutionHold,
    ServerStateRefusal,
    _active_hold,
    _claim_conflict,
    _run_through,
    effect_started,
    executed_refusal,
    given_back_notes,
    held_action_approval,
    held_for_execution,
    held_status,
    refused_before_effect,
    spent_refusal,
)
from .models import (
    ApprovalDecider,
    ApprovalKind,
    ApprovalOrigin,
    ApprovalRecord,
    ApprovalStatus,
    BrowserActionDecision,
)
from .session_ownership import may_use, verified_operator
from .utils import UTC, utc_now

logger = logging.getLogger(__name__)

__all__ = [
    "CONTROLLER_SCOPE",
    "SENSITIVE_TEXT_PREFIX",
    "SENSITIVE_TEXT_RETENTION",
    "ApprovalRefusal",
    "ApprovalRequiredError",
    "ApprovalStore",
    "ClaimedApprovalError",
    "ServerStateRefusal",
    "ExecutionHold",
    "effect_started",
    "given_back_notes",
    "held_action_approval",
    "held_for_execution",
    "held_status",
    "refused_before_effect",
    "spent_refusal",
]

# The scope of an approval that belongs to no session (a governed tool whose
# arguments name none). Session ids are uuid4().hex[:12], so this can never be one.
# There is no session owner to ask, so these approvals follow ApprovalRecord.owner.
CONTROLLER_SCOPE = "controller"
# What a sensitive `type` action's text is stored as in its approval.
SENSITIVE_TEXT_PREFIX = "[sensitive text] hmac-sha256:"
# How long the text of an undecided sensitive approval is kept in memory.
SENSITIVE_TEXT_RETENTION = timedelta(hours=24)
# What an approval of each origin authorises, as a refusal to spend it names it.
_ORIGIN_NAMES: dict[str, str] = {"action": "a browser action", "tool_call": "a tool call"}


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Decision:
    """What a decision did: the record, and what it did to a claim its action holds.

    ``deferred``: a reject put on a claim that has not begun, which stops the
    action. ``withdrawn``: an approve that took that reject back while the action
    may still start. ``withdrawn_after_refusal``: one that took it back once the
    action had already been refused (by that rejection, or before it began for
    another reason), so the approval goes back to approved without the action.
    ``withdrawn_spent``: one that took it back once the action had ended with an
    error that gives nothing back by itself, so the approval stays spent (call A).
    """

    record: ApprovalRecord
    on_claim: Literal["deferred", "withdrawn", "withdrawn_after_refusal", "withdrawn_spent"] | None = None


class ApprovalStore:
    def __init__(
        self,
        root: str | Path,
        db_path: str | None = None,
        *,
        approval_ttl_minutes: int = 15,
        audit: Any = None,
        rejection_receipt: Callable[[ApprovalRecord], Awaitable[None]] | None = None,
    ):
        self._lock = asyncio.Lock()
        # Where a give-back is recorded (an AuditStore); None in a bare store.
        self.audit = audit
        # Records the witness receipt of an operator's rejection that a give-back
        # applied (the manager's); None in a bare store, which keeps no receipts.
        self.rejection_receipt = rejection_receipt
        self.file_store = FileApprovalStore(root)
        self.sqlite_store = SQLiteApprovalStore(db_path) if db_path else None
        self._primary: ApprovalStoreBackend = self.file_store
        self.approval_ttl = timedelta(minutes=max(1, approval_ttl_minutes))
        # Keyed per process, so a stored digest cannot be brute-forced offline.
        # An approval for sensitive text therefore only works in the process
        # that created it: not after a restart, and not in another uvicorn
        # worker or replica. The action asks again rather than matching an
        # unverifiable record.
        self._sensitive_text_key = secrets.token_bytes(32)
        # The text itself stays in memory only, so an approved sensitive action
        # can still be run through execute_approval by this process.
        # Entries are dropped when the approval executes or is rejected, and
        # otherwise after SENSITIVE_TEXT_RETENTION, so text for an approval
        # nobody decides does not sit in memory until the process restarts.
        self._sensitive_texts: dict[str, tuple[str, float]] = {}
        # The live claims, by approval id, so an operator's reject can reach one
        # whose action has not started. In memory: a reject sent to another
        # worker or replica than the one running the action is refused as before.
        self._claims: dict[str, ExecutionHold] = {}

    async def startup(self) -> None:
        await self.file_store.startup()
        self._primary = self.file_store
        if self.sqlite_store is not None:
            try:
                await self.sqlite_store.startup()
            except Exception as exc:
                logger.warning("sqlite approval store unavailable, using file store fallback: %s", exc)
            else:
                self._primary = self.sqlite_store
        await self._sweep_arguments(startup=True)

    async def _sweep_arguments(self, *, startup: bool = False) -> None:
        """Remove the arguments no approve or call will read again: at each create (under the lock) and at startup.

        A rejected tool call's, once its rejection is older than the approval
        TTL: an operator may approve it again until then, and an approve after is
        refused for its arguments (_refuse_what_is_no_longer_whole). At startup
        also those of approvals spent, or gone, with no process left to: a
        call's release removes them once it spends the approval, and a crash or
        a restart mid-call left the record executed and its arguments readable
        for good.
        """
        for approval_id in await self.file_store.arguments_ids():
            try:
                approval: ApprovalRecord | None = await self.get(approval_id)
            except KeyError:
                approval = None
            except Exception:
                if startup:
                    logger.warning(
                        "kept the arguments of approval %s: its record is unreadable", approval_id, exc_info=True
                    )
                continue
            if approval is None or approval.status == "executed":
                if startup:
                    await self.discard_arguments(approval_id)
            elif approval.status == "rejected" and self._rejection_has_lapsed(approval):
                await self.discard_arguments(approval_id)

    def _rejection_has_lapsed(self, approval: ApprovalRecord) -> bool:
        """Whether a rejection is older than the approval TTL, after which its tool call's arguments go."""
        if approval.decided_at is None:
            return False
        return self._parse_timestamp(approval.decided_at) + self.approval_ttl <= datetime.now(UTC)

    async def list(
        self,
        *,
        status: ApprovalStatus | None = None,
        session_id: str | None = None,
    ) -> list[ApprovalRecord]:
        return await self._primary.list(status=status, session_id=session_id)

    async def get(self, approval_id: str) -> ApprovalRecord:
        return await self._primary.get(approval_id)

    async def create_or_reuse_pending(
        self,
        *,
        session_id: str,
        kind: ApprovalKind,
        reason: str,
        action: BrowserActionDecision,
        observation: dict | None = None,
        origin: ApprovalOrigin = "action",
        arguments: str | None = None,
    ) -> ApprovalRecord:
        """The pending approval of this action, or a new one; ``arguments`` is a tool call's whole arguments.

        They are written beside the record before it, so no listing shows an
        approval whose arguments cannot be read, and the record keeps their size
        and sha256 (the identical call reuses the record, and so its arguments).
        """
        async with self._lock:
            await self._sweep_arguments()
            owner = verified_operator()
            existing = await self._find_matching_pending(
                session_id=session_id, kind=kind, action=action, owner=owner, origin=origin
            )
            if existing is not None:
                return existing

            now = utc_now()
            approval = ApprovalRecord(
                id=uuid4().hex[:12],
                session_id=session_id,
                kind=kind,
                status="pending",
                created_at=now,
                updated_at=now,
                reason=reason,
                action=self._stored_action(action),
                observation=observation,
                owner=owner,
                origin=origin,
            )
            if arguments is not None:
                approval.arguments_size = len(arguments)
                # Off the event loop, as the gateway wrote them out: megabytes take a tenth of a second.
                approval.arguments_sha256 = await asyncio.to_thread(_text_sha256, arguments)
                await self.file_store.write_arguments(approval.id, arguments)
            await self._persist(approval)
            if approval.action is not action:
                self._prune_sensitive_texts()
                self._sensitive_texts[approval.id] = (action.text or "", time.monotonic())
            return approval

    async def approve(
        self,
        approval_id: str,
        comment: str | None = None,
        *,
        decided_via: ApprovalDecider = "operator",
    ) -> ApprovalRecord:
        return (await self.decide(approval_id, status="approved", comment=comment, decided_via=decided_via)).record

    async def reject(
        self,
        approval_id: str,
        comment: str | None = None,
        *,
        decided_via: ApprovalDecider = "operator",
    ) -> ApprovalRecord:
        return (await self.decide(approval_id, status="rejected", comment=comment, decided_via=decided_via)).record

    async def claim_execution(self, approval_id: str) -> ExecutionHold | None:
        """Spend an approved approval on the execution about to start, or refuse.

        An approval authorizes one action. It used to be reserved in memory and
        consumed only once the action had returned normally, so an action that
        went through and then failed (a later step, a captcha, a crash, a
        restart) left it approved, and the same approval ran the action again.
        The claim now marks it executed, persisted, before the action starts,
        so a second request carrying the same id is refused; the holder gives
        it back only when the action was refused before it changed anything
        (held_for_execution). Expiry is checked here, never after: an action
        that started in time is not refused once it has run.

        Returns the hold, which the caller releases in a finally; or None when
        the current task already holds it — an outer layer claimed the same
        approval and owns the execution. Each layer used to claim for itself,
        so the inner claim of a governed action failed and the action never ran.

        The write lands whatever happens to the caller, and the hold is made with
        it: a cancellation during the write is put on the hold (claim_cancelled),
        and held_for_execution ends the block as that cancellation. Raised there,
        it left the approval executed with no hold, no audit entry and nothing run.
        """
        if _active_hold(approval_id) is not None:
            return None
        async with self._lock:
            approval, cancelled = await self._mark_executed(approval_id)
            # Made with the write, under the lock: a decision that reads the record next finds the claim.
            hold = ExecutionHold(approval_id, approval.origin)
            hold.claim_cancelled = cancelled
            self._claims[approval_id] = hold
        _HELD_APPROVALS.set({**_HELD_APPROVALS.get(), approval_id: hold})
        return hold

    async def arguments(self, approval_id: str) -> str:
        """A tool call's whole arguments, while its approval is not final; KeyError once they are gone."""
        return await self.file_store.read_arguments(approval_id)

    async def discard_arguments(self, approval_id: str) -> None:
        """Remove a tool call's arguments once its approval is final; a failure is logged, never raised."""
        try:
            await self.file_store.delete_arguments(approval_id)
        except OSError:
            logger.warning("could not remove the arguments of approval %s", approval_id, exc_info=True)

    def revoke_execution(self, hold: ExecutionHold) -> None:
        """Stop a hold from counting, in every task carrying it: none of them can act under it now."""
        hold.active = False
        if self._claims.get(hold.approval_id) is hold:
            del self._claims[hold.approval_id]

    def release_execution(self, hold: ExecutionHold, *, spent: bool) -> None:
        """End a hold: a task still carrying it no longer counts as holding the approval."""
        self.revoke_execution(hold)
        held = _HELD_APPROVALS.get()
        if held.get(hold.approval_id) is hold:
            _HELD_APPROVALS.set({key: value for key, value in held.items() if key != hold.approval_id})
        if spent:
            # Kept through the claim, so that an approval given back can still be executed.
            self._sensitive_texts.pop(hold.approval_id, None)

    async def give_back(self, hold: ExecutionHold) -> ApprovalRecord | None:
        """Return a claimed approval: its action was refused before it changed anything.

        To approved, unless an operator rejected it while the claim waited: then
        to rejected, with their decision. None, with the record left executed,
        when only that reject made the ending give back and it has been withdrawn
        since: decided here, under the lock a withdraw takes, so the ending does
        not depend on whether the withdraw landed before or after the block ended.
        """
        async with self._lock:
            if hold.rejection is None and not hold.gives_back_on_its_own:
                return None
            approval = await self.get(hold.approval_id)
            now = utc_now()
            approval.status = "approved"
            approval.updated_at = now
            approval.executed_at = None
            if hold.rejection is not None:
                approval.status = "rejected"
                approval.decision_comment, approval.decided_via = hold.rejection
                approval.decided_at = now
                approval.approved_expires_at = None
                self._sensitive_texts.pop(hold.approval_id, None)
            await self._persist(approval)
        return approval

    async def record_given_back(self, approval: ApprovalRecord) -> None:
        """Audit a give-back once its record is written: the claim persisted executed, this says why it is not.

        A give-back that applied an operator's rejection names their decision,
        and gets the witness receipt their reject would have had.

        A failure is logged and goes no further. The give-back stands, and the
        refusal it came with is what the agent needs: an audit error in its place
        hid that the approval was approved again.
        """
        rejected = approval.status == "rejected"
        details: dict[str, Any] = {"kind": approval.kind, **approval_provenance(approval)}
        if rejected:
            details.update(comment=approval.decision_comment, decided_via=approval.decided_via)
        if self.audit is not None:
            try:
                await self.audit.append(
                    event_type="approval_given_back",
                    status=approval.status,
                    action="give_back",
                    session_id=approval.session_id,
                    approval_id=approval.id,
                    details=details,
                )
            except Exception:
                logger.exception("could not audit the give-back of approval %s", approval.id)
        if rejected and self.rejection_receipt is not None:
            try:
                await self.rejection_receipt(approval)
            except Exception:
                logger.exception("could not record the witness receipt of the rejection of approval %s", approval.id)

    async def record_cancelled_claim(self, hold: ExecutionHold) -> None:
        """Audit a claim whose caller was cancelled while it was written, in a block that does not give back.

        Nothing ran, and the approval stays spent (a governed tool call's): the
        trail would otherwise end at the approve. A failure is logged, as for a give-back.
        """
        if self.audit is None:
            return
        try:
            approval = await self.get(hold.approval_id)
            await self.audit.append(
                event_type="approval_claim_cancelled",
                status="spent",
                action="claim",
                session_id=approval.session_id,
                approval_id=approval.id,
                details={"kind": approval.kind, **approval_provenance(approval)},
            )
        except Exception:
            logger.exception("could not audit the cancelled claim of approval %s", hold.approval_id)

    async def record_rejection_not_applied(self, hold: ExecutionHold) -> None:
        """Audit an operator's reject that waited on a claim whose execution ended spent all the same.

        The give-back that applies it did not complete (its write failed, or the
        process is stopping), so the approval stays spent; the trail would
        otherwise end at the "deferred" entry. A failure is logged, as for a give-back.
        """
        if self.audit is None or hold.rejection is None:
            return
        comment, decided_via = hold.rejection
        try:
            approval = await self.get(hold.approval_id)
            await self.audit.append(
                event_type="approval_decision",
                status="not_applied",
                action="reject",
                session_id=approval.session_id,
                approval_id=approval.id,
                details={
                    "kind": approval.kind,
                    "comment": comment,
                    "decided_via": decided_via,
                    **approval_provenance(approval),
                },
            )
        except Exception:
            logger.exception("could not audit the rejection of approval %s that did not apply", hold.approval_id)

    async def mark_executed(self, approval_id: str) -> ApprovalRecord:
        async with self._lock:
            approval, cancelled = await self._mark_executed(approval_id)
        if cancelled:
            raise asyncio.CancelledError
        return approval

    async def _mark_executed(self, approval_id: str) -> tuple[ApprovalRecord, bool]:
        """Mark an approved approval executed, under the lock: the record, and whether the caller was cancelled.

        The write lands whatever happens to the caller (_run_through), and the
        cancellation is reported, not raised: one raised while the write was
        under way left the record executed and nothing to say so, an approval
        spent with nothing run.
        """
        approval = await self.get(approval_id)
        if approval.status == "executed":
            raise self.refusal_for_executed(approval)
        if approval.status != "approved":
            raise PermissionError(f"approval {approval_id} is not approved")
        self._ensure_not_expired(approval)
        now = utc_now()
        approval.status = "executed"
        approval.updated_at = now
        approval.executed_at = now
        _, cancelled = await _run_through(self._persist(approval))
        return approval, cancelled

    async def require_approved(
        self,
        *,
        approval_id: str,
        session_id: str,
        kind: ApprovalKind,
        action: BrowserActionDecision,
        origin: ApprovalOrigin = "action",
    ) -> ApprovalRecord:
        approval = await self.get(approval_id)
        if approval.session_id != session_id:
            raise PermissionError(f"approval {approval_id} does not belong to session {session_id}")
        # A controller-scope approval has no session whose owner could be asked,
        # so it is the asking operator's alone: another operator's identical call
        # hashes to the same action and would otherwise spend it. Answers like an
        # approval that does not exist, as the listing does.
        if session_id == CONTROLLER_SCOPE and not may_use(approval.owner):
            raise KeyError(approval_id)
        if approval.kind != kind:
            raise PermissionError(f"approval {approval_id} does not cover {kind}")
        if approval.origin != origin:
            raise PermissionError(
                f"approval {approval_id} is for {_ORIGIN_NAMES[approval.origin]}, not {_ORIGIN_NAMES[origin]}"
            )
        # Inside an execution that holds it, the claim has checked the status and
        # the expiry and spent it: a layer within accepts that, and an approval
        # that expires mid-run does not stop the run already under way.
        held = _active_hold(approval_id) is not None
        if approval.status == "executed" and not held:
            raise self.refusal_for_executed(approval)
        if approval.status != ("executed" if held else "approved"):
            raise PermissionError(f"approval {approval_id} is not approved")
        if not held:
            self._ensure_not_expired(approval)
        if not self._actions_match(approval.action, action):
            raise PermissionError(f"approval {approval_id} does not match the requested action")
        return approval

    def refusal_for_executed(self, approval: ApprovalRecord) -> PermissionError:
        """Why an executed approval cannot run again: spent, or held by an action that has not begun."""
        return executed_refusal(approval, self._claims.get(approval.id))

    async def decide(
        self,
        approval_id: str,
        *,
        status: ApprovalStatus,
        comment: str | None,
        decided_via: ApprovalDecider,
    ) -> Decision:
        """Approve or reject ``approval_id``, or, while its action holds it, put the decision on the claim."""
        async with self._lock:
            approval = await self.get(approval_id)
            if approval.status == "executed":
                claim = self._claims.get(approval_id)
                # Claimed by an action that has not begun, and that a refusal would give
                # back: an operator's reject waits on the claim and stops the action
                # (effect_started), and their approve withdraws a reject waiting there.
                # So too while the claim is being given back (no longer active): the
                # give-back reads the hold's rejection under this lock, after this.
                # A tool call's claim never gives back, so nothing could apply a reject to it.
                if decided_via != "agent" and claim is not None and claim.may_give_back and not claim.effect:
                    if status == "rejected":
                        claim.rejection = (comment, decided_via)
                        return Decision(approval, "deferred")
                    if claim.rejection is not None:
                        claim.rejection = None
                        if not claim.active and not claim.gives_back_on_its_own:
                            return Decision(approval, "withdrawn_spent")
                        refused = claim.stopped or not claim.active or claim.refusal is not None
                        return Decision(approval, "withdrawn_after_refusal" if refused else "withdrawn")
                raise _claim_conflict(approval, claim, decided_via=decided_via, status=status)
            # An operator may change their mind before the action runs; an
            # agent may not overturn a decision, or rejecting would only delay it.
            if decided_via == "agent" and approval.status != "pending":
                raise PermissionError(f"approval {approval_id} has already been {approval.status}")
            # Nor by asking again: the same action requested anew is a new
            # approval, and it stays the operator's to decide.
            if decided_via == "agent" and status == "approved" and await self._operator_rejected(approval):
                raise PermissionError(
                    f"an operator rejected this action in session {approval.session_id}; "
                    f"only an operator can approve {approval_id}"
                )
            if status == "approved":
                await self._refuse_what_is_no_longer_whole(approval)
            now = utc_now()
            approval.status = status
            approval.updated_at = now
            approval.decided_at = now
            approval.decision_comment = comment
            approval.decided_via = decided_via
            if status == "approved":
                approval.approved_expires_at = self._expiry_timestamp(now)
            else:
                approval.approved_expires_at = None
                self._sensitive_texts.pop(approval_id, None)
            await self._persist(approval)
        return Decision(approval)

    async def _refuse_what_is_no_longer_whole(self, approval: ApprovalRecord) -> None:
        """Refuse an approve of an approval missing what it was asked with: the agent asks again.

        A tool call's arguments, kept until the call spends the approval (a reject
        is not final): gone, the operator could not read what they approve. A
        sensitive text, dropped by a reject or a restart: gone, execute_approval
        could not type it.
        """
        if approval.arguments_size is not None and not await self.file_store.has_arguments(approval.id):
            raise ServerStateRefusal(
                f"approval {approval.id}: its arguments are no longer stored; the agent asks again"
            )
        if self._needs_held_text(approval) and self._held_text(approval) is None:
            raise ServerStateRefusal(f"approval {approval.id}: its text is no longer held; the agent asks again")

    async def _persist(self, approval: ApprovalRecord) -> None:
        await self.file_store.upsert(approval)
        if self.sqlite_store is not None and self._primary is self.sqlite_store:
            await self.sqlite_store.upsert(approval)

    async def _operator_rejected(self, approval: ApprovalRecord) -> bool:
        # Whatever the rejected record's origin: a rejection only ever restricts,
        # and one stored before origin existed loads as an action approval, so
        # comparing origins dropped every rejection an operator made before the upgrade.
        requested = approval.action.model_dump(exclude=UNBOUND_FIELDS)
        return any(
            other.kind == approval.kind
            and other.decided_via != "agent"
            and other.action.model_dump(exclude=UNBOUND_FIELDS) == requested
            for other in await self.list(status="rejected", session_id=approval.session_id)
            # Controller scope is shared by every operator: only the same
            # requester's rejection counts, not another operator's.
            if approval.session_id != CONTROLLER_SCOPE or other.owner == approval.owner
        )

    async def _find_matching_pending(
        self,
        *,
        session_id: str,
        kind: ApprovalKind,
        action: BrowserActionDecision,
        owner: str | None,
        origin: ApprovalOrigin,
    ) -> ApprovalRecord | None:
        for approval in await self.list(status="pending", session_id=session_id):
            # Exactly the requester's own pending approval in controller scope:
            # joining another operator's would hand them its decision, and hand
            # the other operator a request they never made.
            if session_id == CONTROLLER_SCOPE and approval.owner != owner:
                continue
            if approval.kind == kind and approval.origin == origin and self._actions_match(approval.action, action):
                return approval
        return None

    def _stored_action(self, action: BrowserActionDecision) -> BrowserActionDecision:
        """The action as an approval records it: sensitive text only as a keyed digest.

        Approvals are written to APPROVAL_ROOT, listed by GET /approvals and the
        approvals MCP tool, and shown to whoever approves. A `type` marked
        sensitive (a password, a card number) was stored and served verbatim,
        while the witness chain and action logs already redacted it.
        """
        if not (action.sensitive and action.text):
            return action
        digest = hmac.new(self._sensitive_text_key, action.text.encode("utf-8"), hashlib.sha256).hexdigest()
        return action.model_copy(update={"text": f"{SENSITIVE_TEXT_PREFIX}{digest}"})

    def _prune_sensitive_texts(self) -> None:
        cutoff = time.monotonic() - SENSITIVE_TEXT_RETENTION.total_seconds()
        for approval_id in [key for key, (_, kept_at) in self._sensitive_texts.items() if kept_at < cutoff]:
            del self._sensitive_texts[approval_id]

    @staticmethod
    def _needs_held_text(approval: ApprovalRecord) -> bool:
        """Whether the approval stores a sensitive text's digest, so running it takes the text this process holds."""
        action = approval.action
        return bool(action.sensitive and action.text and action.text.startswith(SENSITIVE_TEXT_PREFIX))

    def _held_text(self, approval: ApprovalRecord) -> str | None:
        self._prune_sensitive_texts()
        entry = self._sensitive_texts.get(approval.id)
        return entry[0] if entry is not None else None

    def executable_action(self, approval: ApprovalRecord) -> BrowserActionDecision:
        """The approved action with its sensitive text restored, for execute_approval."""
        action = approval.action
        if not self._needs_held_text(approval):
            return action
        text = self._held_text(approval)
        if text is None:
            raise PermissionError(
                f"approval {approval.id} is for sensitive text, which this controller process no longer holds "
                "(restarted, another worker, or older than 24 hours); run the action again to request a new approval"
            )
        return action.model_copy(update={"text": text})

    def _actions_match(self, stored: BrowserActionDecision, requested: BrowserActionDecision) -> bool:
        return stored.model_dump(exclude=UNBOUND_FIELDS) == self._stored_action(requested).model_dump(
            exclude=UNBOUND_FIELDS
        )

    def has_expired(self, approval: ApprovalRecord) -> bool:
        if approval.approved_expires_at is None:
            return False
        return self._parse_timestamp(approval.approved_expires_at) <= datetime.now(UTC)

    def _ensure_not_expired(self, approval: ApprovalRecord) -> None:
        if self.has_expired(approval):
            raise PermissionError(f"approval {approval.id} has expired")

    def _expiry_timestamp(self, decided_at: str) -> str:
        decided = self._parse_timestamp(decided_at)
        return (decided + self.approval_ttl).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
