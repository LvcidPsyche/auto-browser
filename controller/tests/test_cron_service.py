from __future__ import annotations

import asyncio
import hashlib
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from apscheduler.triggers.cron import CronTrigger
from pydantic import ValidationError

from app.app_factory import build_controller_services
from app.audit import get_current_operator
from app.config import Settings
from app.cron_service import CronJobDisabledError, CronService, CronStoreUnreadableError
from app.models import OperatorIdentity
from app.session_ownership import acting_as


class CronServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store_path = Path(self.tmp.name) / "crons.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_crud_masks_webhook_key_and_enforces_limits(self) -> None:
        service = CronService(self.store_path, max_jobs=1)

        created = await service.create_job(
            name="daily check",
            goal="check status",
            schedule="0 9 * * *",
            webhook_enabled=True,
        )

        self.assertEqual(created["name"], "daily check")
        self.assertTrue(created["webhook_enabled"])
        # Disclosed once, on creation — otherwise no caller could ever trigger it.
        self.assertEqual(len(created["webhook_key"]), 64)
        self.assertNotIn("webhook_key_preview", created)
        self.assertNotIn("webhook_key_hash", created)
        self.assertFalse(created["webhook_key"].startswith(created["webhook_key_id"]))
        fetched = await service.get_job(created["id"])
        self.assertEqual(fetched["id"], created["id"])
        self.assertNotIn("webhook_key", fetched)
        self.assertNotIn("webhook_key_hash", fetched)
        self.assertEqual(fetched["webhook_key_id"], created["webhook_key_id"])
        listed = await service.list_jobs()
        self.assertEqual(len(listed), 1)
        self.assertNotIn("webhook_key", listed[0])
        self.assertNotIn("webhook_key_hash", listed[0])

        with self.assertRaises(ValueError):
            await service.create_job(name="overflow", goal="nope")

        updated = await service.update_job(created["id"], {"name": "renamed", "ignored": "value"})
        self.assertEqual(updated["name"], "renamed")
        self.assertNotIn("ignored", updated)

        self.assertFalse(await service.delete_job("missing"))
        self.assertTrue(await service.delete_job(created["id"]))
        with self.assertRaises(KeyError):
            await service.get_job(created["id"])

    async def test_trigger_job_creates_session_queues_run_and_updates_metadata(self) -> None:
        manager = MagicMock()
        manager.create_session = AsyncMock(return_value={"id": "session-1"})
        queue = MagicMock()
        queue.enqueue_run = AsyncMock(return_value={"id": "agent-job-1"})
        service = CronService(self.store_path, job_queue=queue, manager=manager)
        created = await service.create_job(
            name="webhook",
            goal="summarize dashboard",
            start_url="https://example.com",
            auth_profile="ops",
            proxy_persona="us-east",
            webhook_enabled=True,
        )
        raw = service._load()[created["id"]]

        self.assertNotIn("webhook_key", raw)
        self.assertEqual(raw["webhook_key_hash"], hashlib.sha256(created["webhook_key"].encode("utf-8")).hexdigest())

        with self.assertRaises(PermissionError):
            await service.trigger_via_webhook(created["id"], "wrong")
        # Non-ASCII made hmac.compare_digest raise TypeError — a 500, not a refusal.
        with self.assertRaises(PermissionError):
            await service.trigger_via_webhook(created["id"], "clé-invalide")
        with self.assertRaises(KeyError):
            await service.trigger_job("missing")

        result = await service.trigger_via_webhook(created["id"], created["webhook_key"])

        self.assertTrue(result["triggered"])
        manager.create_session.assert_awaited_once_with(
            name=f"cron-{created['id']}",
            start_url="https://example.com",
            auth_profile="ops",
            proxy_persona="us-east",
        )
        queue.enqueue_run.assert_awaited_once()
        stored = service._load()[created["id"]]
        self.assertEqual(stored["last_status"], "queued")
        self.assertEqual(stored["run_count"], 1)

    async def _disabled_job(self, **create):
        manager = MagicMock()
        manager.create_session = AsyncMock(return_value={"id": "session-1"})
        queue = MagicMock()
        queue.enqueue_run = AsyncMock(return_value={"id": "agent-job-1"})
        service = CronService(self.store_path, job_queue=queue, manager=manager)
        job = await service.create_job(name="n", goal="g", schedule="0 9 * * *", webhook_enabled=True, **create)
        await service.update_job(job["id"], {"enabled": False})
        return service, job, manager

    async def test_a_disabled_job_is_not_run_by_webhook_or_trigger_job(self) -> None:
        service, job, manager = await self._disabled_job()

        with self.assertRaisesRegex(CronJobDisabledError, "job is disabled; enable it to run it"):
            await service.trigger_via_webhook(job["id"], job["webhook_key"])
        with self.assertRaisesRegex(CronJobDisabledError, "job is disabled; enable it to run it"):
            await service.trigger_job(job["id"])
        manager.create_session.assert_not_awaited()

        # A wrong key is still a permission error: a disabled job does not tell a stranger its state.
        with self.assertRaises(PermissionError):
            await service.trigger_via_webhook(job["id"], "wrong")

        await service.update_job(job["id"], {"enabled": True})
        self.assertTrue((await service.trigger_job(job["id"]))["triggered"])
        manager.create_session.assert_awaited_once()

    async def test_the_trigger_route_answers_409_for_a_disabled_job(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.routes.operations import create_operations_router

        service, job, manager = await self._disabled_job()
        app = FastAPI()
        app.include_router(create_operations_router(manager=manager, proxy_store=None, cron_service=service))
        response = TestClient(app, raise_server_exceptions=False).post(
            f"/crons/{job['id']}/trigger", json={"webhook_key": job["webhook_key"]}
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("job is disabled", response.json()["detail"])

    async def test_scheduler_registration_and_store_error_paths(self) -> None:
        service = CronService(self.store_path)
        scheduler = MagicMock()
        scheduler.remove_job.side_effect = RuntimeError("missing")
        service._scheduler = scheduler
        created = await service.create_job(name="scheduled", goal="run", schedule="0 9 * * *")

        updated = await service.update_job(created["id"], {"enabled": False, "schedule": "0 10 * * *"})
        self.assertFalse(updated["enabled"])
        scheduler.remove_job.assert_called_with(created["id"])
        self.assertIsInstance(updated["run_count"], int)

        # A corrupt store must fail loudly and be quarantined. This previously
        # asserted `_load() == {}` — pinning the destructive behaviour as
        # correct, because the next _save() then wrote that empty dict over the
        # damaged file and permanently erased every cron job.
        self.store_path.write_text("{bad json", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            service._load()
        self.assertFalse(self.store_path.exists())
        quarantined = list(self.store_path.parent.glob("crons.corrupt-*.json"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_text(encoding="utf-8"), "{bad json")

        uninitialized = CronService(self.store_path)
        with self.assertRaises(RuntimeError):
            await uninitialized._run_job_now({"id": "job-1", "goal": "run"})

    async def test_remove_job_failures_are_logged(self) -> None:
        service = CronService(self.store_path)
        scheduler = MagicMock()
        scheduler.remove_job.side_effect = RuntimeError("missing")
        service._scheduler = scheduler
        created = await service.create_job(name="scheduled", goal="run", schedule="0 9 * * *")

        with patch("app.cron_service.logger.warning") as mock_warning:
            await service.update_job(created["id"], {"enabled": False, "schedule": "0 10 * * *"})
            await service.delete_job(created["id"])

        self.assertEqual(mock_warning.call_count, 2)
        self.assertEqual(mock_warning.call_args_list[0][0][0], "failed to remove cron job %s during update: %s")
        self.assertEqual(mock_warning.call_args_list[1][0][0], "failed to remove cron job %s during delete: %s")

    async def test_invalid_schedule_is_rejected_not_silently_saved(self) -> None:
        service = CronService(self.store_path)
        with self.assertRaises(ValueError):
            await service.create_job(name="bad", goal="run", schedule="every morning")
        self.assertEqual(service._load(), {})
        created = await service.create_job(name="ok", goal="run", schedule="0 9 * * *")
        with self.assertRaises(ValueError):
            await service.update_job(created["id"], {"schedule": "61 * * * *"})
        self.assertEqual(service._load()[created["id"]]["schedule"], "0 9 * * *")

    async def test_legacy_job_over_step_limit_runs_and_leaks_nothing(self) -> None:
        manager = MagicMock()
        manager.create_session = AsyncMock(return_value={"id": "session-1"})
        queue = MagicMock()
        queue.enqueue_run = AsyncMock(return_value={"id": "agent-job-1"})
        service = CronService(self.store_path, job_queue=queue, manager=manager)
        created = await service.create_job(name="legacy", goal="run", webhook_enabled=True)
        # Stores written before the input cap matched AgentRunRequest hold these.
        jobs = service._load()
        jobs[created["id"]]["max_steps"] = 50
        service._save(jobs)

        result = await service.trigger_job(created["id"])

        self.assertTrue(result["triggered"])
        self.assertEqual(queue.enqueue_run.await_args.args[1].max_steps, 20)

    async def test_unbuildable_run_fails_before_opening_a_session(self) -> None:
        manager = MagicMock()
        manager.create_session = AsyncMock(return_value={"id": "session-1"})
        service = CronService(self.store_path, job_queue=MagicMock(), manager=manager)

        with self.assertRaises(ValueError):
            await service._run_job_now({"id": "job-1", "goal": "x" * 5000})

        manager.create_session.assert_not_awaited()
        self.assertEqual(service._active_runs, {})

    async def test_concurrent_fires_start_one_run(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_create_session(**_kwargs):
            started.set()
            await release.wait()
            return {"id": "session-1"}

        manager = MagicMock()
        manager.create_session = AsyncMock(side_effect=slow_create_session)
        queue = MagicMock()
        queue.enqueue_run = AsyncMock(return_value={"id": "agent-job-1"})
        service = CronService(self.store_path, job_queue=queue, manager=manager)
        created = await service.create_job(name="dup", goal="run", webhook_enabled=True)

        first = asyncio.create_task(service.trigger_job(created["id"]))
        await started.wait()
        try:
            # Bounded: before the fix this second fire blocked in create_session.
            second = await asyncio.wait_for(service.trigger_job(created["id"]), timeout=5)
        finally:
            release.set()
        self.assertTrue((await first)["triggered"])

        self.assertEqual(second["reason"], "previous_run_active")
        manager.create_session.assert_awaited_once()

    async def test_failed_session_create_clears_in_flight_mark(self) -> None:
        manager = MagicMock()
        manager.create_session = AsyncMock(side_effect=RuntimeError("Session limit reached"))
        service = CronService(self.store_path, job_queue=MagicMock(), manager=manager)
        created = await service.create_job(name="capped", goal="run", webhook_enabled=True)

        with self.assertRaises(RuntimeError):
            await service.trigger_job(created["id"])

        self.assertEqual(service._active_runs, {})

    async def test_a_store_that_fails_after_the_run_is_queued_still_answers_triggered(self) -> None:
        for name, patch_store in (
            ("unreadable", lambda service: patch.object(service, "_load", side_effect=CronStoreUnreadableError())),
            ("unsaveable", lambda service: patch.object(service, "_save", side_effect=OSError("disk full"))),
        ):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                manager = MagicMock()
                manager.create_session = AsyncMock(return_value={"id": "session-1"})
                queue = MagicMock()
                queue.enqueue_run = AsyncMock(return_value={"id": "agent-job-1"})
                service = CronService(Path(tmp) / "crons.json", job_queue=queue, manager=manager)
                created = await service.create_job(name="n", goal="g", webhook_enabled=True)
                job = service._load()[created["id"]]

                with patch_store(service), self.assertLogs("app.cron_service", level="ERROR") as logged:
                    result = await service._run_job_now(job)

                self.assertTrue(result["triggered"])
                self.assertEqual(result["session_id"], "session-1")
                self.assertEqual(result["queued_job"], {"id": "agent-job-1"})
                self.assertTrue(any(created["id"] in line for line in logged.output), logged.output)

    async def test_cancel_during_enqueue_frees_the_slot_and_closes_the_session(self) -> None:
        started = asyncio.Event()

        async def hang_in_enqueue(*_args, **_kwargs):
            started.set()
            await asyncio.sleep(30)

        manager = MagicMock()
        manager.create_session = AsyncMock(return_value={"id": "session-1"})
        manager.close_session = AsyncMock()
        queue = MagicMock()
        queue.enqueue_run = AsyncMock(side_effect=hang_in_enqueue)
        service = CronService(self.store_path, job_queue=queue, manager=manager)
        created = await service.create_job(name="cancelled", goal="run", webhook_enabled=True)

        task = asyncio.create_task(service.trigger_job(created["id"]))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(service._active_runs, {})
        manager.close_session.assert_awaited_once_with("session-1")
        queue.enqueue_run = AsyncMock(return_value={"id": "agent-job-1"})
        self.assertTrue((await service.trigger_job(created["id"]))["triggered"])


class CronQuarantineWarningTests(unittest.IsolatedAsyncioTestCase):
    """After a corrupt store is set aside, every later start says so while the store is empty."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store_path = Path(self.tmp.name) / "crons.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def _start_and_stop(self) -> None:
        service = CronService(self.store_path)
        await service.startup()
        await service.shutdown()

    async def test_each_start_warns_while_a_quarantine_file_exists_and_the_store_is_empty(self) -> None:
        quarantined = self.store_path.with_suffix(".corrupt-20261105T042259Z.json")
        quarantined.write_text("{truncated", encoding="utf-8")

        for _ in range(2):
            with self.assertLogs("app.cron_service", level="WARNING") as logged:
                await self._start_and_stop()
            self.assertTrue(any(str(quarantined) in line for line in logged.output), logged.output)

    async def test_the_warning_follows_the_real_quarantine_and_says_how_to_silence_it(self) -> None:
        self.store_path.write_text("{truncated", encoding="utf-8")
        with self.assertRaises(RuntimeError), self.assertLogs("app.cron_service", level="ERROR"):
            await self._start_and_stop()
        quarantined = list(Path(self.tmp.name).glob("crons.corrupt-*.json"))
        self.assertEqual(len(quarantined), 1)

        with self.assertLogs("app.cron_service", level="WARNING") as logged:
            await self._start_and_stop()

        self.assertTrue(any(str(quarantined[0]) in line for line in logged.output), logged.output)
        self.assertTrue(any("move or delete that file to silence this warning" in line for line in logged.output))

    async def test_a_deployment_without_apscheduler_warns_too(self) -> None:
        self.store_path.with_suffix(".corrupt-20261105T042259Z.json").write_text("{truncated", encoding="utf-8")

        with patch("app.cron_service._APSCHEDULER_AVAILABLE", False):
            with self.assertLogs("app.cron_service", level="WARNING"):
                await CronService(self.store_path).startup()

    async def test_glob_characters_in_the_store_name_match_literally(self) -> None:
        self.store_path = Path(self.tmp.name) / "c[ab]ons.json"
        mine = self.store_path.with_suffix(".corrupt-20261105T042259Z.json")
        other = Path(self.tmp.name) / "caons.corrupt-20261105T042259Z.json"
        mine.write_text("{truncated", encoding="utf-8")
        other.write_text("{truncated", encoding="utf-8")

        with self.assertLogs("app.cron_service", level="WARNING") as logged:
            await self._start_and_stop()

        self.assertTrue(any(str(mine) in line for line in logged.output), logged.output)
        self.assertFalse(any(str(other) in line for line in logged.output), logged.output)

    async def test_no_warning_once_the_store_holds_jobs(self) -> None:
        self.store_path.with_suffix(".corrupt-20261105T042259Z.json").write_text("{truncated", encoding="utf-8")
        await CronService(self.store_path).create_job(name="new", goal="g")

        with self.assertNoLogs("app.cron_service", level="WARNING"):
            await self._start_and_stop()

    async def test_no_warning_without_a_quarantine_file(self) -> None:
        with self.assertNoLogs("app.cron_service", level="WARNING"):
            await self._start_and_stop()


class CronTimezoneTests(unittest.IsolatedAsyncioTestCase):
    """Schedules read in CRON_TIMEZONE (UTC by default), whatever the host's own time zone is."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store_path = Path(self.tmp.name) / "crons.json"

    async def _started(self, **kwargs) -> CronService:
        service = CronService(self.store_path, **kwargs)
        await service.startup()
        self.addAsyncCleanup(service.shutdown)
        return service

    async def test_the_scheduler_defaults_to_utc_and_holds_the_three_job_defaults(self) -> None:
        service = await self._started()

        self.assertEqual(str(service._scheduler.timezone), "UTC")
        self.assertEqual(
            service._scheduler._job_defaults, {"misfire_grace_time": 300, "coalesce": True, "max_instances": 1}
        )

    async def test_the_configured_zone_reaches_the_scheduler_and_the_trigger_a_job_gets(self) -> None:
        await CronService(self.store_path).create_job(name="n", goal="g", schedule="0 9 * * *")

        service = await self._started(timezone="Asia/Tokyo")

        self.assertEqual(str(service._scheduler.timezone), "Asia/Tokyo")
        [job] = service._scheduler.get_jobs()
        self.assertEqual(str(job.trigger.timezone), "Asia/Tokyo")
        # Nine in the morning there: the zone changes the instant it fires.
        self.assertEqual(job.next_run_time.hour, 9)
        self.assertNotEqual(job.next_run_time.utcoffset(), timedelta(0))

    async def test_a_new_job_and_the_schedule_check_use_the_same_zone(self) -> None:
        service = await self._started(timezone="Asia/Tokyo")
        real_from_crontab = CronTrigger.from_crontab

        with patch("app.cron_service.CronTrigger.from_crontab", wraps=real_from_crontab) as from_crontab:
            created = await service.create_job(name="n", goal="g", schedule="30 6 * * *")

        self.assertGreaterEqual(from_crontab.call_count, 2, "both the check and the registration read the schedule")
        for call in from_crontab.call_args_list:
            self.assertEqual(call.kwargs.get("timezone"), "Asia/Tokyo", call)
        self.assertEqual(str(service._scheduler.get_job(created["id"]).trigger.timezone), "Asia/Tokyo")

    async def test_a_bad_zone_name_stops_startup_with_a_clear_message(self) -> None:
        for bad in ("Mars/Olympus", "not a zone", "../etc/passwd", "Europe"):
            with self.subTest(bad):
                with self.assertRaises(ValidationError) as caught:
                    Settings(CRON_TIMEZONE=bad)
                self.assertIn("CRON_TIMEZONE", str(caught.exception))
                self.assertIn("IANA", str(caught.exception))

    async def test_a_good_or_empty_zone_name_is_kept_or_defaulted_and_the_controller_is_wired_to_it(self) -> None:
        self.assertEqual(Settings().cron_timezone, "UTC")
        self.assertEqual(Settings(CRON_TIMEZONE="").cron_timezone, "UTC")
        self.assertEqual(Settings(CRON_TIMEZONE="  ").cron_timezone, "UTC")
        self.assertEqual(Settings(CRON_TIMEZONE=" America/Chicago ").cron_timezone, "America/Chicago")

        services = build_controller_services(Settings(CRON_TIMEZONE="Europe/Berlin"), version="test")

        self.assertEqual(services.cron_service._timezone, "Europe/Berlin")


class _OutcomeQueue:
    """Keeps a run's finish callback, and answers its agent job's record with the status a test sets."""

    def __init__(self) -> None:
        self.callback = None
        self.status = "completed"
        self.store = SimpleNamespace(get=AsyncMock(side_effect=self._record))

    async def _record(self, _job_id: str):
        return SimpleNamespace(status=self.status)

    async def enqueue_run(self, _session_id, _request, *, on_finish=None):
        self.callback = on_finish
        return {"id": "agent-job-1"}


class CronRunOutcomeTests(unittest.IsolatedAsyncioTestCase):
    """last_status says how the job's latest run ended, not that it was once queued."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager = MagicMock()
        self.manager.create_session = AsyncMock(return_value={"id": "session-1"})
        self.manager.close_session = AsyncMock()
        self.queue = _OutcomeQueue()
        self.service = CronService(Path(self.tmp.name) / "crons.json", job_queue=self.queue, manager=self.manager)
        self.job_id = (await self.service.create_job(name="n", goal="g", webhook_enabled=True))["id"]

    def _stored(self) -> dict:
        return self.service._load()[self.job_id]

    async def test_a_finished_run_replaces_queued_with_how_it_ended(self) -> None:
        for status in ("completed", "failed", "cancelled", "interrupted", "discarded"):
            with self.subTest(status):
                await self.service.trigger_job(self.job_id)
                self.assertEqual(self._stored()["last_status"], "queued")

                self.queue.status = status
                await self.queue.callback("agent-job-1")

                self.assertEqual(self._stored()["last_status"], status)
                self.manager.close_session.assert_awaited_with("session-1")
                self.assertEqual(self.service._active_runs, {})

    async def test_a_record_that_shows_no_end_reads_unknown_and_never_running(self) -> None:
        # The worker could not write the job's end (a full disk), so its record still says it runs: it has ended.
        await self.service.trigger_job(self.job_id)
        self.queue.status = "running"

        await self.queue.callback("agent-job-1")

        self.assertEqual(self._stored()["last_status"], "unknown")
        self.manager.close_session.assert_awaited_once_with("session-1")

    async def test_a_finish_callback_that_runs_before_the_queued_write_keeps_its_outcome(self) -> None:
        async def finished_before_enqueue_returns(_session_id, _request, *, on_finish=None):
            self.queue.status = "failed"
            await on_finish("agent-job-1")
            return {"id": "agent-job-1"}

        self.queue.enqueue_run = finished_before_enqueue_returns

        result = await self.service.trigger_job(self.job_id)

        self.assertTrue(result["triggered"])
        stored = self._stored()
        self.assertEqual(stored["last_status"], "failed", "the later 'queued' write ran over the outcome")
        self.assertEqual(stored["run_count"], 1)
        self.assertIsNotNone(stored["last_run_at"])
        self.manager.close_session.assert_awaited_once_with("session-1")
        self.assertEqual(self.service._active_runs, {})

    async def test_a_finish_callback_blocked_behind_the_queued_write_lands_after_it(self) -> None:
        # The callback waits for the store lock the trigger's update holds: its outcome is the one that stays.
        await self.service.trigger_job(self.job_id)
        await self.service._lock.acquire()
        finish = asyncio.create_task(self.queue.callback("agent-job-1"))
        await asyncio.sleep(0)
        self.service._active_runs[self.job_id] = "session-1"  # still this run's while the callback waits
        jobs = self.service._load()
        jobs[self.job_id]["last_status"] = "queued"
        self.service._save(jobs)
        self.service._lock.release()

        await finish

        self.assertEqual(self._stored()["last_status"], "completed")

    async def test_a_finish_callback_for_an_old_run_leaves_a_newer_runs_status_alone(self) -> None:
        await self.service.trigger_job(self.job_id)
        old_run = self.queue.callback
        # A newer trigger took the slot: its run, not this old one, says what the job's status is.
        self.service._active_runs[self.job_id] = "session-2"
        jobs = self.service._load()
        jobs[self.job_id]["last_status"] = "queued"
        self.service._save(jobs)
        self.queue.status = "failed"

        await old_run("agent-job-1")

        self.assertEqual(self._stored()["last_status"], "queued", "an old run wrote over a newer run's status")
        self.assertEqual(self.service._active_runs, {self.job_id: "session-2"}, "the newer run's slot was freed")
        self.manager.close_session.assert_not_awaited()

    async def test_a_cron_store_that_cannot_be_read_in_the_callback_still_releases_the_run(self) -> None:
        await self.service.trigger_job(self.job_id)

        with (
            patch.object(self.service, "_load", side_effect=CronStoreUnreadableError()),
            self.assertLogs("app.cron_service", level="ERROR") as logged,
        ):
            await self.queue.callback("agent-job-1")

        self.assertTrue(any(self.job_id in line for line in logged.output), logged.output)
        self.manager.close_session.assert_awaited_once_with("session-1")
        self.assertEqual(self.service._active_runs, {})

    async def test_a_cron_store_that_cannot_be_saved_in_the_callback_still_releases_the_run(self) -> None:
        await self.service.trigger_job(self.job_id)

        with (
            patch.object(self.service, "_save", side_effect=OSError("disk full")),
            self.assertLogs("app.cron_service", level="ERROR") as logged,
        ):
            await self.queue.callback("agent-job-1")

        self.assertTrue(any(self.job_id in line for line in logged.output), logged.output)
        self.manager.close_session.assert_awaited_once_with("session-1")
        self.assertEqual(self.service._active_runs, {})

    async def test_an_agent_job_that_cannot_be_read_reads_unknown_and_still_releases_the_run(self) -> None:
        for error in (OSError("disk error"), KeyError("agent-job-1"), ValueError("the record does not parse")):
            with self.subTest(type(error).__name__):
                self.manager.close_session.reset_mock()
                await self.service.trigger_job(self.job_id)
                self.queue.store.get = AsyncMock(side_effect=error)

                with self.assertLogs("app.cron_service", level="ERROR") as logged:
                    await self.queue.callback("agent-job-1")

                self.assertTrue(any("agent-job-1" in line for line in logged.output), logged.output)
                self.assertEqual(self._stored()["last_status"], "unknown")
                self.manager.close_session.assert_awaited_once_with("session-1")
                self.assertEqual(self.service._active_runs, {})

    async def test_a_run_whose_callback_never_comes_stays_queued(self) -> None:
        await self.service.trigger_job(self.job_id)

        self.assertEqual(self._stored()["last_status"], "queued")
        self.assertEqual(self.service._active_runs, {self.job_id: "session-1"})


ALICE = OperatorIdentity(id="alice", source="token")
OWNER_CREDENTIAL_GONE = "refused: owner credential removed"


class CronOwnerCredentialTests(unittest.IsolatedAsyncioTestCase):
    """A job fires as its owner only while a named credential still proves who that owner is."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager = MagicMock()
        self.manager.settings = SimpleNamespace(api_bearer_tokens="alice:token-a,bob:token-b", api_bearer_token=None)
        self.manager.create_session = AsyncMock(return_value={"id": "session-1"})
        self.manager.close_session = AsyncMock()
        self.manager.audit.append = AsyncMock()
        self.queue = MagicMock()
        self.queue.enqueue_run = AsyncMock(return_value={"id": "agent-job-1"})
        self.service = CronService(Path(self.tmp.name) / "crons.json", job_queue=self.queue, manager=self.manager)
        with acting_as(ALICE):
            self.job = await self.service.create_job(name="mine", goal="g", webhook_enabled=True)

    def _stored(self) -> dict:
        return self.service._load()[self.job["id"]]

    def _assert_refused(self, result: dict | None) -> None:
        if result is not None:
            self.assertEqual(
                result, {"triggered": False, "job_id": self.job["id"], "reason": "owner_credential_removed"}
            )
        self.manager.create_session.assert_not_awaited()
        self.queue.enqueue_run.assert_not_awaited()
        self.assertEqual(self.service._active_runs, {})
        self.assertEqual(self._stored()["last_status"], OWNER_CREDENTIAL_GONE)
        self.manager.audit.append.assert_awaited_once()
        audited = self.manager.audit.append.await_args.kwargs
        self.assertEqual(audited["event_type"], "cron_job_refused")
        self.assertEqual(audited["status"], "refused")
        self.assertEqual(
            audited["details"],
            {"cron_job": self.job["id"], "owner": "alice", "reason": "owner_credential_removed"},
        )
        self.assertNotIn(self.job["webhook_key"], str(audited))

    async def test_a_configured_owner_still_fires_as_itself(self) -> None:
        result = await self.service.trigger_via_webhook(self.job["id"], self.job["webhook_key"])

        self.assertTrue(result["triggered"])
        self.manager.create_session.assert_awaited_once()
        self.manager.audit.append.assert_not_awaited()

    async def test_a_removed_owner_is_refused_on_every_path(self) -> None:
        self.manager.settings.api_bearer_tokens = "bob:token-b"

        async def scheduled() -> None:
            await self.service._scheduled_run(self.job["id"])

        async def webhook() -> dict:
            return await self.service.trigger_via_webhook(self.job["id"], self.job["webhook_key"])

        async def trigger_tool() -> dict:
            with acting_as(ALICE):
                return await self.service.trigger_job(self.job["id"])

        for name, fire in (("scheduled", scheduled), ("webhook", webhook), ("trigger", trigger_tool)):
            with self.subTest(name):
                self.manager.audit.append.reset_mock()
                self._assert_refused(await fire())

    async def test_a_deployment_moved_to_the_shared_token_or_to_none_refuses_named_owners(self) -> None:
        for tokens, shared in (("", "the-shared-token"), ("", None), ("malformed-without-a-colon", None)):
            with self.subTest(tokens=tokens, shared=shared):
                self.manager.audit.append.reset_mock()
                self.manager.settings.api_bearer_tokens = tokens
                self.manager.settings.api_bearer_token = shared

                self._assert_refused(await self.service.trigger_via_webhook(self.job["id"], self.job["webhook_key"]))

    async def test_an_owner_only_a_later_entry_repeating_a_token_names_is_refused(self) -> None:
        # A request with a token listed twice proves the first operator listed with it: nothing proves alice.
        self.manager.settings.api_bearer_tokens = "bob:token-b,alice:token-b"
        self._assert_refused(await self.service.trigger_via_webhook(self.job["id"], self.job["webhook_key"]))

        self.manager.settings.api_bearer_tokens = "alice:token-b,bob:token-b"
        self.assertTrue((await self.service.trigger_via_webhook(self.job["id"], self.job["webhook_key"]))["triggered"])

    async def test_an_unowned_job_still_runs_as_anonymous_whatever_the_credentials(self) -> None:
        unowned = await self.service.create_job(name="shared", goal="g", webhook_enabled=True)
        self.assertIsNone(unowned["owner"])
        seen: list[tuple[str, str]] = []

        async def create_session(**_kwargs):
            seen.append(_who())
            return {"id": "session-1"}

        self.manager.create_session = create_session
        self.manager.settings.api_bearer_tokens = ""
        self.manager.settings.api_bearer_token = None

        result = await self.service.trigger_via_webhook(unowned["id"], unowned["webhook_key"])

        self.assertTrue(result["triggered"])
        self.assertEqual(seen, [("anonymous", "anonymous")])
        self.manager.audit.append.assert_not_awaited()

    async def test_a_restored_credential_fires_again_with_no_restart(self) -> None:
        self.manager.settings.api_bearer_tokens = "bob:token-b"
        self._assert_refused(await self.service.trigger_via_webhook(self.job["id"], self.job["webhook_key"]))

        self.manager.settings.api_bearer_tokens = "alice:another-token"
        result = await self.service.trigger_via_webhook(self.job["id"], self.job["webhook_key"])

        self.assertTrue(result["triggered"])

    async def test_a_refused_fire_is_not_counted_as_a_run(self) -> None:
        self.manager.settings.api_bearer_tokens = "bob:token-b"

        await self.service.trigger_via_webhook(self.job["id"], self.job["webhook_key"])

        stored = self._stored()
        self.assertEqual(stored["run_count"], 0)
        self.assertIsNone(stored["last_run_at"])

    async def test_a_refusal_is_answered_before_a_previous_run_is_considered(self) -> None:
        self.service._active_runs[self.job["id"]] = "session-0"
        self.manager.settings.api_bearer_tokens = "bob:token-b"

        result = await self.service.trigger_via_webhook(self.job["id"], self.job["webhook_key"])

        self.assertEqual(result["reason"], "owner_credential_removed")
        self.assertEqual(self.service._active_runs, {self.job["id"]: "session-0"}, "the refusal touched the slot")

    async def test_a_refusal_still_stands_when_its_status_or_its_audit_cannot_be_written(self) -> None:
        self.manager.settings.api_bearer_tokens = "bob:token-b"
        cases = (
            ("store unreadable", patch.object(self.service, "_load", side_effect=CronStoreUnreadableError())),
            ("store unsaveable", patch.object(self.service, "_save", side_effect=OSError("disk full"))),
            ("audit failing", patch.object(self.manager.audit, "append", new=AsyncMock(side_effect=OSError("full")))),
        )
        job = self.service._load()[self.job["id"]]
        for name, failing in cases:
            with self.subTest(name), failing, self.assertLogs("app.cron_service", level="ERROR") as logged:
                result = await self.service._run_job_now(job)

                self.assertEqual(result["reason"], "owner_credential_removed")
                self.assertTrue(any(self.job["id"] in line for line in logged.output), logged.output)
            self.manager.create_session.assert_not_awaited()
            self.queue.enqueue_run.assert_not_awaited()


class CronTriggerIdentityTests(unittest.IsolatedAsyncioTestCase):
    """A job fires as the operator who owns it, and an unowned job fires as nobody.

    acting_as(None) is a no-op, so a manual trigger of an unowned job used to run
    under the caller's identity and the session and agent job it created became
    the caller's.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.seen: dict[str, tuple[str, str]] = {}

        async def create_session(**_kwargs):
            self.seen["session"] = _who()
            return {"id": "session-1"}

        async def enqueue_run(_session_id, _request, **_kwargs):
            self.seen["agent_job"] = _who()
            return {"id": "agent-job-1"}

        manager = MagicMock()
        manager.settings = SimpleNamespace(api_bearer_tokens="alice:token-a", api_bearer_token=None)
        manager.create_session = create_session
        queue = MagicMock()
        queue.enqueue_run = enqueue_run
        self.service = CronService(Path(self.tmp.name) / "crons.json", job_queue=queue, manager=manager)

    async def test_unowned_job_triggered_by_a_named_operator_fires_as_nobody(self) -> None:
        job = await self.service.create_job(name="shared", goal="check", webhook_enabled=True)
        self.assertIsNone(job["owner"])

        with acting_as(OperatorIdentity(id="bob", source="token")):
            result = await self.service.trigger_job(job["id"])
        self.assertTrue(result["triggered"])
        self.assertEqual(self.seen, {"session": ("anonymous", "anonymous"), "agent_job": ("anonymous", "anonymous")})

        self.seen.clear()
        self.service._active_runs.clear()
        with acting_as(OperatorIdentity(id="bob", source="token")):
            await self.service.trigger_via_webhook(job["id"], job["webhook_key"])
        self.assertEqual(self.seen["session"], ("anonymous", "anonymous"))

    async def test_owned_job_still_fires_as_its_owner(self) -> None:
        alice = OperatorIdentity(id="alice", source="token")
        with acting_as(alice):
            job = await self.service.create_job(name="mine", goal="check", webhook_enabled=True)
        self.assertEqual(job["owner"], "alice")

        # Via the webhook, which needs no operator of its own: the owner is restored.
        await self.service.trigger_via_webhook(job["id"], job["webhook_key"])
        self.assertEqual(self.seen, {"session": ("alice", "token"), "agent_job": ("alice", "token")})

    async def test_unowned_job_with_no_operator_in_context_is_unchanged(self) -> None:
        job = await self.service.create_job(name="plain", goal="check")

        await self.service.trigger_job(job["id"])
        self.assertEqual(self.seen["session"], ("anonymous", "anonymous"))


def _who() -> tuple[str, str]:
    operator = get_current_operator()
    return operator.id, operator.source


if __name__ == "__main__":
    unittest.main()
