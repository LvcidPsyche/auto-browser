"""A cron job's webhook key: hashed at rest, shown once, and replaced or removed through rotate and revoke."""

from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.browser_manager import BrowserManager
from app.cron_service import CronService
from app.models import McpToolCallRequest, OperatorIdentity
from app.routes.operations import create_operations_router
from app.session_ownership import acting_as, as_system
from app.tool_gateway.gateway import McpToolGateway
from tests.test_browser_manager_create_session import _settings

ALICE = OperatorIdentity(id="alice", source="token")
BOB = OperatorIdentity(id="bob", source="token")
LEGACY_KEY = "legacy-plaintext-key-0123456789abcdef"


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _legacy_store(path: Path, *, owner: str | None = None, key: str | None = LEGACY_KEY) -> None:
    """A store as an earlier version wrote it: the key itself under webhook_key."""
    job = {
        "id": "job-1",
        "name": "old",
        "provider": "openai",
        "schedule": None,
        "enabled": True,
        "webhook_key": key,
        "goal": "g",
        "start_url": None,
        "auth_profile": None,
        "proxy_persona": None,
        "max_steps": 20,
        "workflow_profile": "fast",
        "owner": owner,
        "created_at": "2026-11-01T00:00:00Z",
        "last_run_at": None,
        "last_status": None,
        "run_count": 0,
    }
    path.write_text(json.dumps({"job-1": job}), encoding="utf-8")


def _service(path: Path, *, audit: bool = True) -> tuple[CronService, MagicMock]:
    manager = MagicMock()
    manager.settings = SimpleNamespace(api_bearer_tokens="alice:token-a,bob:token-b", api_bearer_token=None)
    manager.create_session = AsyncMock(return_value={"id": "session-1"})
    manager.close_session = AsyncMock()
    manager.audit.append = AsyncMock() if audit else AsyncMock(side_effect=OSError("disk full"))
    queue = MagicMock()
    queue.enqueue_run = AsyncMock(return_value={"id": "agent-job-1"})
    return CronService(path, job_queue=queue, manager=manager), manager


class CronWebhookKeyAtRestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "crons.json"

    async def test_the_store_holds_only_a_hash_and_the_key_still_triggers(self) -> None:
        service, _ = _service(self.path)

        job = await service.create_job(name="n", goal="g", webhook_enabled=True)

        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn(job["webhook_key"], text)
        self.assertEqual(json.loads(text)[job["id"]]["webhook_key_hash"], _digest(job["webhook_key"]))
        self.assertTrue((await service.trigger_via_webhook(job["id"], job["webhook_key"]))["triggered"])

    async def test_a_job_without_a_webhook_has_no_hash_and_answers_no_id(self) -> None:
        service, _ = _service(self.path)

        job = await service.create_job(name="n", goal="g", schedule="0 9 * * *")

        self.assertFalse(job["webhook_enabled"])
        self.assertNotIn("webhook_key_id", job)
        self.assertIsNone(service._load()[job["id"]]["webhook_key_hash"])

    async def test_a_legacy_key_is_hashed_once_at_startup_and_keeps_working(self) -> None:
        _legacy_store(self.path)
        service, _ = _service(self.path)

        await service.startup()
        self.addAsyncCleanup(service.shutdown)

        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn(LEGACY_KEY, text)
        stored = json.loads(text)["job-1"]
        self.assertNotIn("webhook_key", stored)
        self.assertEqual(stored["webhook_key_hash"], _digest(LEGACY_KEY))
        self.assertTrue((await service.trigger_via_webhook("job-1", LEGACY_KEY))["triggered"])

    async def test_startup_writes_nothing_when_there_is_nothing_to_hash(self) -> None:
        _legacy_store(self.path, key=None)
        before = self.path.read_bytes()
        service, _ = _service(self.path)

        await service.startup()
        self.addAsyncCleanup(service.shutdown)
        await service.shutdown()
        await service.startup()  # a second start finds nothing either

        self.assertEqual(self.path.read_bytes(), before)

    async def test_a_deployment_without_apscheduler_hashes_too(self) -> None:
        _legacy_store(self.path)
        service, _ = _service(self.path)

        with patch("app.cron_service._APSCHEDULER_AVAILABLE", False):
            await service.startup()

        self.assertNotIn(LEGACY_KEY, self.path.read_text(encoding="utf-8"))
        self.assertEqual(service._load()["job-1"]["webhook_key_hash"], _digest(LEGACY_KEY))

    async def test_a_read_path_never_writes_a_legacy_store(self) -> None:
        _legacy_store(self.path)
        before = self.path.read_bytes()
        service, _ = _service(self.path)

        await service.list_jobs()
        await service.get_job("job-1")

        self.assertEqual(self.path.read_bytes(), before)
        # A trigger saves the run's metadata; it does not migrate the key.
        await service.trigger_via_webhook("job-1", LEGACY_KEY)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["job-1"]["webhook_key"], LEGACY_KEY)

    async def test_a_store_that_cannot_be_rehashed_keeps_working_and_says_so(self) -> None:
        _legacy_store(self.path)
        service, _ = _service(self.path)

        with (
            patch.object(service, "_save", side_effect=OSError("disk full")),
            self.assertLogs("app.cron_service", level="ERROR") as logged,
        ):
            await service.startup()
        self.addAsyncCleanup(service.shutdown)

        # The key is still accepted, by hashing the stored plaintext, and the failure names neither key nor hash.
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["job-1"]["webhook_key"], LEGACY_KEY)
        self.assertTrue((await service.trigger_via_webhook("job-1", LEGACY_KEY))["triggered"])
        with self.assertRaises(PermissionError):
            await service.trigger_via_webhook("job-1", "not-the-key")
        for line in logged.output:
            self.assertNotIn(LEGACY_KEY, line)
            self.assertNotIn(_digest(LEGACY_KEY), line)

    async def test_the_id_is_the_same_before_and_after_the_hash_and_is_not_the_hash(self) -> None:
        _legacy_store(self.path)
        service, _ = _service(self.path)
        before = (await service.get_job("job-1"))["webhook_key_id"]

        await service.startup()
        self.addAsyncCleanup(service.shutdown)
        after = (await service.get_job("job-1"))["webhook_key_id"]

        self.assertEqual(before, after)
        self.assertNotIn(after, _digest(LEGACY_KEY))
        self.assertNotIn(after, LEGACY_KEY)

    async def test_every_read_strips_the_key_and_the_hash(self) -> None:
        service, _ = _service(self.path)
        job = await service.create_job(name="n", goal="g", webhook_enabled=True)
        stored_hash = service._load()[job["id"]]["webhook_key_hash"]
        updated = await service.update_job(job["id"], {"name": "renamed"})
        listed = await service.list_jobs()
        fetched = await service.get_job(job["id"])

        for surface in (updated, listed[0], fetched):
            text = json.dumps(surface)
            self.assertNotIn("webhook_key_hash", surface)
            self.assertNotIn("webhook_key", surface)
            self.assertNotIn(stored_hash, text)
            self.assertNotIn(job["webhook_key"], text)
        self.assertNotIn(stored_hash, json.dumps(job))


class CronWebhookKeyRotateRevokeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "crons.json"

    async def test_rotate_returns_a_new_key_once_and_the_old_one_stops_working(self) -> None:
        service, manager = _service(self.path)
        job = await service.create_job(name="n", goal="g", webhook_enabled=True)

        rotated = await service.rotate_webhook_key(job["id"])

        self.assertEqual(len(rotated["webhook_key"]), 64)
        self.assertNotEqual(rotated["webhook_key"], job["webhook_key"])
        self.assertTrue(rotated["webhook_enabled"])
        self.assertNotEqual(rotated["webhook_key_id"], job["webhook_key_id"])
        self.assertNotIn("webhook_key_hash", rotated)
        with self.assertRaises(PermissionError):
            await service.trigger_via_webhook(job["id"], job["webhook_key"])
        self.assertTrue((await service.trigger_via_webhook(job["id"], rotated["webhook_key"]))["triggered"])
        self.assertNotIn("webhook_key", await service.get_job(job["id"]))
        self.assertNotIn(rotated["webhook_key"], self.path.read_text(encoding="utf-8"))
        manager.audit.append.assert_awaited_once()
        audited = manager.audit.append.await_args.kwargs
        self.assertEqual((audited["event_type"], audited["status"]), ("cron_webhook_key_rotated", "ok"))
        self.assertNotIn(rotated["webhook_key"], str(audited))
        self.assertNotIn(_digest(rotated["webhook_key"]), str(audited))

    async def test_rotating_a_job_with_no_webhook_enables_one(self) -> None:
        service, _ = _service(self.path)
        job = await service.create_job(name="n", goal="g", schedule="0 9 * * *")
        self.assertFalse(job["webhook_enabled"])

        rotated = await service.rotate_webhook_key(job["id"])

        self.assertTrue((await service.get_job(job["id"]))["webhook_enabled"])
        self.assertTrue((await service.trigger_via_webhook(job["id"], rotated["webhook_key"]))["triggered"])

    async def test_rotating_a_legacy_job_removes_the_plaintext_it_held(self) -> None:
        _legacy_store(self.path)
        service, _ = _service(self.path)

        rotated = await service.rotate_webhook_key("job-1")

        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn(LEGACY_KEY, text)
        self.assertNotIn(rotated["webhook_key"], text)
        with self.assertRaises(PermissionError):
            await service.trigger_via_webhook("job-1", LEGACY_KEY)
        self.assertTrue((await service.trigger_via_webhook("job-1", rotated["webhook_key"]))["triggered"])

    async def test_revoke_removes_the_key_and_the_trigger_is_refused_as_for_any_job_with_no_key(self) -> None:
        service, manager = _service(self.path)
        job = await service.create_job(name="n", goal="g", webhook_enabled=True)

        answer = await service.revoke_webhook_key(job["id"])

        self.assertEqual(answer, {"job_id": job["id"], "revoked": True})
        self.assertFalse((await service.get_job(job["id"]))["webhook_enabled"])
        self.assertNotIn("webhook_key_hash", service._load()[job["id"]])
        with self.assertRaises(PermissionError):
            await service.trigger_via_webhook(job["id"], job["webhook_key"])
        manager.audit.append.assert_awaited_once()
        audited = manager.audit.append.await_args.kwargs
        self.assertEqual((audited["event_type"], audited["status"]), ("cron_webhook_key_revoked", "ok"))
        self.assertNotIn(job["webhook_key"], str(audited))

    async def test_revoking_a_job_with_no_key_changes_nothing_and_audits_nothing(self) -> None:
        service, manager = _service(self.path)
        job = await service.create_job(name="n", goal="g", schedule="0 9 * * *")

        answer = await service.revoke_webhook_key(job["id"])

        self.assertEqual(answer, {"job_id": job["id"], "revoked": False})
        manager.audit.append.assert_not_awaited()

    async def test_revoking_a_legacy_job_removes_its_plaintext(self) -> None:
        _legacy_store(self.path)
        service, _ = _service(self.path)

        self.assertEqual(await service.revoke_webhook_key("job-1"), {"job_id": "job-1", "revoked": True})

        stored = service._load()["job-1"]
        self.assertNotIn("webhook_key", stored)
        self.assertNotIn("webhook_key_hash", stored)
        with self.assertRaises(PermissionError):
            await service.trigger_via_webhook("job-1", LEGACY_KEY)

    async def test_another_operators_job_reads_as_not_found(self) -> None:
        service, manager = _service(self.path)
        with acting_as(ALICE):
            job = await service.create_job(name="n", goal="g", webhook_enabled=True)

        with acting_as(BOB):
            with self.assertRaises(KeyError):
                await service.rotate_webhook_key(job["id"])
            with self.assertRaises(KeyError):
                await service.revoke_webhook_key(job["id"])

        manager.audit.append.assert_not_awaited()
        with as_system():
            self.assertTrue((await service.get_job(job["id"]))["webhook_enabled"])
        with self.assertRaises(KeyError):
            await service.rotate_webhook_key("missing")
        with self.assertRaises(KeyError):
            await service.revoke_webhook_key("missing")

    async def test_a_failing_audit_does_not_lose_the_new_key_or_undo_the_revoke(self) -> None:
        service, _ = _service(self.path, audit=False)
        job = await service.create_job(name="n", goal="g", webhook_enabled=True)

        with self.assertLogs("app.cron_service", level="ERROR") as logged:
            rotated = await service.rotate_webhook_key(job["id"])
            revoked = await service.revoke_webhook_key(job["id"])

        self.assertEqual(len(rotated["webhook_key"]), 64)
        self.assertEqual(revoked["revoked"], True)
        self.assertNotIn(rotated["webhook_key"], "\n".join(logged.output))

    async def test_a_failed_save_leaves_the_old_key_working_and_is_not_audited(self) -> None:
        service, manager = _service(self.path)
        job = await service.create_job(name="n", goal="g", webhook_enabled=True)

        for call in (service.rotate_webhook_key, service.revoke_webhook_key):
            with self.subTest(call.__name__), patch.object(service, "_save", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    await call(job["id"])

        manager.audit.append.assert_not_awaited()
        self.assertTrue((await service.trigger_via_webhook(job["id"], job["webhook_key"]))["triggered"])


class CronWebhookKeyRestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service, _ = _service(Path(self.tmp.name) / "crons.json")
        app = FastAPI()
        app.include_router(
            create_operations_router(manager=MagicMock(), proxy_store=MagicMock(), cron_service=self.service)
        )
        self.client = TestClient(app, raise_server_exceptions=False)
        self.job = self.client.post("/crons", json={"name": "n", "goal": "g", "webhook_enabled": True}).json()

    def test_rotate_and_revoke_answer_the_key_once_and_then_only_refusals(self) -> None:
        rotated = self.client.post(f"/crons/{self.job['id']}/webhook-key/rotate")
        self.assertEqual(rotated.status_code, 200, rotated.text)
        new_key = rotated.json()["webhook_key"]
        self.assertNotEqual(new_key, self.job["webhook_key"])
        self.assertNotIn("webhook_key_hash", rotated.json())

        fetched = self.client.get(f"/crons/{self.job['id']}").json()
        self.assertNotIn(new_key, json.dumps(fetched))

        revoked = self.client.delete(f"/crons/{self.job['id']}/webhook-key")
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertEqual(revoked.json(), {"job_id": self.job["id"], "revoked": True})
        self.assertEqual(
            self.client.post(f"/crons/{self.job['id']}/trigger", json={"webhook_key": new_key}).status_code, 403
        )

    def test_an_unknown_job_answers_404_with_the_same_body(self) -> None:
        for response in (
            self.client.post("/crons/nothing-here/webhook-key/rotate"),
            self.client.delete("/crons/nothing-here/webhook-key"),
        ):
            self.assertEqual(response.status_code, 404, response.text)
            self.assertEqual(response.json(), {"detail": "Not found"})

    def test_an_overlong_job_id_is_refused_as_the_trigger_route_refuses_one(self) -> None:
        for response in (
            self.client.post(f"/crons/{'x' * 51}/webhook-key/rotate"),
            self.client.delete(f"/crons/{'x' * 51}/webhook-key"),
        ):
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(response.json(), {"detail": "Invalid request"})


class CronWebhookTriggerRefusalTests(unittest.IsolatedAsyncioTestCase):
    """An unknown id, a job with no key and a wrong key are the same refusal: nothing says which it was."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service, self.manager = _service(Path(self.tmp.name) / "crons.json")

    async def _cases(self) -> dict[str, tuple[str, str]]:
        keyed = await self.service.create_job(name="keyed", goal="g", webhook_enabled=True)
        unkeyed = await self.service.create_job(name="scheduled", goal="g", schedule="0 9 * * *")
        return {
            "unknown id": ("no-such-job", "any-key"),
            "job with no key": (unkeyed["id"], "any-key"),
            "wrong key": (keyed["id"], "wrong-key"),
            "non-ascii wrong key": (keyed["id"], "clé-invalide"),
        }

    async def test_each_refusal_is_the_same_error_after_the_same_work(self) -> None:
        cases = await self._cases()
        errors: dict[str, str] = {}
        real_compare = hmac.compare_digest
        for name, (job_id, key) in cases.items():
            with self.subTest(name), patch("app.cron_service.hmac.compare_digest", wraps=real_compare) as compare:
                with self.assertRaises(PermissionError) as caught:
                    await self.service.trigger_via_webhook(job_id, key)
                self.assertEqual(
                    compare.call_count, 1, "a refusal that skips the comparison is told apart by its speed"
                )
                self.assertEqual(type(caught.exception), PermissionError)
                errors[name] = str(caught.exception)
        self.assertEqual(len(set(errors.values())), 1, errors)
        self.assertNotIn("not enabled", next(iter(errors.values())))
        self.manager.create_session.assert_not_awaited()
        self.manager.audit.append.assert_not_awaited()

    async def test_the_route_answers_each_refusal_with_the_same_status_and_body(self) -> None:
        cases = await self._cases()
        app = FastAPI()
        app.include_router(
            create_operations_router(manager=self.manager, proxy_store=MagicMock(), cron_service=self.service)
        )
        client = TestClient(app, raise_server_exceptions=False)

        answers = {
            name: client.post(f"/crons/{job_id}/trigger", json={"webhook_key": key})
            for name, (job_id, key) in cases.items()
        }

        for name, response in answers.items():
            with self.subTest(name):
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.json(), {"detail": "Not permitted"})

    async def test_a_disabled_job_answers_its_error_only_after_the_key_is_proved(self) -> None:
        job = await self.service.create_job(name="n", goal="g", schedule="0 9 * * *", webhook_enabled=True)
        await self.service.update_job(job["id"], {"enabled": False})
        app = FastAPI()
        app.include_router(
            create_operations_router(manager=self.manager, proxy_store=MagicMock(), cron_service=self.service)
        )
        client = TestClient(app, raise_server_exceptions=False)

        wrong = client.post(f"/crons/{job['id']}/trigger", json={"webhook_key": "wrong-key"})
        right = client.post(f"/crons/{job['id']}/trigger", json={"webhook_key": job["webhook_key"]})

        self.assertEqual(wrong.status_code, 403, wrong.text)
        self.assertEqual(wrong.json(), {"detail": "Not permitted"})
        self.assertEqual(right.status_code, 409, right.text)
        self.assertIn("job is disabled", right.json()["detail"])

    async def test_a_key_that_hashes_to_the_dummy_digest_is_still_refused(self) -> None:
        # What an empty comparison is made against must not be something a caller can present.
        unkeyed = await self.service.create_job(name="scheduled", goal="g", schedule="0 9 * * *")

        with patch("app.cron_service._NO_WEBHOOK_KEY_DIGEST", _digest("guessable")):
            with self.assertRaises(PermissionError):
                await self.service.trigger_via_webhook(unkeyed["id"], "guessable")
            with self.assertRaises(PermissionError):
                await self.service.trigger_via_webhook("no-such-job", "guessable")

        self.manager.create_session.assert_not_awaited()

    async def test_a_right_key_still_runs_a_job(self) -> None:
        job = await self.service.create_job(name="n", goal="g", webhook_enabled=True)

        result = await self.service.trigger_via_webhook(job["id"], job["webhook_key"])

        self.assertTrue(result["triggered"])


class CronWebhookKeyMcpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.settings = _settings(root)
        self.settings.witness_enabled = True
        self.manager = BrowserManager(self.settings)
        await self.manager.audit.startup()
        self.queue = MagicMock()
        self.queue.enqueue_run = AsyncMock(return_value={"id": "agent-job-1"})
        self.cron = CronService(root / "crons.json", job_queue=self.queue, manager=self.manager)
        self.gateway = McpToolGateway(
            manager=self.manager,
            orchestrator=SimpleNamespace(),
            job_queue=self.queue,
            tool_profile="full",
            tool_packs="cron",
            cron_service=self.cron,
        )
        self.job = await self.cron.create_job(name="n", goal="g", webhook_enabled=True)

    async def _call(self, name: str, arguments: dict):
        return await self.gateway.call_tool(McpToolCallRequest(name=name, arguments=arguments))

    def _records_on_disk(self) -> str:
        """Everything the audit log and the witness store hold, as text."""
        root = Path(self.tmp.name)
        return "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for directory in (root / "audit", root / "witness")
            for path in directory.rglob("*")
            if path.is_file()
        )

    async def test_the_tools_are_neither_read_only_nor_idempotent(self) -> None:
        tools = {tool["name"]: tool for tool in self.gateway.list_tools()}
        rotate = tools["browser.rotate_cron_webhook_key"]["annotations"]
        revoke = tools["browser.revoke_cron_webhook_key"]["annotations"]

        self.assertEqual(
            rotate, {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
        )
        self.assertEqual(
            revoke, {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}
        )

    async def test_the_tools_validate_job_id_as_the_routes_do(self) -> None:
        for name in ("browser.rotate_cron_webhook_key", "browser.revoke_cron_webhook_key"):
            with self.subTest(name):
                for arguments in ({"job_id": "x" * 51}, {"job_id": ""}, {}):
                    response = await self._call(name, arguments)

                    self.assertTrue(response.isError, name)
                    self.assertIn(f"Invalid arguments for {name}", response.content[0].text)

    async def test_rotate_answers_the_key_in_the_result_and_no_record_holds_it(self) -> None:
        response = await self._call("browser.rotate_cron_webhook_key", {"job_id": self.job["id"]})

        self.assertFalse(response.isError, response.content[0].text)
        new_key = response.structuredContent["webhook_key"]
        self.assertEqual(len(new_key), 64)
        self.assertNotIn("webhook_key_hash", response.structuredContent)
        # The call left an audit event, and neither it nor any witness record holds the key or its hash.
        events = await self.manager.audit.list()
        self.assertTrue(any(event.event_type == "cron_webhook_key_rotated" for event in events))
        records = self._records_on_disk()
        self.assertTrue(records, "no audit or witness record was written: this test is reading the wrong place")
        self.assertNotIn(new_key, records)
        self.assertNotIn(_digest(new_key), records)

    async def test_revoke_leaves_no_key_or_hash_in_any_record(self) -> None:
        stored_hash = self.cron._load()[self.job["id"]]["webhook_key_hash"]

        response = await self._call("browser.revoke_cron_webhook_key", {"job_id": self.job["id"]})

        self.assertFalse(response.isError, response.content[0].text)
        self.assertEqual(response.structuredContent, {"job_id": self.job["id"], "revoked": True})
        records = self._records_on_disk()
        self.assertNotIn(self.job["webhook_key"], records)
        self.assertNotIn(stored_hash, records)

    async def test_an_unknown_job_reads_as_not_found_over_mcp(self) -> None:
        response = await self._call("browser.rotate_cron_webhook_key", {"job_id": "missing"})

        self.assertTrue(response.isError)
        self.assertIn("Cron job not found: missing", response.content[0].text)

    async def test_a_governed_call_waits_for_approval_and_keeps_the_key_out_of_the_approval(self) -> None:
        arguments = {"job_id": self.job["id"], "workflow_profile": "governed"}

        first = await self._call("browser.rotate_cron_webhook_key", arguments)

        self.assertEqual(first.structuredContent.get("status"), "approval_required")
        approval_id = first.structuredContent["approval_id"]
        await self.manager.approve(approval_id, comment="ok")
        second = await self._call("browser.rotate_cron_webhook_key", {**arguments, "approval_id": approval_id})
        self.assertFalse(second.isError, second.content[0].text)
        new_key = second.structuredContent["webhook_key"]
        self.assertNotIn(new_key, json.dumps(await self.manager.list_approvals()))
        self.assertNotIn(new_key, self._records_on_disk())
