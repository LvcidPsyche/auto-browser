from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ...audit import get_current_operator
from ...models import WitnessRemoteState
from ...utils import utc_now
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


class BrowserWitnessService:
    """Encapsulates Witness policy context and receipt delivery helpers."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager

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
    ) -> None:
        if not self.manager.settings.witness_enabled:
            return
        policy = outcome or WitnessPolicyOutcome(profile=session.protection_mode)  # type: ignore[arg-type]
        payload = {
            "profile": session.protection_mode,  # type: ignore[arg-type]
            "event_type": event_type,
            "status": status,
            "action": action,
            "action_class": action_class,  # type: ignore[arg-type]
            "session_id": session.id,
            "risk_category": risk_category,
            "operator": get_current_operator(),
            "approval": approval or WitnessApproval(),
            "target": self.manager.witness_policy.redact_target(target or {}, evidence_mode=policy.evidence_mode),
            "concerns": policy.concerns,
            "evidence_mode": policy.evidence_mode,
            "evidence": WitnessEvidence(
                before=before if policy.evidence_mode == "standard" else None,
                after=after if policy.evidence_mode == "standard" else None,
                verification=verification,
                artifacts={},
            ),
            "metadata": metadata or {},
        }
        recorded = await self.manager.witness.record(session.id, **payload)
        if not self.manager.witness_remote.enabled:
            return
        attempted_at = utc_now()
        try:
            await self.manager.witness_remote.record(
                session.id,
                recorded.model_dump(
                    mode="json",
                    exclude={"receipt_id", "scope", "chain_prev_hash", "chain_hash"},
                ),
            )
        except Exception as exc:
            session.witness_remote_state.status = "failed"
            session.witness_remote_state.last_attempted_at = attempted_at
            session.witness_remote_state.last_error = f"Hosted Witness delivery failed for {action}."
            logger.warning(
                "witness remote delivery failed for session %s action %s: %s",
                session.id,
                action,
                exc,
            )
            return
        session.witness_remote_state.status = "delivered"
        session.witness_remote_state.last_attempted_at = attempted_at
        session.witness_remote_state.last_delivered_at = attempted_at
        session.witness_remote_state.last_error = None

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
        payload = dict(session.pending_witness_context or {})
        session.pending_witness_context = None
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
            witness_context.get("sensitive_input")
            or target.get("text_redacted")
            or target.get("sensitive")
        )
        stores_auth_material = bool(
            witness_context.get("stores_auth_material")
            or action_name in {"save_auth_profile", "save_storage_state"}
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
