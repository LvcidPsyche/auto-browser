from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.cron_service import CronService


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
        self.assertIn("webhook_key_preview", created)
        # Disclosed once, on creation — otherwise no caller could ever trigger it.
        self.assertEqual(len(created["webhook_key"]), 64)
        self.assertTrue(created["webhook_key"].startswith(created["webhook_key_preview"].removesuffix("...")))
        fetched = await service.get_job(created["id"])
        self.assertEqual(fetched["id"], created["id"])
        self.assertNotIn("webhook_key", fetched)
        listed = await service.list_jobs()
        self.assertEqual(len(listed), 1)
        self.assertNotIn("webhook_key", listed[0])

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

        self.assertEqual(created["webhook_key"], raw["webhook_key"])

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


if __name__ == "__main__":
    unittest.main()
