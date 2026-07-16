"""Pillar 5 — Workflow routes (/workflows)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

workflow_router = APIRouter(prefix="/workflows", tags=["workflows"])


class WorkflowRunRequest(BaseModel):
    workflow_id: str
    steps: list[dict[str, Any]]
    initial_context: dict[str, Any] = {}


@workflow_router.post("/run")
async def workflow_run(body: WorkflowRunRequest, request: Request):
    engine = getattr(request.app.state, "workflow_engine", None)
    if engine is None:
        raise HTTPException(503, "Workflow engine not initialized")
    run = await engine.run(
        workflow_id=body.workflow_id,
        steps=body.steps,
        initial_context=body.initial_context,
    )
    return {
        "run_id": run.run_id,
        "status": run.status.value,
        "step_statuses": {k: v.value for k, v in run.step_statuses.items()},
        "context": run.context,
        "error": run.error,
    }


@workflow_router.get("/runs")
async def workflow_list_runs(request: Request, workflow_id: str = ""):
    engine = getattr(request.app.state, "workflow_engine", None)
    if engine is None:
        raise HTTPException(503, "Workflow engine not initialized")
    return {"runs": engine.list_runs(workflow_id=workflow_id)}


@workflow_router.get("/runs/{run_id}")
async def workflow_get_run(run_id: str, request: Request):
    engine = getattr(request.app.state, "workflow_engine", None)
    if engine is None:
        raise HTTPException(503, "Workflow engine not initialized")
    runs = engine.list_runs()
    for run in runs:
        if run.get("run_id") == run_id:
            return run
    raise HTTPException(404, f"Run {run_id!r} not found")
