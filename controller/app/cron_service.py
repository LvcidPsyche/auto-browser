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
    "webhook_key_hash": str | null (SHA-256 of the 256-bit random secret for POST /crons/{id}/trigger; the secret is
                   shown once, by create or rotate, and is never read back),
    "goal":        str (natural language goal for agent),
    "start_url":   str | null,
    "auth_profile":str | null,
    "proxy_persona":str | null,
    "max_steps":   int (default 20),
    "workflow_profile": "fast" | "governed" (the creating caller's; absent in jobs from older versions = "fast"),
    "owner":       str | null (token-verified operator who created it),
    "created_at":  ISO datetime,
    "last_run_at": ISO datetime | null,
    "last_status": str | null ("queued", then how that run's agent job ended: "completed", "failed", "cancelled",
                   "interrupted" or "discarded", or "unknown" when its record could not say; or
                   "refused: owner credential removed" when a fire was refused),
    "run_count":   int,
  }

Depends on APScheduler for cron execution (optional install).
If APScheduler is not available, cron scheduling is disabled but webhook triggers still work.
"""

from __future__ import annotations

import asyncio
import glob
import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

logger = logging.getLogger(__name__)

from .auth_policy import credential_names_operator
from .session_ownership import ANONYMOUS, acting_as, as_system, may_use, owner_identity, verified_operator
from .utils import PRIVATE_FILE_MODE, UTC, atomic_write_text

if TYPE_CHECKING:
    from .models import WorkflowProfile

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    _APSCHEDULER_AVAILABLE = True
except ImportError:
    _APSCHEDULER_AVAILABLE = False
    logger.info("APScheduler not installed — cron scheduling disabled. Install: pip install apscheduler")


# What an unknown id, a job with no key and a wrong key are refused with: nothing says which it was.
WEBHOOK_KEY_REFUSED = "Invalid webhook key"
# How a run's agent job can end, as AgentJobRecord.status says it. Anything else means its end was never written.
_FINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted", "discarded"})
# The run ended (its finish callback came), and its agent job's record cannot say how.
RUN_OUTCOME_UNKNOWN = "unknown"
# A fire refused because no named credential proves the job's owner any more: the answer's reason, and last_status.
OWNER_CREDENTIAL_REMOVED = "owner_credential_removed"
OWNER_CREDENTIAL_REMOVED_STATUS = "refused: owner credential removed"


def _hash_webhook_key(key: str) -> str:
    """SHA-256 of a webhook key: the key is 256 random bits, so nothing slower is needed to protect it at rest."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _webhook_key_id(key_hash: str) -> str:
    """A non-secret id for telling keys apart: derived from the hash, and neither the hash nor any part of it."""
    return hashlib.sha256(f"cron-webhook-key-id:{key_hash}".encode("utf-8")).hexdigest()[:12]


# Compared against when there is no key to compare with, so a refusal costs the same whatever it was for.
_NO_WEBHOOK_KEY_DIGEST = _hash_webhook_key("no webhook key is stored for this job")


class CronJobDisabledError(RuntimeError):
    """A disabled job was asked to run."""


# Fixed and path-free, because callers (MCP above all) show it as it is; the path and the cause are logged.
CRON_STORE_UNREADABLE = (
    "the cron store (CRON_STORE_PATH) is unreadable; cron jobs are not loaded until it is fixed. The server log has "
    "the cause and where the file is."
)


class CronStoreUnreadableError(RuntimeError):
    """The cron store could not be used; the text is the fixed CRON_STORE_UNREADABLE (REST answers it as 503)."""

    def __init__(self, *_args: object) -> None:
        # *_args: copy and pickle rebuild an exception from its args, which hold the fixed text.
        super().__init__(CRON_STORE_UNREADABLE)


