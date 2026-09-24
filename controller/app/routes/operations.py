from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from ..approvals import ApprovalRequiredError
from ..models import ApprovalDecisionRequest
from ..tool_inputs import CreateCronJobInput, CreateProxyPersonaInput, TriggerCronJobInput
from ._utils import internal_error

logger = logging.getLogger(__name__)


def create_operations_router(
    *,
    manager: Any,
    proxy_store: Any,
    cron_service: Any,
) -> APIRouter:
    router = APIRouter()

    @router.get("/remote-access")
    async def get_remote_access(session_id: str | None = None) -> dict[str, Any]:
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
        return await manager.list_audit_events(
            limit=max(1, min(limit, 500)),
            session_id=session_id,
            event_type=event_type,
            operator_id=operator_id,
        )

    @router.get("/approvals")
    async def list_approvals(status: str | None = None, session_id: str | None = None) -> list[dict[str, Any]]:
        return await manager.list_approvals(status=status, session_id=session_id)

    @router.get("/approvals/{approval_id}")
    async def get_approval(approval_id: str) -> dict[str, Any]:
        return await manager.get_approval(approval_id)

    @router.post("/approvals/{approval_id}/approve")
    async def approve_approval(approval_id: str, payload: ApprovalDecisionRequest) -> dict[str, Any]:
        try:
            return await manager.approve(approval_id, comment=payload.comment)
        except PermissionError:
            raise HTTPException(status_code=409, detail="Conflict") from None

    @router.post("/approvals/{approval_id}/reject")
    async def reject_approval(approval_id: str, payload: ApprovalDecisionRequest) -> dict[str, Any]:
        try:
            return await manager.reject(approval_id, comment=payload.comment)
        except PermissionError:
            raise HTTPException(status_code=409, detail="Conflict") from None

    @router.post("/approvals/{approval_id}/execute")
    async def execute_approval(approval_id: str) -> dict[str, Any]:
        try:
            return await manager.execute_approval(approval_id)
        except ApprovalRequiredError:
            raise
        except PermissionError:
            raise HTTPException(status_code=409, detail="Conflict") from None
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid request") from None
        except Exception:
            raise internal_error(logger, "approval execution failed for approval %s", approval_id) from None

    @router.get("/pii-scrubber")
    async def get_pii_scrubber() -> dict[str, Any]:
        return manager.get_pii_scrubber_status()

    @router.get("/proxy-personas")
    async def list_proxy_personas() -> list[Any]:
        return proxy_store.list_personas()

    @router.post("/proxy-personas")
    async def set_proxy_persona(payload: CreateProxyPersonaInput) -> dict[str, Any]:
        try:
            return proxy_store.set_persona(
                payload.name,
                server=payload.server,
                username=payload.username,
                password=payload.password,
                description=payload.description,
            )
        except (ValueError, RuntimeError):
            raise HTTPException(status_code=400, detail="Invalid request") from None

    @router.get("/proxy-personas/{name}")
    async def get_proxy_persona(name: str) -> dict[str, Any]:
        try:
            return proxy_store.get_persona(name)
        except KeyError:
            raise HTTPException(status_code=404, detail="Not found") from None

    @router.delete("/proxy-personas/{name}")
    async def delete_proxy_persona(name: str) -> dict[str, Any]:
        deleted = proxy_store.delete_persona(name)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Proxy persona not found: {name!r}")
        return {"deleted": True, "name": name}

    @router.get("/crons")
    async def list_cron_jobs() -> list[Any]:
        return await cron_service.list_jobs()

    @router.post("/crons")
    async def create_cron_job(payload: CreateCronJobInput) -> dict[str, Any]:
        try:
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
            )
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid request") from None

    @router.get("/crons/{job_id}")
    async def get_cron_job(job_id: str) -> dict[str, Any]:
        return await cron_service.get_job(job_id)

    @router.delete("/crons/{job_id}")
    async def delete_cron_job(job_id: str) -> dict[str, Any]:
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
            return await cron_service.trigger_via_webhook(payload.job_id, payload.webhook_key or "")
        except KeyError:
            raise HTTPException(status_code=404, detail="Not found") from None
        except PermissionError:
            raise HTTPException(status_code=403, detail="Not permitted") from None
        except (ValidationError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid request") from None

    return router
