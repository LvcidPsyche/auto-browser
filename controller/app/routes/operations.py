from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from ..action_errors import BROWSER_UNREACHABLE_DETAIL, BrowserUnreachableError
from ..approvals import ServerStateRefusal
from ..auth_state import AuthStateDecryptError
from ..browser.services.session_creation import SessionLimitError
from ..cron_service import CronJobDisabledError
from ..models import ApprovalDecisionRequest
from ..tool_inputs import CreateCronJobInput, CreateProxyPersonaInput, TriggerCronJobInput
from ._utils import bounded_limit, cron_store_errors, persona_file_errors, typed_route_errors

logger = logging.getLogger(__name__)


# The cap's own text names the job owner's live session ids (check_limit runs as the owner), and the trigger checks
# only the job's webhook key, which another operator may hold: the trigger names no session (W2-14).
CRON_TRIGGER_SESSION_LIMIT_DETAIL = (
    "Session limit reached: the job's run could not start a session. Close a session or raise MAX_SESSIONS."
)


def create_operations_router(
    *,
    manager: Any,
    proxy_store: Any,
    cron_service: Any,
) -> APIRouter:
    router = APIRouter()

    @router.get("/remote-access")
    async def get_remote_access(session_id: str | None = None) -> dict[str, Any]:
        with typed_route_errors(logger, "remote access info failed"):
            if session_id and session_id not in manager.sessions:
                try:
                    record = await manager.get_session_record(session_id)
                    return record["remote_access"]
                except KeyError:
                    raise HTTPException(status_code=404, detail="Unknown session") from None
            return manager.get_remote_access_info(session_id)

    @router.get("/audit/events")
    async def list_audit_events(
        limit: int = 100,
        session_id: str | None = None,
        event_type: str | None = None,
        operator_id: str | None = None,
    ) -> list[dict[str, Any]]:
        with typed_route_errors(logger, "audit events listing failed"):
            return await manager.list_audit_events(
                limit=bounded_limit(limit, 500),
                session_id=session_id,
                event_type=event_type,
                operator_id=operator_id,
            )

    @router.get("/approvals")
    async def list_approvals(status: str | None = None, session_id: str | None = None) -> list[dict[str, Any]]:
        with typed_route_errors(logger, "approvals listing failed"):
            return await manager.list_approvals(status=status, session_id=session_id)

    @router.get("/approvals/{approval_id}")
    async def get_approval(approval_id: str) -> dict[str, Any]:
        with typed_route_errors(logger, "get approval failed for approval %s", approval_id):
            return await manager.get_approval(approval_id)

    # A tool call's whole arguments, which the approval's reason previews; the record's access rule.
    @router.get("/approvals/{approval_id}/arguments")
    async def get_approval_arguments(approval_id: str) -> dict[str, Any]:
        with typed_route_errors(logger, "approval arguments failed for approval %s", approval_id):
            return await manager.approval_arguments(approval_id)

    # A decision refused for what the server knows (an approval its action holds, or one whose
    # arguments or text are gone) answers with why: ServerStateRefusal names nothing an agent
    # wrote. Any other refusal keeps the fixed text.
    @router.post("/approvals/{approval_id}/approve")
    async def approve_approval(approval_id: str, payload: ApprovalDecisionRequest) -> dict[str, Any]:
        try:
            return await manager.approve(approval_id, comment=payload.comment)
        except ServerStateRefusal as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except PermissionError:
            raise HTTPException(status_code=409, detail="Conflict") from None

    @router.post("/approvals/{approval_id}/reject")
    async def reject_approval(approval_id: str, payload: ApprovalDecisionRequest) -> dict[str, Any]:
        try:
            return await manager.reject(approval_id, comment=payload.comment)
        except ServerStateRefusal as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except PermissionError:
            raise HTTPException(status_code=409, detail="Conflict") from None

    @router.post("/approvals/{approval_id}/execute")
    async def execute_approval(approval_id: str) -> dict[str, Any]:
        with typed_route_errors(logger, "approval execution failed for approval %s", approval_id):
            try:
                return await manager.execute_approval(approval_id)
            except PermissionError:
                raise HTTPException(status_code=409, detail="Conflict") from None

    @router.get("/pii-scrubber")
    async def get_pii_scrubber() -> dict[str, Any]:
        with typed_route_errors(logger, "pii scrubber status failed"):
            return manager.get_pii_scrubber_status()

    @router.get("/proxy-personas")
    async def list_proxy_personas() -> list[Any]:
        with persona_file_errors():
            return proxy_store.list_personas()

    @router.post("/proxy-personas")
    async def set_proxy_persona(payload: CreateProxyPersonaInput) -> dict[str, Any]:
        try:
            with persona_file_errors():
                return proxy_store.set_persona(
                    payload.name,
                    server=payload.server,
                    username=payload.username,
                    password=payload.password,
                    description=payload.description,
                )
        except PermissionError:
            raise HTTPException(status_code=409, detail="Conflict") from None
        except (ValueError, RuntimeError):
            raise HTTPException(status_code=400, detail="Invalid request") from None

    @router.get("/proxy-personas/{name}")
    async def get_proxy_persona(name: str) -> dict[str, Any]:
        with persona_file_errors():
            try:
                return proxy_store.get_persona(name)
            except KeyError:
                raise HTTPException(status_code=404, detail="Not found") from None

    @router.delete("/proxy-personas/{name}")
    async def delete_proxy_persona(name: str) -> dict[str, Any]:
        with persona_file_errors():
            deleted = proxy_store.delete_persona(name)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Proxy persona not found: {name!r}")
        return {"deleted": True, "name": name}

    @router.get("/crons")
    async def list_cron_jobs() -> list[Any]:
        with cron_store_errors():
            return await cron_service.list_jobs()

    @router.post("/crons")
    async def create_cron_job(payload: CreateCronJobInput) -> dict[str, Any]:
        try:
            with cron_store_errors():
                return await cron_service.create_job(
                    name=payload.name,
                    goal=payload.goal,
                    provider=payload.provider,
                    schedule=payload.schedule,
                    start_url=payload.start_url,
                    auth_profile=payload.auth_profile,
                    proxy_persona=payload.proxy_persona,
                    max_steps=payload.max_steps,
                    enabled=payload.enabled,
                    webhook_enabled=payload.webhook_enabled,
                    # REST callers are operators: the profile the body names, fast by default.
                    workflow_profile=payload.workflow_profile,
                )
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid request") from None

    @router.get("/crons/{job_id}")
    async def get_cron_job(job_id: str) -> dict[str, Any]:
        with cron_store_errors():
            return await cron_service.get_job(job_id)

    @router.delete("/crons/{job_id}")
    async def delete_cron_job(job_id: str) -> dict[str, Any]:
        with cron_store_errors():
            deleted = await cron_service.delete_job(job_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Cron job not found: {job_id}")
        return {"deleted": True, "job_id": job_id}

    @router.post("/crons/{job_id}/trigger")
    async def trigger_cron_job_via_webhook(job_id: str, request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("trigger body must be a JSON object")
            # The path names the job; a job_id in the body must not redirect the
            # trigger to a different one.
            payload = TriggerCronJobInput.model_validate({**body, "job_id": job_id})
            # The run's session reads the job's proxy persona.
            with cron_store_errors(), persona_file_errors():
                return await cron_service.trigger_via_webhook(payload.job_id, payload.webhook_key or "")
        except KeyError:
            raise HTTPException(status_code=404, detail="Not found") from None
        except PermissionError:
            raise HTTPException(status_code=403, detail="Not permitted") from None
        except CronJobDisabledError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except BrowserUnreachableError:  # the run's session could not reach its browser (create_session)
            raise HTTPException(status_code=503, detail=BROWSER_UNREACHABLE_DETAIL) from None
        except SessionLimitError:  # the run's session (CB1-8), with a detail that names none (W2-14)
            raise HTTPException(status_code=409, detail=CRON_TRIGGER_SESSION_LIMIT_DETAIL) from None
        except AuthStateDecryptError as exc:
            # A fixed text that says what to check; it names no path.
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except (ValidationError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid request") from None

    return router
