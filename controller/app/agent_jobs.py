from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import uuid4

from .audit import get_current_operator
from .models import (
    CONTEXT_HINTS_MAX_CHARS,
    AgentJobCheckpoint,
    AgentJobRecord,
    AgentJobStatus,
    AgentRunRequest,
    AgentRunResult,
    AgentStepRequest,
    AgentStepResult,
    OperatorIdentity,
)
from .session_ownership import acting_as
from .utils import PRIVATE_FILE_MODE, atomic_write_text, record_json, record_path, utc_now

logger = logging.getLogger(__name__)


class _AlreadyFinished(ValueError):
    """AgentJobQueue._withdraw found nothing to take back: the job has already finished."""


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
        job_id: str | None = None,
    ) -> AgentJobRecord:
        async with self._lock:
            now = utc_now()
            record = AgentJobRecord(
                id=job_id or uuid4().hex[:12],
                session_id=session_id,
                kind=kind,  # type: ignore[arg-type]
                status="queued",
                created_at=now,
                updated_at=now,
                request=request,
                parent_job_id=parent_job_id,
                operator=operator,
            )
            write = asyncio.ensure_future(asyncio.to_thread(self._write_sync, record))
            try:
                await asyncio.shield(write)
            except asyncio.CancelledError:
                # A thread cannot be stopped, so the record may still land: keep the lock until it has, however
                # often this call is cancelled meanwhile, so a caller handling the cancellation
                # (AgentJobQueue._enqueue) finds the record under the lock.
                while not write.done():
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.wait([write])
                if not write.cancelled() and write.exception() is not None:
                    logger.error("agent job %s could not be written: %s", record.id, write.exception())
                raise
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

    async def interrupt_queued(self, job_id: str) -> tuple[AgentJobRecord, bool]:
        async with self._lock:
            return await asyncio.to_thread(self._interrupt_queued_sync, job_id)

    async def discard(self, job_id: str) -> tuple[AgentJobRecord, bool]:
        async with self._lock:
            return await asyncio.to_thread(self._discard_sync, job_id)

    async def mark_running_interrupted(self) -> None:
        for record in await self.list():
            if record.status == "running":
                record.status = "interrupted"
                record.error = "agent_job_interrupted_on_restart"
                await self.update(record)
            elif record.status == "cancelling":
                # The restart finished the cancellation. Left as "cancelling",
                # the job could never be discarded and still read as live.
                record.status = "cancelled"
                record.error = "agent_job_cancelled"
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
        path = record_path(self.root, job_id, ".json")
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

    def _interrupt_queued_sync(self, job_id: str) -> tuple[AgentJobRecord, bool]:
        record = self._get_sync(job_id)
        if record.status != "queued":
            return record, False
        record.status = "interrupted"
        record.error = "agent_job_interrupted"
        record.updated_at = utc_now()
        self._write_sync(record)
        return record, True

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
        path = record_path(self.root, record.id, ".json")
        atomic_write_text(path, record_json(record, indent=2), mode=PRIVATE_FILE_MODE)


RESUME_CONTEXT_PREFIX = "Resuming background agent job"
RESUME_CONTEXT_MIN_CHARS = 1000
QUEUED_OPERATOR_CREDENTIAL_REMOVED = (
    "refused: the operator who queued this job no longer has a named credential (API_BEARER_TOKENS)"
)


