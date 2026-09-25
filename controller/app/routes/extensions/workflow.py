"""Pillar 5 — Workflow routes (/workflows)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

workflow_router = APIRouter(prefix="/workflows", tags=["workflows"])


class WorkflowStepInput(BaseModel):
    """One step, validated before the run starts.

    Steps used to be raw dicts splatted into WorkflowStep: an unknown or
    missing key was a TypeError and a 500, a non-numeric retry_max failed only
    mid-run, and an unbounded retry_max with exponential backoff could park a
    request for hours.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=200)
    action: str = Field(min_length=1, max_length=200)
    params: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    retry_max: int = Field(default=2, ge=0, le=10)
    retry_backoff_seconds: float = Field(default=3.0, ge=0, le=60)
    timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    condition: str = ""


class WorkflowRunRequest(BaseModel):
    workflow_id: str
    steps: list[WorkflowStepInput]
    initial_context: dict[str, Any] = {}

    @model_validator(mode="after")
    def validate_step_graph(self) -> "WorkflowRunRequest":
        ids = [step.id for step in self.steps]
        duplicates = sorted({step_id for step_id in ids if ids.count(step_id) > 1})
        if duplicates:
            raise ValueError(f"duplicate step ids: {duplicates}")
        known = set(ids)
        for step in self.steps:
            unknown = sorted(set(step.depends_on) - known)
            if unknown:
                raise ValueError(f"step {step.id!r} depends on unknown steps: {unknown}")
        return self


@workflow_router.post("/run")
async def workflow_run(body: WorkflowRunRequest, request: Request):
    engine = getattr(request.app.state, "workflow_engine", None)
    if engine is None:
        raise HTTPException(503, "Workflow engine not initialized")
    run = await engine.run(
        workflow_id=body.workflow_id,
        steps=[step.model_dump() for step in body.steps],
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
