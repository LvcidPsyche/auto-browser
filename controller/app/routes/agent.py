from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from ..agent_jobs import AgentJobQueueFull
from ..models import AgentResumeRequest, AgentRunRequest, AgentStepRequest
from ..utils import error_text
from ._utils import queue_full, typed_route_errors

logger = logging.getLogger(__name__)


def create_agent_router(*, manager: Any, orchestrator: Any, job_queue: Any) -> APIRouter:
    router = APIRouter()

    @router.get("/agent/jobs")
    async def list_agent_jobs(status: str | None = None, session_id: str | None = None) -> list[dict[str, Any]]:
        with typed_route_errors(logger, "agent jobs listing failed"):
            return await job_queue.list_jobs(status=status, session_id=session_id)

    @router.get("/agent/jobs/{job_id}")
    async def get_agent_job(job_id: str) -> dict[str, Any]:
        with typed_route_errors(logger, "get agent job failed for job %s", job_id):
            return await job_queue.get_job(job_id)

    @router.post("/sessions/{session_id}/agent/step")
    async def run_agent_step(session_id: str, payload: AgentStepRequest) -> dict[str, Any]:
        with typed_route_errors(logger, "agent step failed for session %s", session_id):
            try:
                result = await orchestrator.step(
                    session_id=session_id,
                    provider_name=payload.provider,
                    goal=payload.goal,
                    observation_limit=payload.observation_limit,
                    context_hints=payload.context_hints,
                    approval_id=payload.approval_id,
                    provider_model=payload.provider_model,
                    workflow_profile=payload.workflow_profile,
                )
                status_code = 200 if result.status != "error" else (result.error_code or 502)
                if status_code != 200:
                    raise HTTPException(status_code=status_code, detail=result.model_dump())
                return result.model_dump()
            except KeyError:
                raise HTTPException(status_code=404, detail="Unknown session") from None
            except RuntimeError:
                raise HTTPException(status_code=503, detail="Service unavailable") from None

    @router.post("/sessions/{session_id}/agent/jobs/step", status_code=202)
    async def enqueue_agent_step(session_id: str, payload: AgentStepRequest) -> dict[str, Any]:
        with typed_route_errors(logger, "enqueue agent step failed for session %s", session_id):
            await manager.get_session(session_id)
            try:
                return await job_queue.enqueue_step(session_id, payload)
            except AgentJobQueueFull:  # and only that: any other RuntimeError is a fault, answered 500 and logged
                raise queue_full(logger, "a step for session", session_id) from None

    @router.post("/sessions/{session_id}/agent/run")
    async def run_agent_loop(session_id: str, payload: AgentRunRequest) -> dict[str, Any]:
        with typed_route_errors(logger, "agent run failed for session %s", session_id):
            try:
                result = await orchestrator.run(
                    session_id=session_id,
                    provider_name=payload.provider,
                    goal=payload.goal,
                    max_steps=payload.max_steps,
                    observation_limit=payload.observation_limit,
                    context_hints=payload.context_hints,
                    approval_id=payload.approval_id,
                    provider_model=payload.provider_model,
                    workflow_profile=payload.workflow_profile,
                    resume_checkpoints=payload.resume_checkpoints,
                )
                return result.model_dump()
            except KeyError:
                raise HTTPException(status_code=404, detail="Unknown session") from None
            except RuntimeError:
                raise HTTPException(status_code=503, detail="Service unavailable") from None

    @router.post("/sessions/{session_id}/agent/jobs/run", status_code=202)
    async def enqueue_agent_run(session_id: str, payload: AgentRunRequest) -> dict[str, Any]:
        with typed_route_errors(logger, "enqueue agent run failed for session %s", session_id):
            await manager.get_session(session_id)
            try:
                return await job_queue.enqueue_run(session_id, payload)
            except AgentJobQueueFull:
                raise queue_full(logger, "a run for session", session_id) from None

    @router.post("/agent/jobs/{job_id}/resume", status_code=202)
    async def resume_agent_job(job_id: str, payload: AgentResumeRequest | None = None) -> dict[str, Any]:
        request = payload or AgentResumeRequest()
        with typed_route_errors(logger, "resume failed for agent job %s", job_id):
            try:
                return await job_queue.resume_job(job_id, max_steps=request.max_steps)
            except KeyError:
                raise HTTPException(status_code=404, detail="Unknown job") from None
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=error_text(exc)) from None
            except AgentJobQueueFull:
                raise queue_full(logger, "the resume of job", job_id) from None

    @router.post("/agent/jobs/{job_id}/discard")
    async def discard_agent_job(job_id: str) -> dict[str, Any]:
        with typed_route_errors(logger, "discard failed for agent job %s", job_id):
            try:
                return await job_queue.discard_job(job_id)
            except KeyError:
                raise HTTPException(status_code=404, detail="Unknown job") from None
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=error_text(exc)) from None

    @router.post("/agent/jobs/{job_id}/cancel", status_code=202)
    async def cancel_agent_job(job_id: str) -> dict[str, Any]:
        with typed_route_errors(logger, "cancel failed for agent job %s", job_id):
            try:
                return await job_queue.cancel_job(job_id)
            except KeyError:
                raise HTTPException(status_code=404, detail="Unknown job") from None
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=error_text(exc)) from None

    return router
