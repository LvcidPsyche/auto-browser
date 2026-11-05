from __future__ import annotations

from typing import Any

from ...action_records import write_record
from ...utils import utc_now


class BrowserTakeoverService:
    def __init__(self, manager: Any) -> None:
        self.manager = manager

    async def request(self, session_id: str, reason: str) -> dict[str, Any]:
        manager = self.manager
        session = await manager.get_session(session_id)
        payload = {
            "session": await manager._session_summary(session),
            "reason": reason,
            "takeover_url": manager._current_takeover_url(session),
            "remote_access": manager.remote_access.session_info(session),
            "message": (
                "Human takeover requested. Open the noVNC URL to continue visually."
                if session.isolation_mode == "docker_ephemeral"
                else "Human takeover requested. Open the noVNC URL to continue visually. In this POC, takeover is global to the single browser desktop."
            ),
        }
        line = {"timestamp": utc_now(), "action": "request_human_takeover", **payload}

        async def writes() -> None:
            await manager._append_jsonl(session.artifact_dir / "actions.jsonl", line)
            await manager.audit.append(
                event_type="takeover_requested",
                status="ok",
                action="request_human_takeover",
                session_id=session.id,
                details={"reason": reason},
            )
            await manager._record_witness_receipt(
                session,
                event_type="control",
                status="ok",
                action="request_human_takeover",
                action_class="control",
                target={"reason": reason},
            )

        # One unit a cancellation does not cut short, after the session's record still being written (an action's
        # behind its cancellation), as an action's record is.
        await write_record(session, writes)
        payload["session"] = await manager._session_summary(session)
        await manager._persist_session(session, status="active")
        return payload
