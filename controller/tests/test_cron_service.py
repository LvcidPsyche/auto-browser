from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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
        self.assertNotIn("webhook_key", created)
        self.assertEqual((await service.get_job(created["id"]))["id"], created["id"])
        self.assertEqual(len(await service.list_jobs()), 1)

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

        with self.assertRaises(PermissionError):
            await service.trigger_via_webhook(created["id"], "wrong")
        with self.assertRaises(KeyError):
            await service.trigger_job("missing")

        result = await service.trigger_via_webhook(created["id"], raw["webhook_key"])

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

        self.store_path.write_text("{bad json", encoding="utf-8")
        self.assertEqual(service._load(), {})

        uninitialized = CronService(self.store_path)
        with self.assertRaises(RuntimeError):
            await uninitialized._run_job_now({"id": "job-1", "goal": "run"})


if __name__ == "__main__":
    unittest.main()