class AgentJobQueue:
    def __init__(
        self,
        *,
        orchestrator,
        store_root: str | Path,
        session_accessible: Callable[[str], Awaitable[bool]],
        worker_count: int = 1,
        audit_store=None,
        operator_configured: Callable[[str], bool] | None = None,
    ):
        # Whether a named credential still proves an operator (build_controller_services wires it to the settings); None
        # checks nothing. A job a token-verified operator queued does not run once that credential is gone.
        self._operator_configured = operator_configured
        self.orchestrator = orchestrator
        # Whether the current operator may use a session (BrowserManager.session_accessible): a job in
        # another operator's session, or of an id with no session under named credentials, is left out
        # of listings and refused by id.
        self.session_accessible = session_accessible
        self.store = AgentJobStore(store_root)
        self.worker_count = max(1, worker_count)
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        self._workers: list[asyncio.Task] = []
        self._requeue_task: asyncio.Task[None] | None = None
        self._started = False
        self.audit = audit_store
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._cancellation_reasons: dict[str, str] = {}
        # Jobs done running whose task is still in its finish callback: _withdraw must not cancel those.
        self._finishing: set[str] = set()
        # job_id -> coroutine function invoked once the job reaches a terminal
        # state, whatever that state is. Used by callers that created a session
        # solely to run this job and must close it afterwards (see CronService).
        # Deliberately in-memory: if the controller restarts, the browser
        # sessions these callbacks would close are gone with it.
        self._on_finish: dict[str, Callable[[str], Awaitable[None]]] = {}

    async def _run_finish_callback(self, job_id: str) -> None:
        callback = self._on_finish.pop(job_id, None)
        if callback is None:
            return
        try:
            await callback(job_id)
        except Exception:
            # Never let cleanup failure escape into the worker loop — but never
            # swallow it silently either, or a leaked session looks like success.
            logger.exception("finish callback for agent job %s failed", job_id)

    async def startup(self) -> None:
        await self.store.startup()
        await self.store.mark_running_interrupted()
        queued = await self.store.list(status="queued")
        self._workers = [
            asyncio.create_task(self._worker_loop(index), name=f"agent-job-worker-{index}")
            for index in range(self.worker_count)
        ]
        # The queue holds 100: with more records than that on disk, putting them here would wait for room
        # that only a worker can make, so a task puts them (oldest first) while startup returns.
        self._requeue_task = asyncio.create_task(
            self._requeue([record.id for record in reversed(queued)]), name="agent-job-requeue"
        )
        self._started = True

    async def _requeue(self, job_ids: list[str]) -> None:
        # More recovered jobs than the queue holds keep it full until workers have taken enough of them. A new enqueue
        # meanwhile is refused as at capacity (_enqueue), which is true of the queue at that moment: the caller tries
        # again, as the refusal says, and room a worker makes goes to whichever of the two reaches it first.
        for job_id in job_ids:
            await self.queue.put(job_id)

    async def shutdown(self) -> None:
        if self._requeue_task is not None:
            self._requeue_task.cancel()
            try:
                await self._requeue_task
            except asyncio.CancelledError:
                pass
            except Exception:
                # The workers still have to be stopped below.
                logger.exception("the agent job re-queue failed")
            self._requeue_task = None
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
        usable = {record.session_id: await self.session_accessible(record.session_id) for record in records}
        return [self._public_record(record) for record in records if usable[record.session_id]]

    async def get_job(self, job_id: str) -> dict:
        return self._public_record(await self._accessible(job_id))

    async def _accessible(self, job_id: str) -> AgentJobRecord:
        """A job in another operator's session answers as if it did not exist."""
        record = await self.store.get(job_id)
        if not await self.session_accessible(record.session_id):
            raise KeyError(job_id)
        return record

    async def enqueue_step(self, session_id: str, payload: AgentStepRequest) -> dict:
        return await self._enqueue(session_id, "agent_step", payload)

    async def enqueue_run(
        self,
        session_id: str,
        payload: AgentRunRequest,
        *,
        on_finish: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict:
        """Queue a run. `on_finish` runs once the job reaches a terminal state, whatever that state is, and is given
        the job's id: it is registered before the job is visible to a worker, so a job that finishes at once still
        calls it, before the caller has this call's answer.

        A caller that passes one owns the session the job runs in and closes it there: if the enqueue is cancelled
        after the job exists, the job is withdrawn too (recorded as interrupted), so none is left on a closed session.
        """
        return await self._enqueue(session_id, "agent_run", payload, on_finish=on_finish)

    async def resume_job(self, job_id: str, *, max_steps: int | None = None) -> dict:
        source = await self._accessible(job_id)
        if not self._is_resumable(source):
            raise ValueError("Job is not resumable")
        if source.kind != "agent_run":
            raise ValueError("Only agent_run jobs can be resumed")

        payload = AgentRunRequest.model_validate(source.request)
        # A new run, with the budget the first was given unless the caller gives one: what was left could be a single
        # step, too few to finish anything.
        if max_steps is not None:
            payload.max_steps = max_steps
        # The approval the first run was queued with was given for that run: this one asks for its own.
        payload.approval_id = None
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
        await self._accessible(job_id)
        record, changed = await self.store.discard(job_id)
        if changed:
            await self._audit("agent_job_discarded", "discarded", record)
        return self._public_record(record)

    async def cancel_job(self, job_id: str) -> dict:
        await self._accessible(job_id)
        record, changed = await self.store.request_cancel(job_id)
        task = self._running_tasks.get(job_id) if record.status == "cancelling" else None
        if job_id in self._finishing:
            # The run is over and its task is in its finish callback (closing its session); the record still reads
            # running or cancelling only because the write of its end failed. Cancelled there, the close never
            # happened (as in _withdraw). Nothing is left to stop, so the record is settled below as one no worker
            # holds is.
            task = None
        if task is not None:
            # Before anything is audited: the record already says cancelling, and an audit that fails (a full
            # disk) used to raise here and leave the job running.
            self._cancellation_reasons[job_id] = "user"
            task.cancel()
        if changed:
            await self._audit_quietly("agent_job_cancel_requested", record.status, record)
        if record.status == "cancelled":
            if changed:
                await self._audit_quietly("agent_job_cancelled", "cancelled", record)
            return self._public_record(record)

        if task is None:
            record = await self.store.finish_cancelled(job_id, user_cancelled=False)
            await self._audit_quietly("agent_job_interrupted", "interrupted", record)
            return self._public_record(record)

        # The worker records how the job ended. Neither its cancellation nor an error of its own is raised over this
        # cancel, and a cancellation of this call is not swallowed (wait_for(shield(task)) did both).
        await asyncio.wait([task], timeout=5)
        return self._public_record(await self.store.get(job_id))

    async def _withdraw(self, job_id: str) -> None:
        """Take back a job whose caller gave up on it while queuing it (a cron trigger cancelled mid-enqueue).

        Nobody asked to cancel it, so it is recorded as a restart or a shutdown records the jobs it stops,
        interrupted, not as an operator's cancel: a queued job is marked before a worker can start it, and a
        running one is stopped the way shutdown stops it. Raises _AlreadyFinished for a job that already finished.
        """
        record, changed = await self.store.interrupt_queued(job_id)
        if changed:
            await self._audit_quietly("agent_job_interrupted", "interrupted", record)
            return
        task = self._running_tasks.get(job_id)
        # The task of a finished job may still be running its finish callback (closing its session): cancelled, the
        # close never happened, and nothing else would close the session. Its record can still say running when the
        # write of its end failed, so the in-memory mark counts as much as the record.
        if task is None or job_id in self._finishing or record.status not in {"running", "cancelling"}:
            raise _AlreadyFinished(f"agent job {job_id} has already finished")
        # Any reason but "user" makes the worker record an interruption; an operator's cancel under way keeps its own.
        self._cancellation_reasons.setdefault(job_id, "withdrawn")
        task.cancel()
        await asyncio.wait([task], timeout=5)

    async def _enqueue(
        self,
        session_id: str,
        kind: str,
        payload: AgentStepRequest | AgentRunRequest,
        *,
        parent_job_id: str | None = None,
        on_finish: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict:
        # Made here, so a cancellation during the write still knows which record it left behind.
        job_id = uuid4().hex[:12]
        try:
            record = await self.store.create(
                session_id=session_id,
                kind=kind,
                request=payload.model_dump(),
                operator=get_current_operator(),
                parent_job_id=parent_job_id,
                job_id=job_id,
            )
        except asyncio.CancelledError:
            # The record may have landed queued, and nothing would put it on the queue now: the next startup
            # would run it after its caller gave up on it (a cron trigger has closed its session by then).
            try:
                await self._withdraw(job_id)
            except KeyError:
                pass  # cancelled before the write began: there is no record
            except Exception:
                logger.exception("agent job %s was left queued after its create was cancelled", job_id)
            raise
        if on_finish is not None:
            self._on_finish[record.id] = on_finish
        try:
            self.queue.put_nowait(record.id)
        except asyncio.QueueFull:
            self._on_finish.pop(record.id, None)
            record.status = "failed"
            record.error = "Job queue is full (max 100 queued jobs)"
            await self.store.update(record)
            raise RuntimeError("Job queue is at capacity. Try again later.")
        try:
            await self._audit("agent_job_enqueued", "queued", record)
        except BaseException as exc:
            ran_to_its_end = False
            if on_finish is not None:
                # The caller is about to close the session this job runs in: take the job back first.
                try:
                    await self._withdraw(record.id)
                except _AlreadyFinished:
                    # There is nothing to take back. If a worker had it, a caller told of a failure (not a cancellation)
                    # would say it never ran, and invite a second run. One no worker holds whose callback is still here
                    # never ran (cancelled or discarded in the queue), and nothing is bound to run that callback.
                    ran_to_its_end = isinstance(exc, Exception) and (
                        record.id in self._running_tasks or record.id not in self._on_finish
                    )
                except Exception:
                    logger.exception("agent job %s could not be withdrawn before its session closes", record.id)
                finally:
                    # A caller answered with an error closes the session itself. One answered with success leaves that
                    # to the finish callback, which the worker may not have reached yet: it stays.
                    if not ran_to_its_end:
                        self._on_finish.pop(record.id, None)
            if not ran_to_its_end:
                raise
            logger.exception("agent job %s has finished, but its queuing could not be audited", record.id)
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
            except Exception as exc:
                logger.exception("agent job worker %s crashed on %s: %s", worker_index, job_id, exc)
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    # Told to stop, and the job failed as it stopped (its record could not be written): stop anyway,
                    # or this worker goes on serving the queue and shutdown waits for it forever.
                    raise asyncio.CancelledError from exc
            finally:
                self._running_tasks.pop(job_id, None)
                self._cancellation_reasons.pop(job_id, None)
                self._finishing.discard(job_id)
                self.queue.task_done()

    async def _process_job(self, job_id: str) -> None:
        try:
            # The worker has no operator of its own: the job runs as whoever
            # queued it, so it can use that operator's session and no other.
            with acting_as(await self._queued_by(job_id)):
                await self._process_job_inner(job_id)
        finally:
            # Runs on success, failure, and cancellation alike. Whoever created a
            # session purely to host this job gets it back here or not at all.
            self._finishing.add(job_id)
            await self._run_finish_callback(job_id)

    async def _queued_by(self, job_id: str) -> OperatorIdentity | None:
        try:
            return (await self.store.get(job_id)).operator
        except KeyError:
            return None

    async def _process_job_inner(self, job_id: str) -> None:
        record = await self.store.start_queued(job_id)
        if record is None:
            # Not in a queued state — nothing will run, so the caller's session
            # still needs releasing. _process_job's finally does that.
            return

        try:
            if self._queued_by_a_removed_operator(record):
                # Never started: it ends as any failed job does, and _process_job's finally releases its session.
                record = await self.store.finish(
                    record.id, status="failed", result=None, error=QUEUED_OPERATOR_CREDENTIAL_REMOVED
                )
                await self._audit_quietly("agent_job_finished", record.status, record)
                return
            await self._audit("agent_job_started", "running", record)
        except asyncio.CancelledError:
            # Stopped as it started: recorded as a stop during the run is, or the record stays "running" or
            # "cancelling" until the next restart.
            await self._record_stop(job_id)
            raise

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
            # A run or step that ended in "error" did not complete: record it failed, with the reason it gave.
            failed = result.status == "error"
            record = await self.store.finish(
                record.id,
                status="failed" if failed else "completed",
                result=result.model_dump(),
                error=self._reported_error(result) if failed else None,
            )
        except asyncio.CancelledError:
            await self._record_stop(job_id)
            raise
        except Exception as exc:
            # Previously a bare `except Exception` that recorded the constant
            # string "agent_job_failed" and logged nothing at all, so a crashing
            # provider adapter, a Playwright error, or a JSON parse failure were
            # indistinguishable and left no server-side traceback.
            logger.exception("agent job %s failed", job_id)
            detail = f"{type(exc).__name__}: {exc}"
            record = await self.store.finish(
                record.id,
                status="failed",
                result=None,
                error=detail[:500],
            )
        await self._audit("agent_job_finished", record.status, record)

    def _queued_by_a_removed_operator(self, record: AgentJobRecord) -> bool:
        """Whether a token-verified operator queued this job and no named credential proves them any more.

        Only a token-verified operator proved who they were when queuing: one named by a header claimed it, and no
        operator named no one, so no credential is asked about either of those.
        """
        operator = record.operator
        if self._operator_configured is None or operator is None or operator.source != "token":
            return False
        return not self._operator_configured(operator.id)

    async def _record_stop(self, job_id: str) -> None:
        """Record a job that was stopped: cancelled by its operator, or interrupted (a shutdown, a withdrawal)."""
        user_cancelled = self._cancellation_reasons.get(job_id) == "user"
        record = await self.store.finish_cancelled(job_id, user_cancelled=user_cancelled)
        event_type = "agent_job_cancelled" if user_cancelled else "agent_job_interrupted"
        # Quietly: the job has stopped and its record says so; an error raised here would take the place of the
        # CancelledError the caller re-raises.
        await self._audit_quietly(event_type, record.status, record)

    async def _audit_quietly(self, event_type: str, status: str, record: AgentJobRecord) -> None:
        """Audit a cancellation or interruption that has already taken effect: a failure is logged, never raised."""
        try:
            await self._audit(event_type, status, record)
        except Exception:
            logger.exception("agent job %s: the %s audit event could not be written", record.id, event_type)

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

    @classmethod
    def _reported_error(cls, result: AgentStepResult | AgentRunResult) -> str:
        """Why a step or run reported "error": the error of the step that did (the last such one for a run)."""
        steps = [result] if isinstance(result, AgentStepResult) else result.steps
        reasons = [step.error for step in steps if step.status == "error" and step.error]
        return cls._truncate(reasons[-1] if reasons else "the agent reported an error without a reason", 500) or ""

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
                f"{RESUME_CONTEXT_PREFIX} {record.id}. No completed step checkpoints were recorded; "
                "continue from the current browser state and avoid repeating completed work when visible."
            )
        latest = record.checkpoints[-1]
        step_lines = []
        for checkpoint in record.checkpoints[-6:]:
            action = checkpoint.action or "unknown"
            location = cls._truncate(checkpoint.url or checkpoint.title, 200) or "current page"
            step_lines.append(f"{checkpoint.step_index}. {checkpoint.status} {action} at {location}")
        return (
            f"{RESUME_CONTEXT_PREFIX} {record.id} after {len(record.checkpoints)} completed step(s). "
            f"Latest checkpoint: status={latest.status}, action={latest.action or 'unknown'}, "
            f"url={cls._truncate(latest.url, 500) or 'unknown'}. Continue from the current browser state; do not repeat completed "
            "actions unless the page state requires it.\nCompleted checkpoints:\n" + "\n".join(step_lines)
        )

    @staticmethod
    def _merge_context_hints(existing: str | None, resume_context: str) -> str:
        """Operator hints plus this resume's context, within the request's limit.

        The merged text is stored as the new job's request and re-validated when
        a worker picks it up. It used to be unbounded: long checkpoint URLs, or a
        few resumes in a row each appending another block, pushed it past
        context_hints' limit and the resumed job failed with a ValidationError
        before running a step. An earlier resume block is dropped, since the
        new one describes the same run from a later point.
        """
        operator_hints = (existing or "").split(RESUME_CONTEXT_PREFIX, 1)[0].strip()
        if not operator_hints:
            return resume_context[:CONTEXT_HINTS_MAX_CHARS]
        # Operator hints win, but always leave the resume context some room.
        operator_hints = operator_hints[: CONTEXT_HINTS_MAX_CHARS - 2 - RESUME_CONTEXT_MIN_CHARS]
        budget = CONTEXT_HINTS_MAX_CHARS - 2 - len(operator_hints)
        return f"{operator_hints}\n\n{resume_context[:budget]}"

    @staticmethod
    def _truncate(value: object, limit: int) -> str | None:
        if value is None:
            return None
        text = str(value)
        if len(text) <= limit:
            return text
        return f"{text[: limit - 3]}..."
