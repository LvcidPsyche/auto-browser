from __future__ import annotations

from typing import Any

from ...approval_display import approval_provenance
from ...approvals import CONTROLLER_SCOPE, held_for_execution
from ...auth_policy import has_named_credentials
from ...models import ApprovalDecider, ApprovalRecord
from ...session_ownership import may_use
from ...witness import WitnessApproval


def _owner_from(approval: ApprovalRecord, lookup: tuple[bool, str | None] | None) -> str | None:
    """Who may use an approval, given its session's (known, owner) lookup (None: it has no session).

    Its session's owner, when the session is known: a known session nobody owns
    stays open to everyone. With no session, or one with no record (its record
    lost while the approval stayed), the operator who asked, as the approval
    recorded; read as unowned, anyone could use another operator's approval.
    """
    if lookup is not None and lookup[0]:
        return lookup[1]
    return approval.owner


def _usable(approval: ApprovalRecord, lookup: tuple[bool, str | None] | None, *, named: bool) -> bool:
    """Whether the current operator may use an approval, given its session's lookup (see _owner_from).

    An approval whose session has no record and whose record names nobody has no
    owner to check: under named credentials it is refused, as
    BrowserManager.session_accessible refuses such a session id.
    """
    if named and lookup is not None and not lookup[0] and approval.owner is None:
        return False
    return may_use(_owner_from(approval, lookup))


