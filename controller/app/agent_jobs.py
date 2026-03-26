from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from uuid import uuid4

from .audit import get_current_operator
from .models import AgentJobRecord, AgentJobStatus, AgentRunRequest, AgentStepRequest
from .utils import utc_now

logger = logging.getLogger(__name__)


class AgentJobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._lock = asyncio.Lock()

    async def startup(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    async def list(
        self,
        *,
        status: AgentJobStatus | None = None,
        session_id: str | None = None,
    ) -> list[AgentJobRecord]:
        return await asyncio.to_thread(self._list_sync, status, session_id)

    async def get(self, job_id: str) -> AgentJobRecord:
        return await asyncio.to_thread(self._get_sync, job_id)

    async def create(self, *, session_id: str, kind: str, request: dict, operator=None) -> AgentJobRecord:
        async with self._lock:
            now = utc_now()
            record = AgentJobRecord(
                id=uuid4().hex[:12],
                session_id=session_id,
                kind=kind,  # type: ignore[arg-type]
                status="queued",
                created_at=now,
                updated_at=now,
                request=request,
                operator=operator,
            )
            await asyncio.to_thread(self._write_sync, record)
            return record

    async def update(self, record: AgentJobRecord) -> None:
        async with self._lock:
            record.updated_at = utc_now()
            await asyncio.to_thread(self._write_sync, record)

    async def mark_running_interrupted(self) -> None:
        for record in await self.list():
            if record.status == "running":
                record.status = "interrupted"
                await self.update(record)

    def _list_sync(
        self,
        status: AgentJobStatus | None,
        session_id: str | None,
    ) -> list[AgentJobRecord]:
        records: list[AgentJobRecord] = []
        for path in sorted(self.root.glob("*.json"), reverse=True):
            try:
                record = AgentJobRecord.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("failed to decode job record %s: %s", path, exc)
                continue
            if status is not None and record.status != status:
                continue
            if session_id is not None and record.session_id != session_id:
                continue
            records.append(record)
        records.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        return records

    def _get_sync(self, job_id: str) -> AgentJobRecord:
        path = self.root / f"{job_id}.json"
        if not path.exists():
            raise KeyError(job_id)
        return AgentJobRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def _write_sync(self, record: AgentJobRecord) -> None:
        path = self.root / f"{record.id}.json"
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        tmp_path.replace(path)



class AgentJobQueue:
    def __init__(self, *, orchestrator, store_root: str | Path, worker_count: int = 1, audit_store=None):
        self.orchestrator = orchestrator
        self.store = AgentJobStore(store_root)
        self.worker_count = max(1, worker_count)
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        self._workers: list[asyncio.Task] = []
        self._started = False
        self.audit = audit_store

    async def startup(self) -> None:
        await self.store.startup()
        await self.store.mark_running_interrupted()
        queued = await self.store.list(status="queued")
        for record in reversed(queued):
            await self.queue.put(record.id)
        self._workers = [
            asyncio.create_task(self._worker_loop(index), name=f"agent-job-worker-{index}")
            for index in range(self.worker_count)
        ]
        self._started = True

    async def shutdown(self) -> None:
        for worker in self._workers:
            worker.cancel()
        for worker in self._workers:
            try:
                await worker
            except asyncio.CancelledError:
                pass
        self._workers = []
        self._started = False

    async def list_jobs(
        self,
        *,
        status: AgentJobStatus | None = None,
        session_id: str | None = None,
    ) -> list[dict]:
        records = await self.store.list(status=status, session_id=session_id)
        return [record.model_dump() for record in records]

    async def get_job(self, job_id: str) -> dict:
        return (await self.store.get(job_id)).model_dump()

    async def enqueue_step(self, session_id: str, payload: AgentStepRequest) -> dict:
        return await self._enqueue(session_id, "agent_step", payload)

    async def enqueue_run(self, session_id: str, payload: AgentRunRequest) -> dict:
        return await self._enqueue(session_id, "agent_run", payload)

    async def _enqueue(
        self,
        session_id: str,
        kind: str,
        payload: AgentStepRequest | AgentRunRequest,
    ) -> dict:
        record = await self.store.create(
            session_id=session_id,
            kind=kind,
            request=payload.model_dump(),
            operator=get_current_operator(),
        )
        try:
            self.queue.put_nowait(record.id)
        except asyncio.QueueFull:
            record.status = "failed"
            record.error = "Job queue is full (max 100 queued jobs)"
            await self.store.update(record)
            raise RuntimeError("Job queue is at capacity. Try again later.")
        await self._audit("agent_job_enqueued", "queued", record)
        return record.model_dump()

    async def _worker_loop(self, worker_index: int) -> None:
        while True:
            job_id = await self.queue.get()
            try:
                await self._process_job(job_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.exception("agent job worker %s crashed on %s: %s", worker_index, job_id, exc)
            finally:
                self.queue.task_done()

    async def _process_job(self, job_id: str) -> None:
        record = await self.store.get(job_id)
        if record.status != "queued":
            return

        record.status = "running"
        await self.store.update(record)
        await self._audit("agent_job_started", "running", record)

        try:
            if record.kind == "agent_step":
                request = AgentStepRequest.model_validate(record.request)
                result = await self.orchestrator.step(
                    session_id=record.session_id,
                    provider_name=request.provider,
                    goal=request.goal,
                    observation_limit=request.observation_limit,
                    context_hints=request.context_hints,
                    upload_approved=request.upload_approved,
                    approval_id=request.approval_id,
                    provider_model=request.provider_model,
                )
            else:
                request = AgentRunRequest.model_validate(record.request)
                result = await self.orchestrator.run(
                    session_id=record.session_id,
                    provider_name=request.provider,
                    goal=request.goal,
                    max_steps=request.max_steps,
                    observation_limit=request.observation_limit,
                    context_hints=request.context_hints,
                    upload_approved=request.upload_approved,
                    approval_id=request.approval_id,
                    provider_model=request.provider_model,
                )
            record.status = "completed"
            record.result = result.model_dump()
            record.error = None
        except Exception as exc:
            record.status = "failed"
            record.error = str(exc)
            record.result = None
        await self.store.update(record)
        await self._audit("agent_job_finished", record.status, record)

    async def _audit(self, event_type: str, status: str, record: AgentJobRecord) -> None:
        if self.audit is None:
            return
        await self.audit.append(
            event_type=event_type,
            status=status,
            action=record.kind,
            session_id=record.session_id,
            job_id=record.id,
            operator=record.operator,
            details={"kind": record.kind, "error": record.error},
        )