class CronService:
    """Manages cron jobs and webhook triggers for browser automation."""

    def __init__(
        self,
        store_path: str | Path,
        *,
        max_jobs: int = 50,
        job_queue: Any = None,  # AgentJobQueue
        manager: Any = None,  # BrowserManager
    ):
        self._store_path = Path(store_path)
        self._max_jobs = max_jobs
        self.job_queue = job_queue
        self.manager = manager
        self._scheduler: Any = None
        self._lock = asyncio.Lock()
        # cron job id -> session id currently hosting its run. Guards against
        # overlapping fires and records what _release_run must close.
        self._active_runs: dict[str, str] = {}

    async def startup(self) -> None:
        """Start the scheduler and register all enabled cron jobs."""
        # Before the scheduler check: it needs only the store, so a deployment without APScheduler is told too.
        jobs = self._load()
        self._warn_if_store_was_quarantined(jobs)
        await self._hash_legacy_webhook_keys()
        if not _APSCHEDULER_AVAILABLE:
            return
        self._scheduler = AsyncIOScheduler()
        for job in jobs.values():
            if job.get("enabled") and job.get("schedule"):
                self._register_job(job)
        self._scheduler.start()
        logger.info("cron service started with %d jobs", len(jobs))

    async def _hash_legacy_webhook_keys(self) -> None:
        """Replace keys an earlier version stored as they are with their hash: once, at startup, never on a read.

        Before the scheduler check, as webhook triggers work without APScheduler. A store that cannot be re-saved is
        only logged, and goes on working: a job still holding its key is compared by hashing that key.
        """
        async with self._lock:
            jobs = self._load()
            legacy = [job for job in jobs.values() if job.get("webhook_key")]
            if not legacy:
                return
            for job in legacy:
                job["webhook_key_hash"] = self._stored_key_hash(job)
                del job["webhook_key"]
            try:
                self._save(jobs)
            except OSError as exc:
                logger.error(
                    "cron store %s: %d job(s) still hold their webhook key in plaintext, as the store could not be "
                    "re-saved with their hashes: %s",
                    self._store_path,
                    len(legacy),
                    exc,
                )

    def _warn_if_store_was_quarantined(self, jobs: dict[str, Any]) -> None:
        """Say at every start that an empty store follows a quarantined one.

        _load raises once, when it moves a corrupt store aside; the next start finds no file and reads an empty
        store with no sign that the jobs are in the quarantined file.
        """
        if jobs:
            return
        for quarantined in sorted(self._store_path.parent.glob(f"{glob.escape(self._store_path.stem)}.corrupt-*.json")):
            logger.warning(
                "cron store %s holds no jobs, and a corrupt store was set aside as %s; restore its jobs from "
                "that file if they are still wanted, and move or delete that file to silence this warning",
                self._store_path,
                quarantined,
            )

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
        workflow_profile: WorkflowProfile = "fast",
    ) -> dict[str, Any]:
        self._validate_schedule(schedule)
        self._require_profile_access(auth_profile, owner=verified_operator())
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
                "webhook_key_hash": _hash_webhook_key(webhook_key) if webhook_key else None,
                "goal": goal,
                "start_url": start_url,
                "auth_profile": auth_profile,
                "proxy_persona": proxy_persona,
                "max_steps": max_steps,
                # Every run starts under the profile the creating call ran under.
                "workflow_profile": workflow_profile,
                # A job fires as the operator who created it: its session is
                # theirs, and it may open only auth profiles they may open.
                "owner": verified_operator(),
                "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "last_run_at": None,
                "last_status": None,
                "run_count": 0,
            }
            jobs[job_id] = job
            self._save(jobs)

            if enabled and schedule and self._scheduler is not None:
                self._register_job(job)

            created = self._safe_job(job)
            if webhook_key:
                # The one time the full key is disclosed: the store keeps only its hash, and every read strips
                # that, so a caller who does not read it here must rotate the key to get a new one.
                created["webhook_key"] = webhook_key
            return created

    async def list_jobs(self) -> list[dict[str, Any]]:
        return [self._safe_job(j) for j in self._load().values() if may_use(j.get("owner"))]

    async def get_job(self, job_id: str) -> dict[str, Any]:
        return self._safe_job(self._accessible(self._load(), job_id))

    @staticmethod
    def _accessible(jobs: dict[str, Any], job_id: str) -> dict[str, Any]:
        """Another operator's job answers as if it did not exist."""
        job = jobs.get(job_id)
        if job is None or not may_use(job.get("owner")):
            raise KeyError(f"Cron job not found: {job_id}")
        return job

    def _require_profile_access(self, auth_profile: str | None, *, owner: str | None) -> None:
        """Refuse a profile the job could not open when it fires, as whom it fires.

        Checked when the job is defined, not first when it fires: a job is not a
        way to reach a profile its creator could not open directly.
        """
        if not auth_profile or self.manager is None:
            return
        with acting_as(owner_identity(owner) or ANONYMOUS):
            self.manager.auth_profiles.require_access(auth_profile, action="scheduling a cron job with it")

    async def update_job(self, job_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        if "schedule" in updates:
            self._validate_schedule(updates["schedule"])
        async with self._lock:
            jobs = self._load()
            job = self._accessible(jobs, job_id)
            if "auth_profile" in updates:
                self._require_profile_access(updates["auth_profile"], owner=job.get("owner"))
            allowed = {
                "name",
                "goal",
                "provider",
                "schedule",
                "enabled",
                "start_url",
                "auth_profile",
                "proxy_persona",
                "max_steps",
            }
            for k, v in updates.items():
                if k in allowed:
                    job[k] = v
            jobs[job_id] = job
            self._save(jobs)

            # Re-register scheduler job if schedule/enabled changed
            if self._scheduler is not None:
                try:
                    self._scheduler.remove_job(job_id)
                except Exception as exc:
                    logger.warning("failed to remove cron job %s during update: %s", job_id, exc)
                if job.get("enabled") and job.get("schedule"):
                    self._register_job(job)

            return self._safe_job(job)

    async def delete_job(self, job_id: str) -> bool:
        async with self._lock:
            jobs = self._load()
            if job_id not in jobs or not may_use(jobs[job_id].get("owner")):
                return False
            del jobs[job_id]
            self._save(jobs)
            if self._scheduler is not None:
                try:
                    self._scheduler.remove_job(job_id)
                except Exception as exc:
                    logger.warning("failed to remove cron job %s during delete: %s", job_id, exc)
            return True

    # ── Webhook key ─────────────────────────────────────────────────────────

    async def rotate_webhook_key(self, job_id: str) -> dict[str, Any]:
        """Give the job a new webhook key, or its first: the only time this key is ever shown."""
        new_key = secrets.token_hex(32)
        async with self._lock:
            jobs = self._load()
            job = self._accessible(jobs, job_id)
            job.pop("webhook_key", None)  # a plaintext key from an older store does not outlive its replacement
            job["webhook_key_hash"] = _hash_webhook_key(new_key)
            self._save(jobs)
            rotated = self._safe_job(job)
        await self._audit_quietly(
            job_id,
            event_type="cron_webhook_key_rotated",
            status="ok",
            action="rotate_cron_webhook_key",
            details={"cron_job": job_id, "webhook_key_id": rotated["webhook_key_id"]},
        )
        rotated["webhook_key"] = new_key
        return rotated

    async def revoke_webhook_key(self, job_id: str) -> dict[str, Any]:
        """Remove the job's webhook key, after which its webhook is refused as a job without a key is."""
        async with self._lock:
            jobs = self._load()
            job = self._accessible(jobs, job_id)
            key_hash = self._stored_key_hash(job)
            if key_hash:
                job.pop("webhook_key", None)
                job.pop("webhook_key_hash", None)
                self._save(jobs)
        if key_hash:
            await self._audit_quietly(
                job_id,
                event_type="cron_webhook_key_revoked",
                status="ok",
                action="revoke_cron_webhook_key",
                details={"cron_job": job_id, "webhook_key_id": _webhook_key_id(key_hash)},
            )
        return {"job_id": job_id, "revoked": bool(key_hash)}

    # ── Webhook trigger ─────────────────────────────────────────────────────

    async def trigger_via_webhook(self, job_id: str, webhook_key: str) -> dict[str, Any]:
        """Verify webhook key and enqueue the job for immediate execution."""
        job = self._load().get(job_id)
        stored_hash = self._stored_key_hash(job) if job is not None else None
        # An unknown id, a job with no key and a wrong key are one refusal, after one comparison: what differs is
        # which hash there was to compare against. Compared as bytes: compare_digest raises TypeError for a str with
        # non-ASCII characters, which surfaced as a 500 instead of a refusal.
        matched = hmac.compare_digest(
            _hash_webhook_key(webhook_key).encode("utf-8"), (stored_hash or _NO_WEBHOOK_KEY_DIGEST).encode("utf-8")
        )
        if not matched or stored_hash is None:
            raise PermissionError(WEBHOOK_KEY_REFUSED)
        return await self._run_job_now(job)

    async def trigger_job(self, job_id: str) -> dict[str, Any]:
        """Trigger a job immediately (no auth — internal use only)."""
        return await self._run_job_now(self._accessible(self._load(), job_id))

    # ── Internal ───────────────────────────────────────────────────────────

    @staticmethod
    def _validate_schedule(schedule: str | None) -> None:
        """Reject a cron expression the scheduler cannot parse.

        _register_job only logs a parse failure, so an invalid schedule was
        saved and reported as created, then silently never ran.
        """
        if not schedule or not _APSCHEDULER_AVAILABLE:
            return
        try:
            CronTrigger.from_crontab(schedule)
        except ValueError as exc:
            raise ValueError(f"Invalid cron schedule {schedule!r}: {exc}") from None

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
        # The scheduler already drops a disabled job; a webhook or trigger_job must not run it either.
        if not job.get("enabled", True):
            raise CronJobDisabledError("job is disabled; enable it to run it")

        # From the settings as they are now, whoever fired it: a job does not outlive its owner's credential. A
        # deployment moved to the shared token or to none has no named credential left, so it refuses named owners too.
        owner_id = job.get("owner")
        if owner_id and not credential_names_operator(self.manager.settings, owner_id):
            return await self._refuse_run(job_id, owner_id)

        from .models import AGENT_RUN_MAX_STEPS, AgentRunRequest

        # Refuse to start a second run while this job's previous one is still in
        # flight. Without this, a schedule faster than its own runtime stacks up
        # sessions and hits the session cap even with the release below.
        if job_id in self._active_runs:
            logger.warning(
                "cron job %s skipped: previous run (session %s) is still active",
                job_id,
                self._active_runs[job_id],
            )
            return {"triggered": False, "job_id": job_id, "reason": "previous_run_active"}

        # Built before the session exists: this used to run after
        # create_session, so a job the request model rejects (older stores
        # accepted max_steps up to 100) left the session open and the job
        # marked in flight, and every later fire was skipped until restart.
        run_request = AgentRunRequest(
            provider=job.get("provider") or "openai",
            goal=job["goal"],
            max_steps=min(job.get("max_steps") or 20, AGENT_RUN_MAX_STEPS),
            workflow_profile=job.get("workflow_profile") or "fast",
        )

        # Claim the slot before the first await, so a webhook and a scheduled
        # fire arriving together cannot both pass the check above.
        self._active_runs[job_id] = ""
        # Fires as the job's owner, whoever triggered it (the scheduler, a
        # webhook): the session and the agent job are theirs. An unowned job
        # fires as nobody: acting_as(None) would leave the triggering operator in
        # place, and the session and agent job it creates would become theirs.
        owner = owner_identity(job.get("owner")) or ANONYMOUS
        try:
            with acting_as(owner):
                session_result = await self.manager.create_session(
                    name=f"cron-{job_id}",
                    start_url=job.get("start_url"),
                    auth_profile=job.get("auth_profile"),
                    proxy_persona=job.get("proxy_persona"),
                )
        except BaseException:
            self._active_runs.pop(job_id, None)
            raise
        session_id = session_result["id"]
        self._active_runs[job_id] = session_id

        # This session exists only to host this job, and nothing else will ever
        # close it. Before this, the first cron fire consumed a session slot
        # permanently — with MAX_SESSIONS defaulting to 1, every later fire and
        # every manual create_session then failed "Session limit reached", and
        # under docker_ephemeral each fire also leaked a browser container.
        # The callback goes in with the job: a worker can finish it before enqueue_run returns.
        try:
            with acting_as(owner):
                queued = await self.job_queue.enqueue_run(
                    session_id,
                    run_request,
                    on_finish=lambda agent_job_id: self._finish_run(job_id, session_id, agent_job_id),
                )
        except BaseException:
            # Enqueue can reject (queue at capacity), and the trigger can be cancelled while it waits. The
            # session exists but nothing will ever run in it, so release it here rather than stranding it
            # (and the job's slot) until restart. A job the queue already accepted is withdrawn by the queue.
            await self._release_run(job_id, session_id)
            raise

        # Update run metadata. The run is queued whatever happens here: a store that fails now does not make it untrue.
        try:
            async with self._lock:
                jobs = self._load()
                if job_id in jobs:
                    jobs[job_id]["last_run_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
                    # A run no longer in the slot has finished and said how (_finish_run), or a newer run has
                    # taken it and says what the status is: "queued" must not land over either.
                    if self._active_runs.get(job_id) == session_id:
                        jobs[job_id]["last_status"] = "queued"
                    jobs[job_id]["run_count"] = jobs[job_id].get("run_count", 0) + 1
                    self._save(jobs)
        except (CronStoreUnreadableError, OSError) as exc:
            logger.error("cron job %s: its run is queued, but the run's metadata could not be saved: %s", job_id, exc)

        return {
            "triggered": True,
            "job_id": job_id,
            "session_id": session_id,
            "queued_job": queued,
        }

    async def _refuse_run(self, job_id: str, owner_id: str) -> dict[str, Any]:
        """Refuse a fire whose owner no named credential proves any more: no session, no agent job, one audit event."""
        logger.warning("cron job %s refused: no named credential proves its owner %s any more", job_id, owner_id)
        try:
            async with self._lock:
                jobs = self._load()
                if job_id in jobs:
                    jobs[job_id]["last_status"] = OWNER_CREDENTIAL_REMOVED_STATUS
                    self._save(jobs)
        except (CronStoreUnreadableError, OSError) as exc:
            logger.error("cron job %s: its refusal could not be saved in last_status: %s", job_id, exc)
        await self._audit_quietly(
            job_id,
            event_type="cron_job_refused",
            status="refused",
            action="fire_cron_job",
            details={"cron_job": job_id, "owner": owner_id, "reason": OWNER_CREDENTIAL_REMOVED},
        )
        return {"triggered": False, "job_id": job_id, "reason": OWNER_CREDENTIAL_REMOVED}

    async def _audit_quietly(self, job_id: str, *, event_type: str, status: str, action: str, details: dict) -> None:
        """Audit what has already happened: a failure is logged, never raised over it."""
        if self.manager is None:
            return
        try:
            await self.manager.audit.append(event_type=event_type, status=status, action=action, details=details)
        except Exception:
            logger.exception("cron job %s: the %s audit event could not be written", job_id, event_type)

    async def _finish_run(self, job_id: str, session_id: str, agent_job_id: str) -> None:
        """The agent job's finish callback: record how the run ended in last_status, then release its session.

        It can run before enqueue_run returns. The write follows _release_run's rule: only the run that still holds the
        slot writes, under the store lock, so an old run never writes over a newer run's status; the slot is given up
        in the same step of the event loop as that write, so the trigger's later "queued" (written only by a run in
        the slot) cannot land over it. A store that cannot be read or saved is logged, and the run is released anyway.
        """
        try:
            status = await self._run_outcome(job_id, agent_job_id)
            async with self._lock:
                if self._active_runs.get(job_id) == session_id:
                    jobs = self._load()
                    if job_id in jobs:
                        jobs[job_id]["last_status"] = status
                        self._save(jobs)
        except (CronStoreUnreadableError, OSError) as exc:
            logger.error("cron job %s: how run %s ended could not be saved: %s", job_id, agent_job_id, exc)
        finally:
            await self._release_run(job_id, session_id)

    async def _run_outcome(self, job_id: str, agent_job_id: str) -> str:
        """How a finished agent job ended, from its record; RUN_OUTCOME_UNKNOWN when the record cannot say."""
        try:
            status = (await self.job_queue.store.get(agent_job_id)).status
        except (KeyError, OSError, ValueError) as exc:
            logger.error("cron job %s: the record of agent job %s could not be read: %s", job_id, agent_job_id, exc)
            return RUN_OUTCOME_UNKNOWN
        return status if status in _FINAL_JOB_STATUSES else RUN_OUTCOME_UNKNOWN

    async def _release_run(self, job_id: str, session_id: str) -> None:
        """Close the session a cron run was given, and clear the in-flight mark.

        Called from the job queue's finish callback on completion, failure and
        cancellation alike, and by the trigger when queuing fails or is cancelled.
        A cancelled trigger can do both: withdrawing a running job fires the
        callback, then its own cleanup runs. Only the call that still finds this
        run in the slot acts, so the session is closed once and a slot a newer
        trigger took in between is left alone. The mark is cleared even if the
        close fails — otherwise one stuck close would block that cron job forever.
        """
        if self._active_runs.get(job_id) != session_id:
            return
        del self._active_runs[job_id]
        if self.manager is None:
            return
        try:
            # Housekeeping, from the job queue's finish callback: no operator.
            with as_system():
                await self.manager.close_session(session_id)
        except Exception as exc:
            logger.warning(
                "cron job %s: failed to close session %s: %s",
                job_id,
                session_id,
                exc,
            )

    def _load(self) -> dict[str, Any]:
        # The read itself decides whether there is a store: Path.exists() raises a permission error as itself
        # (EACCES on Python 3.11 and 3.12), outside the handling below.
        try:
            data = json.loads(self._store_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except OSError as exc:
            # Possibly passing (a sharing violation, a brief permission fault): a good, hand-edited store is
            # never moved aside for one. It stays where it is, and reads fail until it can be read.
            logger.error("cron store %s could not be read (%s); it was left where it is", self._store_path, exc)
            raise CronStoreUnreadableError from exc
        except (ValueError, RecursionError) as exc:  # json.JSONDecodeError, UnicodeDecodeError: the file is corrupt
            self._quarantine(exc)
            raise CronStoreUnreadableError from exc
        if not isinstance(data, dict):
            # It parses, but every caller would fail on it later (a list has no .values()).
            self._quarantine(f"it holds a JSON {type(data).__name__}, not an object")
            raise CronStoreUnreadableError
        return data

    def _quarantine(self, cause: object) -> None:
        """Move a corrupt store aside, so that no save can write over the jobs in it.

        Do NOT answer {} instead. That looked harmless but was destructive: the next
        _save() persisted the empty dict over the damaged file, permanently erasing
        every cron job the operator had defined. A fallback that hides a bug is worse
        than the bug.
        """
        quarantine = self._store_path.with_suffix(f".corrupt-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json")
        try:
            self._store_path.replace(quarantine)
            logger.error(
                "cron store %s is unreadable (%s); moved to %s. Cron jobs are not loaded until this is resolved.",
                self._store_path,
                cause,
                quarantine,
            )
        except Exception:
            logger.exception("cron store %s is unreadable (%s) and could not be quarantined", self._store_path, cause)

    def _save(self, data: dict[str, Any]) -> None:
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self._store_path, json.dumps(data, indent=2), mode=PRIVATE_FILE_MODE)

    @staticmethod
    def _stored_key_hash(job: dict[str, Any]) -> str | None:
        """The hash of the job's webhook key, or None. A key still held as it is (an unmigrated store) is hashed."""
        if job.get("webhook_key_hash"):
            return str(job["webhook_key_hash"])
        legacy_key = job.get("webhook_key")
        return _hash_webhook_key(str(legacy_key)) if legacy_key else None

    @classmethod
    def _safe_job(cls, job: dict[str, Any]) -> dict[str, Any]:
        """The job as callers see it: neither the key nor its hash, and a non-secret id instead of them."""
        key_hash = cls._stored_key_hash(job)
        result = {key: value for key, value in job.items() if key not in {"webhook_key", "webhook_key_hash"}}
        result["webhook_enabled"] = key_hash is not None
        if key_hash is not None:
            result["webhook_key_id"] = _webhook_key_id(key_hash)
        return result
