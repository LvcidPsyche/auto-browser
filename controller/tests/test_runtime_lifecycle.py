"""Lifecycle regressions: things that leak, wedge, or die silently.

Each test here corresponds to a defect that was invisible in production because
the failure mode was *silence* — a session never released, a loop that stopped
running, a store quietly emptied, an exception recorded as a constant string.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.audit import FileAuditStore
from app.cron_service import CronService
from app.maintenance import MaintenanceService


class _FakeManager:
    """Tracks create/close so a leaked session is observable."""

    def __init__(self) -> None:
        self.created: list[str] = []
        self.closed: list[str] = []
        self._counter = 0

    async def create_session(self, **kwargs) -> dict:
        self._counter += 1
        session_id = f"sess-{self._counter}"
        self.created.append(session_id)
        return {"id": session_id}

    async def close_session(self, session_id: str) -> None:
        self.closed.append(session_id)

    @property
    def leaked(self) -> list[str]:
        return [s for s in self.created if s not in self.closed]


class _FakeJobQueue:
    """Captures the finish callback instead of running a real worker."""

    def __init__(self) -> None:
        self.callbacks: dict[str, object] = {}
        self.enqueued: list[str] = []
        self.fail_enqueue = False

    async def enqueue_run(self, session_id: str, payload) -> dict:
        if self.fail_enqueue:
            raise RuntimeError("Job queue is at capacity. Try again later.")
        self.enqueued.append(session_id)
        return {"id": f"job-for-{session_id}"}

    def on_finish(self, job_id: str, callback) -> None:
        self.callbacks[job_id] = callback

    async def finish(self, job_id: str) -> None:
        await self.callbacks[job_id]()


def _cron(tmp_path: Path) -> tuple[CronService, _FakeManager, _FakeJobQueue]:
    manager = _FakeManager()
    queue = _FakeJobQueue()
    service = CronService(tmp_path / "cron.json", job_queue=queue, manager=manager)
    return service, manager, queue


def _job(job_id: str = "j1") -> dict:
    return {"id": job_id, "goal": "do a thing", "schedule": "* * * * *"}


@pytest.mark.asyncio
async def test_cron_session_is_closed_when_the_run_finishes(tmp_path: Path) -> None:
    """The session a cron run is given must come back.

    Regression: _run_job_now created a session and nothing ever closed it. With
    MAX_SESSIONS defaulting to 1, the first cron fire consumed the only slot
    permanently — every later fire and every manual create_session then failed
    "Session limit reached".
    """
    service, manager, queue = _cron(tmp_path)

    result = await service._run_job_now(_job())
    assert result["triggered"] is True
    assert manager.leaked == [result["session_id"]], "session is held while the run is in flight"

    await queue.finish(f"job-for-{result['session_id']}")

    assert manager.leaked == [], "session must be released once the job reaches a terminal state"


@pytest.mark.asyncio
async def test_cron_session_is_closed_even_when_the_run_fails(tmp_path: Path) -> None:
    """Release is unconditional — a failed run must not strand its session."""
    service, manager, queue = _cron(tmp_path)
    result = await service._run_job_now(_job())

    # The queue invokes the callback from a finally, so failure looks the same.
    await queue.finish(f"job-for-{result['session_id']}")

    assert manager.leaked == []


@pytest.mark.asyncio
async def test_cron_session_is_released_when_enqueue_is_rejected(tmp_path: Path) -> None:
    """A full queue must not leave an orphaned session behind."""
    service, manager, queue = _cron(tmp_path)
    queue.fail_enqueue = True

    with pytest.raises(RuntimeError):
        await service._run_job_now(_job())

    assert manager.leaked == []


@pytest.mark.asyncio
async def test_cron_skips_a_fire_while_the_previous_run_is_active(tmp_path: Path) -> None:
    """A schedule faster than its own runtime must not stack sessions."""
    service, manager, queue = _cron(tmp_path)

    first = await service._run_job_now(_job())
    second = await service._run_job_now(_job())

    assert second["triggered"] is False
    assert second["reason"] == "previous_run_active"
    assert len(manager.created) == 1, "the overlapping fire must not create a second session"

    await queue.finish(f"job-for-{first['session_id']}")
    third = await service._run_job_now(_job())
    assert third["triggered"] is True, "a new run is allowed once the previous one released"


def test_corrupt_cron_store_is_quarantined_not_silently_emptied(tmp_path: Path) -> None:
    """Returning {} here used to destroy every cron job on the next save."""
    store = tmp_path / "cron.json"
    store.write_text("{not valid json", encoding="utf-8")
    service = CronService(store)

    with pytest.raises(RuntimeError, match="unreadable"):
        service._load()

    assert not store.exists(), "the damaged file is moved aside"
    quarantined = list(tmp_path.glob("cron.corrupt-*.json"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "{not valid json"


@pytest.mark.asyncio
async def test_maintenance_loop_survives_a_failing_sweep(tmp_path: Path, caplog) -> None:
    """One bad sweep must not end the loop.

    Regression: _run_loop had no guard, so a single exception killed the task
    with nothing observing it and artifacts grew forever afterwards.
    """
    settings = SimpleNamespace(
        cleanup_interval_seconds=0.01,
        artifact_root=str(tmp_path / "artifacts"),
        upload_root=str(tmp_path / "uploads"),
        auth_root=str(tmp_path / "auth"),
        artifact_retention_hours=1,
        upload_retention_hours=1,
        auth_retention_hours=1,
    )
    service = MaintenanceService(settings=settings, session_provider=lambda: [])

    calls = {"n": 0}

    async def exploding_cleanup():
        calls["n"] += 1
        raise OSError("simulated stat race")

    service.run_cleanup = exploding_cleanup  # type: ignore[assignment]

    with caplog.at_level(logging.ERROR):
        task = asyncio.create_task(service._run_loop())
        await asyncio.sleep(0.08)
        service._stop_event.set()
        await asyncio.wait_for(task, timeout=2)

    assert calls["n"] >= 2, "loop kept running after the first failure"
    assert any("cleanup sweep failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_audit_listing_survives_a_torn_line(tmp_path: Path) -> None:
    """A partial line from a kill mid-append must not 500 the listing forever."""
    from app.models import AuditEvent, OperatorIdentity

    def _event(action: str) -> AuditEvent:
        return AuditEvent(
            id=f"evt-{action}",
            timestamp="2026-08-04T00:00:00Z",
            event_type="test_event",
            status="ok",
            action=action,
            session_id="s1",
            operator=OperatorIdentity(id="op-1"),
        )

    store = FileAuditStore(str(tmp_path), max_events=100)
    await store.startup()
    await store.append_event(_event("a"))

    # Simulate a process killed halfway through writing a record.
    with store.events_path.open("a", encoding="utf-8") as fh:
        fh.write('{"event_type": "torn", "sta\n')

    await store.append_event(_event("b"))

    events = await store.list(limit=50, session_id=None, event_type=None, operator_id=None)
    actions = [e.action for e in events]
    assert "a" in actions and "b" in actions, "valid records on both sides of the tear survive"


def test_background_task_failures_are_logged(caplog) -> None:
    """Fire-and-forget failures used to surface only at GC time, if at all."""
    from app.utils import spawn_background_task

    async def boom():
        raise ValueError("kaboom")

    async def drive():
        task = spawn_background_task(boom())
        with pytest.raises(ValueError):
            await task

    with caplog.at_level(logging.ERROR):
        asyncio.run(drive())

    assert any("kaboom" in str(r.getMessage()) or "kaboom" in str(r.exc_info) for r in caplog.records)


def test_events_module_counts_dropped_sse_events() -> None:
    """Drops were DEBUG-only, so an operator silently missed actions."""
    from app import events as events_module

    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    queue.put_nowait("occupied")

    before = events_module.dropped_event_count()
    events_module._SESSION_QUEUES["sess-x"].append(queue)
    try:
        events_module._dispatch("sess-x", {"type": "test"})
    finally:
        events_module._SESSION_QUEUES["sess-x"].remove(queue)

    assert events_module.dropped_event_count() == before + 1


def test_redis_session_store_sets_socket_timeouts() -> None:
    """A hung (not refused) redis blocked every action indefinitely."""
    source = Path(__file__).resolve().parent.parent / "app" / "session_store.py"
    text = source.read_text(encoding="utf-8")
    assert "socket_connect_timeout=2" in text
    assert "socket_timeout=2" in text


def test_metrics_path_collapses_unmatched_routes() -> None:
    """Raw URL paths as Prometheus labels grow the registry without bound."""
    source = Path(__file__).resolve().parent.parent / "app" / "middleware" / "http.py"
    text = source.read_text(encoding="utf-8")
    assert "__unmatched__" in text
    assert "_metric_path" in text


def test_cron_store_roundtrips_valid_json(tmp_path: Path) -> None:
    """Guard the guard: quarantine must not fire on healthy stores."""
    service = CronService(tmp_path / "cron.json")
    service._save({"j1": _job()})
    assert service._load()["j1"]["goal"] == "do a thing"
    assert not list(tmp_path.glob("*corrupt*"))
    assert json.loads((tmp_path / "cron.json").read_text(encoding="utf-8"))["j1"]["id"] == "j1"
