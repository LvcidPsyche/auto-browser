from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from uuid import uuid4

from .audit import get_current_operator
from .models import (
    AgentJobCheckpoint,
    AgentJobRecord,
    AgentJobStatus,
    AgentRunRequest,
    AgentStepRequest,
    AgentStepResult,
)
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
        async with self._lock:
            return await asyncio.to_thread(self._list_sync, status, session_id)

    async def get(self, job_id: str) -> AgentJobRecord:
        async with self._lock:
            return await asyncio.to_thread(self._get_sync, job_id)

    async def create(
        self,
        *,
        session_id: str,
        kind: str,
        request: dict,
        operator=None,
        parent_job_id: str | None = None,
    ) -> AgentJobRecord:
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
                parent_job_id=parent_job_id,
                operator=operator,
            )
            await asyncio.to_thread(self._write_sync, record)
            return record

    async def update(self, record: AgentJobRecord) -> None:
        async with self._lock:
            record.updated_at = utc_now()
            await asyncio.to_thread(self._write_sync, record)

    async def start_queued(self, job_id: str) -> AgentJobRecord | None:
        async with self._lock:
            return await asyncio.to_thread(self._start_queued_sync, job_id)

    async def append_checkpoint(self, job_id: str, checkpoint: AgentJobCheckpoint) -> AgentJobRecord:
        async with self._lock:
            return await asyncio.to_thread(self._append_checkpoint_sync, job_id, checkpoint)

    async def finish(
        self,
        job_id: str,
        *,
        status: AgentJobStatus,
        result: dict | None,
        error: str | None,
    ) -> AgentJobRecord:
        async with self._lock:
            return await asyncio.to_thread(self._finish_sync, job_id, status, result, error)

    async def finish_cancelled(self, job_id: str, *, user_cancelled: bool) -> AgentJobRecord:
        async with self._lock:
            return await asyncio.to_thread(self._finish_cancelled_sync, job_id, user_cancelled)

    async def request_cancel(self, job_id: str) -> tuple[AgentJobRecord, bool]:
        async with self._lock:
            return await asyncio.to_thread(self._request_cancel_sync, job_id)

    async def discard(self, job_id: str) -> tuple[AgentJobRecord, bool]:
        async with self._lock:
            return await asyncio.to_thread(self._discard_sync, job_id)

    async def mark_running_interrupted(self) -> None:
        for record in await self.list():
            if record.status == "running":
                record.status = "interrupted"
                record.error = "agent_job_interrupted_on_restart"
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

    def _start_queued_sync(self, job_id: str) -> AgentJobRecord | None:
        record = self._get_sync(job_id)
        if record.status != "queued":
            return None
        record.status = "running"
        record.updated_at = utc_now()
        self._write_sync(record)
        return record

    def _append_checkpoint_sync(self, job_id: str, checkpoint: AgentJobCheckpoint) -> AgentJobRecord:
        record = self._get_sync(job_id)
        record.checkpoints.append(checkpoint)
        record.updated_at = utc_now()
        self._write_sync(record)
        return record

    def _finish_sync(
        self,
        job_id: str,
        status: AgentJobStatus,
        result: dict | None,
        error: str | None,
    ) -> AgentJobRecord:
        record = self._get_sync(job_id)
        if record.status == "cancelling":
            record.status = "cancelled"
            record.error = "agent_job_cancelled"
            record.result = None
        elif record.status not in {"cancelled", "discarded"}:
            record.status = status
            record.error = error
            record.result = result
        record.updated_at = utc_now()
        self._write_sync(record)
        return record

    def _finish_cancelled_sync(self, job_id: str, user_cancelled: bool) -> AgentJobRecord:
        record = self._get_sync(job_id)
        if user_cancelled:
            record.status = "cancelled"
            record.error = "agent_job_cancelled"
        else:
            record.status = "interrupted"
            record.error = "agent_job_interrupted"
        record.result = None
        record.updated_at = utc_now()
        self._write_sync(record)
        return record

    def _request_cancel_sync(self, job_id: str) -> tuple[AgentJobRecord, bool]:
        record = self._get_sync(job_id)
        if record.status == "queued":
            record.status = "cancelled"
            record.error = "agent_job_cancelled"
            record.updated_at = utc_now()
            self._write_sync(record)
            return record, True
        if record.status == "running":
            record.status = "cancelling"
            record.error = "agent_job_cancellation_requested"
            record.updated_at = utc_now()
            self._write_sync(record)
            return record, True
        if record.status in {"cancelling", "cancelled"}:
            return record, False
        raise ValueError("Only queued or running jobs can be cancelled")

    def _discard_sync(self, job_id: str) -> tuple[AgentJobRecord, bool]:
        record = self._get_sync(job_id)
        if record.status in {"running", "cancelling"}:
            raise ValueError("Running jobs must be cancelled before they can be discarded")
        if record.status != "discarded":
            record.status = "discarded"
            record.error = "agent_job_discarded"
            record.updated_at = utc_now()
            self._write_sync(record)
            return record, True
        return record, False

    def _write_sync(self, record: AgentJobRecord) -> None:
        path = self.root / f"{record.id}.json"
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        for attempt in range(5):
            try:
                tmp_path.replace(path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.01 * (attempt + 1))



