"""
cron_service.py — Cron-scheduled and webhook-triggered browser automation jobs.

Allows defining recurring browser tasks on a cron schedule or via HTTP webhooks.
Each job specifies: schedule, session config, agent goal/steps.

Storage: JSON file at CRON_STORE_PATH (default /data/crons/crons.json)

Job schema:
  {
    "id":          str (uuid hex),
    "name":        str,
    "provider":    str,
    "schedule":    str (cron expr, e.g. "0 9 * * 1-5") or null for webhook-only,
    "enabled":     bool,
    "webhook_key": str | null (secret key for POST /crons/{id}/trigger),
    "goal":        str (natural language goal for agent),
    "start_url":   str | null,
    "auth_profile":str | null,
    "proxy_persona":str | null,
    "max_steps":   int (default 20),
    "created_at":  ISO datetime,
    "last_run_at": ISO datetime | null,
    "last_status": str | null,
    "run_count":   int,
  }

Depends on APScheduler for cron execution (optional install).
If APScheduler is not available, cron scheduling is disabled but webhook triggers still work.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

from .utils import UTC

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    _APSCHEDULER_AVAILABLE = True
except ImportError:
    _APSCHEDULER_AVAILABLE = False
    logger.info(
        "APScheduler not installed — cron scheduling disabled. "
        "Install: pip install apscheduler"
    )


class CronService:
    """Manages cron jobs and webhook triggers for browser automation."""

    def __init__(
        self,
        store_path: str | Path,
        *,
        max_jobs: int = 50,
        job_queue: Any = None,  # AgentJobQueue
        manager: Any = None,   # BrowserManager
    ):
        self._store_path = Path(store_path)
        self._max_jobs = max_jobs
        self.job_queue = job_queue
        self.manager = manager
        self._scheduler: Any = None
        self._lock = asyncio.Lock()

    async def startup(self) -> None:
        """Start the scheduler and register all enabled cron jobs."""
        if not _APSCHEDULER_AVAILABLE:
            return
        self._scheduler = AsyncIOScheduler()
        jobs = self._load()
        for job in jobs.values():
            if job.get("enabled") and job.get("schedule"):
                self._register_job(job)
        self._scheduler.start()
        logger.info("cron service started with %d jobs", len(jobs))

    async def shutdown(self) -> None:
        """Stop the scheduler."""
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)

    # ── CRUD ───────────────────────────────────────────────────────────────

    async def create_job(
        self,
        *,
        name: str,
        goal: str,
        provider: str = "openai",
        schedule: str | None = None,
        start_url: str | None = None,
        auth_profile: str | None = None,
        proxy_persona: str | None = None,
        max_steps: int = 20,
        enabled: bool = True,
        webhook_enabled: bool = False,
    ) -> dict[str, Any]:
        async with self._lock:
            jobs = self._load()
            if len(jobs) >= self._max_jobs:
                raise ValueError(f"Cron job limit reached ({self._max_jobs})")

            job_id = uuid4().hex[:12]
            webhook_key = secrets.token_hex(32) if webhook_enabled else None

            job: dict[str, Any] = {
                "id": job_id,
                "name": name,
                "provider": provider,
                "schedule": schedule,
                "enabled": enabled,
                "webhook_key": webhook_key,
                "goal": goal,
                "start_url": start_url,
                "auth_profile": auth_profile,
                "proxy_persona": proxy_persona,
                "max_steps": max_steps,
                "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "last_run_at": None,
                "last_status": None,
                "run_count": 0,
            }
            jobs[job_id] = job
            self._save(jobs)

            if enabled and schedule and self._scheduler is not None:
                self._register_job(job)

            return self._safe_job(job)

    async def list_jobs(self) -> list[dict[str, Any]]:
        return [self._safe_job(j) for j in self._load().values()]

    async def get_job(self, job_id: str) -> dict[str, Any]:
        jobs = self._load()
        if job_id not in jobs:
            raise KeyError(f"Cron job not found: {job_id}")
        return self._safe_job(jobs[job_id])

    async def update_job(self, job_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            jobs = self._load()
            if job_id not in jobs:
                raise KeyError(f"Cron job not found: {job_id}")
            job = jobs[job_id]
            allowed = {"name", "goal", "provider", "schedule", "enabled", "start_url",
                       "auth_profile", "proxy_persona", "max_steps"}
            for k, v in updates.items():
                if k in allowed:
                    job[k] = v
            jobs[job_id] = job
            self._save(jobs)

            # Re-register scheduler job if schedule/enabled changed
            if self._scheduler is not None:
                try:
                    self._scheduler.remove_job(job_id)
                except Exception:
                    pass
                if job.get("enabled") and job.get("schedule"):
                    self._register_job(job)

            return self._safe_job(job)

    async def delete_job(self, job_id: str) -> bool:
        async with self._lock:
            jobs = self._load()
            if job_id not in jobs:
                return False
            del jobs[job_id]
            self._save(jobs)
            if self._scheduler is not None:
                try:
                    self._scheduler.remove_job(job_id)
                except Exception:
                    pass
            return True

    # ── Webhook trigger ─────────────────────────────────────────────────────

    async def trigger_via_webhook(
        self, job_id: str, webhook_key: str
    ) -> dict[str, Any]:
        """Verify webhook key and enqueue the job for immediate execution."""
        jobs = self._load()
        if job_id not in jobs:
            raise KeyError(f"Cron job not found: {job_id}")
        job = jobs[job_id]
        stored_key = job.get("webhook_key")
        if not stored_key:
            raise PermissionError("This job does not have webhook triggering enabled")
        if not hmac.compare_digest(webhook_key, stored_key):
            raise PermissionError("Invalid webhook key")
        return await self._run_job_now(job)

    async def trigger_job(self, job_id: str) -> dict[str, Any]:
        """Trigger a job immediately (no auth — internal use only)."""
        jobs = self._load()
        if job_id not in jobs:
            raise KeyError(f"Cron job not found: {job_id}")
        return await self._run_job_now(jobs[job_id])

    # ── Internal ───────────────────────────────────────────────────────────

    def _register_job(self, job: dict[str, Any]) -> None:
        if not _APSCHEDULER_AVAILABLE or self._scheduler is None:
            return
        try:
            trigger = CronTrigger.from_crontab(job["schedule"])
            self._scheduler.add_job(
                self._scheduled_run,
                trigger=trigger,
                id=job["id"],
                args=[job["id"]],
                replace_existing=True,
            )
            logger.info("registered cron job %s: %s", job["id"], job["schedule"])
        except Exception as exc:
            logger.warning("failed to register cron job %s: %s", job["id"], exc)

    async def _scheduled_run(self, job_id: str) -> None:
        """Called by APScheduler on cron trigger."""
        try:
            jobs = self._load()
            if job_id not in jobs:
                return
            await self._run_job_now(jobs[job_id])
        except Exception as exc:
            logger.error("cron job %s failed: %s", job_id, exc)

    async def _run_job_now(self, job: dict[str, Any]) -> dict[str, Any]:
        """Enqueue the job with the AgentJobQueue and update run metadata."""
        job_id = job["id"]
        if self.job_queue is None or self.manager is None:
            raise RuntimeError("CronService not fully initialized (no job_queue/manager)")

        from .models import AgentRunRequest

        # Create session for the job
        session_result = await self.manager.create_session(
            name=f"cron-{job_id}",
            start_url=job.get("start_url"),
            auth_profile=job.get("auth_profile"),
            proxy_persona=job.get("proxy_persona"),
        )
        session_id = session_result["id"]

        # Enqueue agent run
        run_request = AgentRunRequest(
            provider=job.get("provider") or "openai",
            goal=job["goal"],
            max_steps=job.get("max_steps", 20),
        )
        queued = await self.job_queue.enqueue_run(session_id, run_request)

        # Update run metadata
        async with self._lock:
            jobs = self._load()
            if job_id in jobs:
                jobs[job_id]["last_run_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
                jobs[job_id]["last_status"] = "queued"
                jobs[job_id]["run_count"] = jobs[job_id].get("run_count", 0) + 1
                self._save(jobs)

        return {
            "triggered": True,
            "job_id": job_id,
            "session_id": session_id,
            "queued_job": queued,
        }

    def _load(self) -> dict[str, Any]:
        if not self._store_path.exists():
            return {}
        try:
            return json.loads(self._store_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("failed to load cron store %s: %s", self._store_path, exc)
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._store_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self._store_path)

    @staticmethod
    def _safe_job(job: dict[str, Any]) -> dict[str, Any]:
        """Return job dict with webhook_key masked."""
        result = dict(job)
        if result.get("webhook_key"):
            result["webhook_key_preview"] = result["webhook_key"][:8] + "..."
            result["webhook_enabled"] = True
        else:
            result["webhook_enabled"] = False
        result.pop("webhook_key", None)
        return result
