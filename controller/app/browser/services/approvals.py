from __future__ import annotations

from typing import Any

from ...witness import WitnessApproval


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
        return [approval.model_dump() for approval in approvals]

    async def get(self, approval_id: str) -> dict[str, Any]:
        approval = await self.manager.approvals.get(approval_id)
        return approval.model_dump()

    async def approve(self, approval_id: str, comment: str | None = None) -> dict[str, Any]:
        approval = await self.manager.approvals.approve(approval_id, comment=comment)
        session = self.manager.sessions.get(approval.session_id)
        await self.manager.audit.append(
            event_type="approval_decision",
            status="approved",
            action="approve",
            session_id=approval.session_id,
            approval_id=approval.id,
            details={"kind": approval.kind, "comment": comment},
        )
        if session is not None:
            await self.manager._record_witness_receipt(
                session,
                event_type="approval",
                status="approved",
                action="approve",
                action_class="control",
                approval=WitnessApproval(
                    required=True,
                    approval_id=approval.id,
                    status=approval.status,
                    reason=approval.reason,
                ),
                target={"kind": approval.kind, "action": approval.action.action},
                metadata={"comment": comment},
            )
        return approval.model_dump()

    async def reject(self, approval_id: str, comment: str | None = None) -> dict[str, Any]:
        approval = await self.manager.approvals.reject(approval_id, comment=comment)
        session = self.manager.sessions.get(approval.session_id)
        await self.manager.audit.append(
            event_type="approval_decision",
            status="rejected",
            action="reject",
            session_id=approval.session_id,
            approval_id=approval.id,
            details={"kind": approval.kind, "comment": comment},
        )
        if session is not None:
            await self.manager._record_witness_receipt(
                session,
                event_type="approval",
                status="rejected",
                action="reject",
                action_class="control",
                approval=WitnessApproval(
                    required=True,
                    approval_id=approval.id,
                    status=approval.status,
                    reason=approval.reason,
                ),
                target={"kind": approval.kind, "action": approval.action.action},
                metadata={"comment": comment},
            )
        return approval.model_dump()

    async def execute(self, approval_id: str) -> dict[str, Any]:
        approval = await self.manager.approvals.get(approval_id)
        if approval.status != "approved":
            raise PermissionError(f"approval {approval_id} is not approved")

        decision = self.manager.approvals.executable_action(approval)
        if decision.action == "upload":
            execution = await self.manager.upload(
                approval.session_id,
                selector=decision.selector,
                element_id=decision.element_id,
                file_path=decision.file_path or "",
                approved=False,
                approval_id=approval.id,
            )
            latest = await self.manager.approvals.get(approval.id)
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
            details={"kind": approval.kind, "action": decision.action},
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
                target={"kind": approval.kind, "action": decision.action},
            )
        return {
            "approval": latest.model_dump(),
            "execution": execution,
        }