class AgentJobQueue:
    def __init__(self, *, orchestrator, store_root: str | Path, worker_count: int = 1, audit_store=None):
        self.orchestrator = orchestrator
        self.store = AgentJobStore(store_root)
        self.worker_count = max(1, worker_count)
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        self._workers: list[asyncio.Task] = []
        self._started = False
        self.audit = audit_store
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._cancellation_reasons: dict[str, str] = {}

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
        return [self._public_record(record) for record in records]

    async def get_job(self, job_id: str) -> dict:
        return self._public_record(await self.store.get(job_id))

    async def enqueue_step(self, session_id: str, payload: AgentStepRequest) -> dict:
        return await self._enqueue(session_id, "agent_step", payload)

    async def enqueue_run(self, session_id: str, payload: AgentRunRequest) -> dict:
        return await self._enqueue(session_id, "agent_run", payload)

    async def resume_job(self, job_id: str, *, max_steps: int | None = None) -> dict:
        source = await self.store.get(job_id)
        if not self._is_resumable(source):
            raise ValueError("Job is not resumable")
        if source.kind != "agent_run":
            raise ValueError("Only agent_run jobs can be resumed")

        payload = AgentRunRequest.model_validate(source.request)
        completed_steps = len(source.checkpoints)
        remaining_steps = max(1, payload.max_steps - completed_steps)
        payload.max_steps = max_steps or remaining_steps
        payload.context_hints = self._merge_context_hints(
            payload.context_hints,
            self._resume_context(source),
        )
        resumed = await self._enqueue(
            source.session_id,
            "agent_run",
            payload,
            parent_job_id=source.id,
        )
        await self._audit("agent_job_resumed", "queued", await self.store.get(resumed["id"]))
        return resumed

    async def discard_job(self, job_id: str) -> dict:
        record, changed = await self.store.discard(job_id)
        if changed:
            await self._audit("agent_job_discarded", "discarded", record)
        return self._public_record(record)

    async def cancel_job(self, job_id: str) -> dict:
        record, changed = await self.store.request_cancel(job_id)
        if changed:
            await self._audit("agent_job_cancel_requested", record.status, record)
        if record.status == "cancelled":
            if changed:
                await self._audit("agent_job_cancelled", "cancelled", record)
            return self._public_record(record)

        task = self._running_tasks.get(job_id)
        if task is None:
            record = await self.store.finish_cancelled(job_id, user_cancelled=False)
            await self._audit("agent_job_interrupted", "interrupted", record)
            return self._public_record(record)

        self._cancellation_reasons[job_id] = "user"
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            return self._public_record(await self.store.get(job_id))
        return self._public_record(await self.store.get(job_id))

    async def _enqueue(
        self,
        session_id: str,
        kind: str,
        payload: AgentStepRequest | AgentRunRequest,
        *,
        parent_job_id: str | None = None,
    ) -> dict:
        record = await self.store.create(
            session_id=session_id,
            kind=kind,
            request=payload.model_dump(),
            operator=get_current_operator(),
            parent_job_id=parent_job_id,
        )
        try:
            self.queue.put_nowait(record.id)
        except asyncio.QueueFull:
            record.status = "failed"
            record.error = "Job queue is full (max 100 queued jobs)"
            await self.store.update(record)
            raise RuntimeError("Job queue is at capacity. Try again later.")
        await self._audit("agent_job_enqueued", "queued", record)
        return self._public_record(record)

    async def _worker_loop(self, worker_index: int) -> None:
        while True:
            job_id = await self.queue.get()
            task: asyncio.Task[None] | None = None
            try:
                task = asyncio.create_task(self._process_job(job_id), name=f"agent-job-{job_id}")
                self._running_tasks[job_id] = task
                await task
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    if task is not None:
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
                    raise
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.exception("agent job worker %s crashed on %s: %s", worker_index, job_id, exc)
            finally:
                self._running_tasks.pop(job_id, None)
                self._cancellation_reasons.pop(job_id, None)
                self.queue.task_done()

    async def _process_job(self, job_id: str) -> None:
        record = await self.store.start_queued(job_id)
        if record is None:
            return

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
                    workflow_profile=request.workflow_profile,
                )
                record = await self.store.append_checkpoint(record.id, self._checkpoint_from_step(1, result))
            else:
                request = AgentRunRequest.model_validate(record.request)

                async def checkpoint_step(step_index: int, step_result: AgentStepResult) -> None:
                    await self.store.append_checkpoint(record.id, self._checkpoint_from_step(step_index, step_result))

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
                    workflow_profile=request.workflow_profile,
                    on_step=checkpoint_step,
                )
            record = await self.store.finish(record.id, status="completed", result=result.model_dump(), error=None)
        except asyncio.CancelledError:
            user_cancelled = self._cancellation_reasons.get(job_id) == "user"
            record = await self.store.finish_cancelled(job_id, user_cancelled=user_cancelled)
            event_type = "agent_job_cancelled" if user_cancelled else "agent_job_interrupted"
            await self._audit(event_type, record.status, record)
            raise
        except Exception:
            record = await self.store.finish(record.id, status="failed", result=None, error="agent_job_failed")
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

    @classmethod
    def _public_record(cls, record: AgentJobRecord) -> dict:
        payload = record.model_dump()
        payload["checkpoint_count"] = len(record.checkpoints)
        payload["resumable"] = cls._is_resumable(record)
        return payload

    @staticmethod
    def _is_resumable(record: AgentJobRecord) -> bool:
        if record.kind != "agent_run":
            return False
        if record.status in {"interrupted", "failed"}:
            return True
        if record.status != "completed" or not isinstance(record.result, dict):
            return False
        return record.result.get("status") in {"max_steps_reached", "approval_required", "error"}

    @classmethod
    def _checkpoint_from_step(cls, step_index: int, result: AgentStepResult) -> AgentJobCheckpoint:
        execution = result.execution if isinstance(result.execution, dict) else {}
        after = execution.get("after") if isinstance(execution.get("after"), dict) else {}
        observation = result.observation if isinstance(result.observation, dict) else {}
        decision = result.decision if isinstance(result.decision, dict) else {}
        return AgentJobCheckpoint(
            step_index=step_index,
            created_at=utc_now(),
            status=result.status,
            action=cls._truncate(decision.get("action"), 120),
            reason=cls._truncate(decision.get("reason"), 300),
            url=cls._truncate(after.get("url") or observation.get("url"), 2000),
            title=cls._truncate(after.get("title") or observation.get("title"), 500),
            error=cls._truncate(result.error, 500),
        )

    @classmethod
    def _resume_context(cls, record: AgentJobRecord) -> str:
        if not record.checkpoints:
            return (
                f"Resuming background agent job {record.id}. No completed step checkpoints were recorded; "
                "continue from the current browser state and avoid repeating completed work when visible."
            )
        latest = record.checkpoints[-1]
        step_lines = []
        for checkpoint in record.checkpoints[-6:]:
            action = checkpoint.action or "unknown"
            location = checkpoint.url or checkpoint.title or "current page"
            step_lines.append(f"{checkpoint.step_index}. {checkpoint.status} {action} at {location}")
        return (
            f"Resuming background agent job {record.id} after {len(record.checkpoints)} completed step(s). "
            f"Latest checkpoint: status={latest.status}, action={latest.action or 'unknown'}, "
            f"url={latest.url or 'unknown'}. Continue from the current browser state; do not repeat completed "
            "actions unless the page state requires it.\nCompleted checkpoints:\n"
            + "\n".join(step_lines)
        )

    @staticmethod
    def _merge_context_hints(existing: str | None, resume_context: str) -> str:
        if existing:
            return f"{existing}\n\n{resume_context}"
        return resume_context

    @staticmethod
    def _truncate(value: object, limit: int) -> str | None:
        if value is None:
            return None
        text = str(value)
        if len(text) <= limit:
            return text
        return f"{text[: limit - 3]}..."
