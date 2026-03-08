from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from .browser_manager import BrowserManager
from .config import get_settings
from .models import (
    AgentRunRequest,
    AgentStepRequest,
    ClickRequest,
    CreateSessionRequest,
    HumanTakeoverRequest,
    NavigateRequest,
    PressRequest,
    SaveStorageStateRequest,
    ScrollRequest,
    TypeRequest,
    UploadRequest,
)
from .orchestrator import BrowserOrchestrator
from .provider_registry import ProviderRegistry

logging.basicConfig(level=logging.INFO)

settings = get_settings()
manager = BrowserManager(settings)
providers = ProviderRegistry(settings)
orchestrator = BrowserOrchestrator(manager, providers)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await manager.startup()
    try:
        yield
    finally:
        await manager.shutdown()


app = FastAPI(
    title="Browser Operator Controller",
    version="0.2.0",
    lifespan=lifespan,
    summary="Visual browser operator control plane for LLM workflows.",
)

app.mount("/artifacts", StaticFiles(directory=settings.artifact_root), name="artifacts")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> dict[str, str]:
    try:
        await manager.ensure_browser()
        return {"status": "ready"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/agent/providers")
async def list_agent_providers() -> list[dict]:
    return [item.model_dump() for item in orchestrator.list_providers()]


@app.get("/sessions")
async def list_sessions() -> list[dict]:
    return await manager.list_sessions()


@app.post("/sessions")
async def create_session(payload: CreateSessionRequest) -> dict:
    try:
        return await manager.create_session(
            name=payload.name,
            start_url=payload.start_url,
            storage_state_path=payload.storage_state_path,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/sessions/{session_id}")
async def get_session(session_id: str) -> dict:
    try:
        session = await manager.get_session(session_id)
        return await manager._session_summary(session)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc


@app.get("/sessions/{session_id}/observe")
async def observe(session_id: str, limit: int = 40) -> dict:
    try:
        return await manager.observe(session_id, limit=limit)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc


@app.post("/sessions/{session_id}/actions/navigate")
async def navigate(session_id: str, payload: NavigateRequest) -> dict:
    try:
        return await manager.navigate(session_id, payload.url)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post("/sessions/{session_id}/actions/click")
async def click(session_id: str, payload: ClickRequest) -> dict:
    try:
        return await manager.click(
            session_id,
            selector=payload.selector,
            element_id=payload.element_id,
            x=payload.x,
            y=payload.y,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post("/sessions/{session_id}/actions/type")
async def type_text(session_id: str, payload: TypeRequest) -> dict:
    try:
        return await manager.type(
            session_id,
            selector=payload.selector,
            element_id=payload.element_id,
            text=payload.text,
            clear_first=payload.clear_first,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post("/sessions/{session_id}/actions/press")
async def press_key(session_id: str, payload: PressRequest) -> dict:
    try:
        return await manager.press(session_id, payload.key)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post("/sessions/{session_id}/actions/scroll")
async def scroll(session_id: str, payload: ScrollRequest) -> dict:
    try:
        return await manager.scroll(session_id, payload.delta_x, payload.delta_y)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post("/sessions/{session_id}/actions/upload")
async def upload(session_id: str, payload: UploadRequest) -> dict:
    try:
        return await manager.upload(
            session_id,
            selector=payload.selector,
            element_id=payload.element_id,
            file_path=payload.file_path,
            approved=payload.approved,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/sessions/{session_id}/storage-state")
async def save_storage_state(session_id: str, payload: SaveStorageStateRequest) -> dict:
    try:
        return await manager.save_storage_state(session_id, payload.path)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post("/sessions/{session_id}/takeover")
async def request_human_takeover(session_id: str, payload: HumanTakeoverRequest) -> dict:
    try:
        return await manager.request_human_takeover(session_id, payload.reason)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc


@app.post("/sessions/{session_id}/agent/step")
async def run_agent_step(session_id: str, payload: AgentStepRequest) -> dict:
    try:
        result = await orchestrator.step(
            session_id=session_id,
            provider_name=payload.provider,
            goal=payload.goal,
            observation_limit=payload.observation_limit,
            context_hints=payload.context_hints,
            upload_approved=payload.upload_approved,
            provider_model=payload.provider_model,
        )
        status_code = 200 if result.status != "error" else 502
        if status_code != 200:
            raise HTTPException(status_code=status_code, detail=result.model_dump())
        return result.model_dump()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/sessions/{session_id}/agent/run")
async def run_agent_loop(session_id: str, payload: AgentRunRequest) -> dict:
    try:
        result = await orchestrator.run(
            session_id=session_id,
            provider_name=payload.provider,
            goal=payload.goal,
            max_steps=payload.max_steps,
            observation_limit=payload.observation_limit,
            context_hints=payload.context_hints,
            upload_approved=payload.upload_approved,
            provider_model=payload.provider_model,
        )
        return result.model_dump()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.delete("/sessions/{session_id}")
async def close_session(session_id: str) -> dict:
    try:
        return await manager.close_session(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