class BrowserApprovalService:
    def __init__(self, manager: Any) -> None:
        self.manager = manager

    async def list(
        self,
        *,
        status: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        approvals = await self.manager.approvals.list(status=status, session_id=session_id)
        named = has_named_credentials(self.manager.settings)
        # One lookup per session, not per approval: a session can hold many.
        lookups = {
            session: await self.manager.session_owner_lookup(session)
            for session in {item.session_id for item in approvals if item.session_id != CONTROLLER_SCOPE}
        }
        return [
            approval.model_dump()
            for approval in approvals
            if _usable(approval, lookups.get(approval.session_id), named=named)
        ]

    async def get(self, approval_id: str) -> dict[str, Any]:
        return (await self._accessible(approval_id)).model_dump()

    async def arguments(self, approval_id: str) -> dict[str, Any]:
        """A tool call's whole arguments, for whoever may read its approval (KeyError otherwise, as get)."""
        approval = await self._accessible(approval_id)
        return {
            "approval_id": approval.id,
            "size": approval.arguments_size,
            "sha256": approval.arguments_sha256,
            "arguments": await self.manager.approvals.arguments(approval.id),
        }

    async def _may_use(self, approval: ApprovalRecord) -> bool:
        """Whether the current operator may use an approval: see _usable."""
        lookup = None
        if approval.session_id != CONTROLLER_SCOPE:
            lookup = await self.manager.session_owner_lookup(approval.session_id)
        return _usable(approval, lookup, named=has_named_credentials(self.manager.settings))

    async def _accessible(self, approval_id: str) -> ApprovalRecord:
        """An approval the current operator may not use (see _usable) answers as if it did not exist."""
        approval = await self.manager.approvals.get(approval_id)
        if not await self._may_use(approval):
            raise KeyError(approval_id)
        return approval

    async def approve(
        self,
        approval_id: str,
        comment: str | None = None,
        *,
        decided_via: ApprovalDecider = "operator",
    ) -> dict[str, Any]:
        await self._accessible(approval_id)
        decision = await self.manager.approvals.decide(
            approval_id, status="approved", comment=comment, decided_via=decided_via
        )
        approval = decision.record
        if decision.on_claim is not None:
            # Claimed by an action that has not begun: the approve withdrew the
            # operator's reject that waited on it (ApprovalStore.decide).
            await self.manager.audit.append(
                event_type="approval_decision",
                status="withdrawn",
                action="approve",
                session_id=approval.session_id,
                approval_id=approval.id,
                details={
                    "kind": approval.kind,
                    "comment": comment,
                    "decided_via": decided_via,
                    **approval_provenance(approval),
                },
            )
            if decision.on_claim == "withdrawn_spent":
                message = (
                    f"approval {approval.id}: the action that claimed it ended before it began, with an error "
                    "that gives no approval back: the rejection waiting on it is withdrawn, and the approval stays "
                    "spent; the agent must ask for a new approval"
                )
            elif decision.on_claim == "withdrawn_after_refusal" and self.manager.approvals.has_expired(approval):
                # Given back expired: the agent's note says so, and a retry with the id is refused.
                message = (
                    f"approval {approval.id}: the action that claimed it was already refused before it began, and "
                    "the approval has expired: the rejection waiting on it is withdrawn, and the agent must ask "
                    "for a new approval"
                )
            elif decision.on_claim == "withdrawn_after_refusal":
                message = (
                    f"approval {approval.id}: the action that claimed it was already refused before it began, so "
                    "the rejection waiting on it is withdrawn and the approval goes back to approved; the agent "
                    "may retry with this approval_id"
                )
            else:
                message = (
                    f"approval {approval.id} is claimed by an action that has not begun: the rejection waiting "
                    "on it is withdrawn, and the action may start"
                )
            if decision.on_claim == "withdrawn_after_refusal":
                # The chain already holds the action blocked on the rejection, and the give-back writes no
                # receipt for an approval it returns approved: without this, the next run under the same
                # approval followed a rejection with nothing between. The record goes back to approved.
                await self._decision_receipt(
                    approval.model_copy(update={"status": "approved"}),
                    action="approve",
                    comment=comment,
                    decided_via=decided_via,
                )
            return {**approval.model_dump(), "message": message}
        await self.manager.audit.append(
            event_type="approval_decision",
            status="approved",
            action="approve",
            session_id=approval.session_id,
            approval_id=approval.id,
            details={
                "kind": approval.kind,
                "comment": comment,
                "decided_via": decided_via,
                **approval_provenance(approval),
            },
        )
        await self._decision_receipt(approval, action="approve", comment=comment, decided_via=decided_via)
        return approval.model_dump()

    async def reject(
        self,
        approval_id: str,
        comment: str | None = None,
        *,
        decided_via: ApprovalDecider = "operator",
    ) -> dict[str, Any]:
        await self._accessible(approval_id)
        approval = await self.manager.approvals.reject(approval_id, comment=comment, decided_via=decided_via)
        if approval.status == "executed":
            # Claimed by an action that has not begun (ApprovalStore._transition).
            await self.manager.audit.append(
                event_type="approval_decision",
                status="deferred",
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
            return {
                **approval.model_dump(),
                "message": (
                    f"approval {approval.id} is claimed by an action: the rejection stops the action unless it "
                    "has begun, and the approval then becomes rejected"
                ),
            }
        await self.manager.audit.append(
            event_type="approval_decision",
            status="rejected",
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
        await self._decision_receipt(approval, action="reject", comment=comment, decided_via=decided_via)
        return approval.model_dump()

    async def rejection_receipt(self, approval: ApprovalRecord) -> None:
        """The witness receipt of an operator's reject that a give-back applied (ApprovalStore.record_given_back).

        The reject came while the action held the approval, and was recorded
        only as deferred; this is the receipt it gets once it took effect.
        """
        await self._decision_receipt(
            approval, action="reject", comment=approval.decision_comment, decided_via=approval.decided_via
        )

    async def _decision_receipt(
        self,
        approval: ApprovalRecord,
        *,
        action: str,
        comment: str | None,
        decided_via: ApprovalDecider | None,
    ) -> None:
        """Record a decision on ``approval`` in its session's witness chain, if the session is live."""
        session = self.manager.sessions.get(approval.session_id)
        if session is None:
            return
        await self.manager._record_witness_receipt(
            session,
            event_type="approval",
            status=approval.status,
            action=action,
            action_class="control",
            approval=WitnessApproval(
                required=True,
                approval_id=approval.id,
                status=approval.status,
                reason=approval.reason,
            ),
            target={"kind": approval.kind, "action": approval.action.action, **approval_provenance(approval)},
            metadata={"comment": comment, "decided_via": decided_via},
        )

    async def execute(self, approval_id: str) -> dict[str, Any]:
        approval = await self._accessible(approval_id)
        if approval.status == "executed":
            raise self.manager.approvals.refusal_for_executed(approval)
        if approval.status != "approved":
            raise PermissionError(f"approval {approval_id} is not approved")
        if approval.session_id == CONTROLLER_SCOPE:
            # It authorises a tool call the agent makes itself, passing the id
            # back; there is no session for the server to replay it in.
            raise PermissionError(
                f"approval {approval_id} is a controller-wide approval: the agent runs the approved tool "
                "call again with this approval_id; it cannot be executed here"
            )
        if approval.origin == "tool_call":
            # A governed tool call's stand-in names the call, it is not an
            # action: there is nothing here to run it with.
            raise PermissionError(
                f"approval {approval_id} is for a tool call: the agent runs the approved tool call again with "
                "this approval_id; it cannot be executed here"
            )
        if approval.action.action == "request_human_takeover":
            # A takeover approved as an action: a stand-in stored before origin
            # existed. No tool call can spend it either (it is not a tool_call
            # approval), so the agent is not sent there with its id.
            raise PermissionError(
                f"approval {approval_id} is a request_human_takeover approval stored before approvals recorded "
                "their origin: nothing can run it; repeat the tool call it was for without approval_id to ask "
                "for a new approval"
            )
        if approval.action.action == "done":
            # Queued before done was refused as an action: nothing can run it.
            raise PermissionError(f"approval {approval_id} is for done, which ends an agent's run: nothing can run it")

        decision = self.manager.approvals.executable_action(approval)
        if decision.action == "upload":
            # The file first, as browser.execute_action and the agent loop check it: a
            # missing file is no refusal before the effect, and would spend the approval.
            session = await self.manager.get_session(approval.session_id)
            await self.manager.uploads.safe_path(decision.file_path or "", session=session)
        # Claimed here, for the approval's own kind: execute_decision only
        # claims the kinds the runtime requires, so a governed `write` approval
        # executed through this endpoint was never consumed and ran again on
        # every call.
        async with held_for_execution(self.manager.approvals, approval.id, may_give_back=True):
            if decision.action == "upload":
                execution = await self.manager.upload(
                    approval.session_id,
                    selector=decision.selector,
                    element_id=decision.element_id,
                    file_path=decision.file_path or "",
                    approved=False,
                    approval_id=approval.id,
                )
            else:
                execution = await self.manager.execute_decision(
                    approval.session_id,
                    decision,
                    approval_id=approval.id,
                )
        latest = await self.manager.approvals.get(approval.id)
        await self.manager.audit.append(
            event_type="approval_executed",
            status="ok",
            action="execute_approval",
            session_id=approval.session_id,
            approval_id=approval.id,
            details={"kind": approval.kind, "action": decision.action, **approval_provenance(approval)},
        )
        session = self.manager.sessions.get(approval.session_id)
        if session is not None:
            await self.manager._record_witness_receipt(
                session,
                event_type="approval",
                status="executed",
                action="execute_approval",
                action_class="control",
                approval=WitnessApproval(
                    required=True,
                    approval_id=approval.id,
                    status=latest.status,
                    reason=approval.reason,
                ),
                target={"kind": approval.kind, "action": decision.action, **approval_provenance(approval)},
            )
        return {
            "approval": latest.model_dump(),
            "execution": execution,
        }
