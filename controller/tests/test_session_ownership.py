"""Sessions belong to the token-verified operator who created them.

Under named credentials (API_BEARER_TOKENS), operator A could drive operator
B's logged-in session, read its pages and save its cookies to a profile of
their own, which got around the auth-profile ownership 1.8.1 added. A session
is now owned by the token-verified operator who created it, the rule profiles
follow. Unowned sessions (the shared token, or no token) stay open to all, and
another operator's session answers like one that does not exist: left out of
listings, refused by id, over REST, MCP and /artifacts, with its approvals,
agent jobs and cron jobs. Background work runs as whoever it works for.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.action_errors import SessionNotFoundError
from app.agent_jobs import AgentJobQueue
from app.audit import get_current_operator
from app.browser_manager import BrowserManager, BrowserSession
from app.cron_service import CronService
from app.models import AgentRunRequest, BrowserActionDecision, McpToolCallRequest, OperatorIdentity
from app.session_ownership import acting_as, as_system
from app.tool_gateway.gateway import McpToolGateway
from app.utils import UTC
from tests.test_browser_manager_create_session import FakeBrowser, FakeContext, FakePage, _settings

ALICE = OperatorIdentity(id="alice", source="token")
BOB = OperatorIdentity(id="bob", source="token")
# What X-Operator-Id asserts: a label, not a proof.
ALICE_BY_HEADER = OperatorIdentity(id="alice", source="header")


class SessionOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = BrowserManager(_settings(Path(self.tmp.name)))
        self.manager._settle = AsyncMock()  # type: ignore[method-assign]
        self.manager._maybe_provision_session_tunnel = AsyncMock()  # type: ignore[method-assign]
        # The shared browser node: one browser, a new context per session.
        browser = FakeBrowser(FakeContext(FakePage()))
        browser.new_context = AsyncMock(side_effect=lambda **_kwargs: FakeContext(FakePage()))
        self.manager.browser = browser  # type: ignore[assignment]
        self.manager._acquire_session_browser = AsyncMock(return_value=(browser, None))  # type: ignore[method-assign]
        self.gateway = McpToolGateway(manager=self.manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace())

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def _create(self, operator: OperatorIdentity | None) -> str:
        with acting_as(operator):
            return (await self.manager.create_session(name="fixture"))["id"]

    async def _tool(self, operator: OperatorIdentity, name: str, arguments: dict):
        with acting_as(operator):
            return await self.gateway.call_tool(McpToolCallRequest(name=name, arguments=arguments))

    async def test_only_a_token_verified_creator_owns_a_session(self) -> None:
        cases = ((ALICE, "alice"), (ALICE_BY_HEADER, None), (None, None))
        for operator, owner in cases:
            with self.subTest(operator=operator):
                session_id = await self._create(operator)
                self.assertEqual(self.manager.sessions[session_id].owner, owner)
                with as_system():
                    self.assertEqual((await self.manager.get_session_record(session_id))["owner"], owner)
                    await self.manager.close_session(session_id)  # MAX_SESSIONS is 2 here

    async def test_another_operator_finds_no_such_session(self) -> None:
        session_id = await self._create(ALICE)

        for intruder in (BOB, ALICE_BY_HEADER, None):
            with self.subTest(intruder=intruder), acting_as(intruder):
                with self.assertRaises(SessionNotFoundError) as ctx:
                    await self.manager.get_session(session_id)
                self.assertEqual(ctx.exception.code, "unknown_session")
                with self.assertRaises(SessionNotFoundError):
                    await self.manager.get_session_record(session_id)
                self.assertNotIn(session_id, [item["id"] for item in await self.manager.list_sessions()])

        with acting_as(ALICE):
            self.assertIs(await self.manager.get_session(session_id), self.manager.sessions[session_id])
            self.assertIn(session_id, [item["id"] for item in await self.manager.list_sessions()])
        with as_system():
            await self.manager.get_session(session_id)

    async def test_unowned_sessions_stay_open_to_everyone(self) -> None:
        session_id = await self._create(None)

        with acting_as(BOB):
            await self.manager.get_session(session_id)
            self.assertIn(session_id, [item["id"] for item in await self.manager.list_sessions()])

    async def test_a_closed_session_is_not_revealed_as_closed_to_another_operator(self) -> None:
        session_id = await self._create(ALICE)
        with acting_as(ALICE):
            await self.manager.close_session(session_id)

        with acting_as(BOB):
            self.assertNotIn(session_id, [item["id"] for item in await self.manager.list_sessions()])
            with self.assertRaises(SessionNotFoundError) as ctx:
                await self.manager.get_session(session_id)
            self.assertEqual(ctx.exception.code, "unknown_session")
        with acting_as(ALICE):
            with self.assertRaises(SessionNotFoundError) as ctx:
                await self.manager.get_session(session_id)
            self.assertEqual(ctx.exception.code, "session_closed")

    async def test_mcp_tools_refuse_another_operators_session(self) -> None:
        session_id = await self._create(ALICE)

        # list_downloads reads the session's record without resolving the
        # session, so only the gateway's own check stands in its way.
        for tool in ("browser.get_session", "browser.list_downloads"):
            with self.subTest(tool=tool):
                refused = await self._tool(BOB, tool, {"session_id": session_id})
                self.assertTrue(refused.isError)
                self.assertEqual(refused.structuredContent["code"], "unknown_session")
                allowed = await self._tool(ALICE, tool, {"session_id": session_id})
                self.assertFalse(allowed.isError, allowed.content[0].text)

    async def test_an_omitted_session_id_never_resolves_to_another_operators_session(self) -> None:
        await self._create(ALICE)

        response = await self._tool(BOB, "browser.list_tabs", {})

        self.assertTrue(response.isError)
        self.assertEqual(response.structuredContent["code"], "no_session")

    async def test_approvals_of_another_operators_session_are_hidden(self) -> None:
        session_id = await self._create(ALICE)
        decision = BrowserActionDecision(action="click", reason="post", element_id="op-post", risk_category="post")
        approval = await self.manager.approvals.create_or_reuse_pending(
            session_id=session_id, kind="post", reason="post it", action=decision
        )

        with acting_as(BOB):
            self.assertEqual(await self.manager.list_approvals(), [])
            for call in (self.manager.get_approval, self.manager.approve, self.manager.reject):
                with self.assertRaises(KeyError):
                    await call(approval.id)
            with self.assertRaises(KeyError):
                await self.manager.execute_approval(approval.id)
        self.assertEqual((await self.manager.approvals.get(approval.id)).status, "pending")

        with acting_as(ALICE):
            self.assertEqual([item["id"] for item in await self.manager.list_approvals()], [approval.id])
            self.assertEqual((await self.manager.approve(approval.id))["status"], "approved")

    async def test_agent_jobs_run_as_their_operator_and_stay_theirs(self) -> None:
        session_id = await self._create(ALICE)
        ran_as: list[str] = []

        class RecordingOrchestrator:
            async def run(self_inner, **_kwargs):
                ran_as.append(get_current_operator().id)
                await self.manager.get_session(session_id)  # refused unless run as alice
                return SimpleNamespace(model_dump=lambda: {"status": "done"})

        queue = AgentJobQueue(
            orchestrator=RecordingOrchestrator(),
            store_root=Path(self.tmp.name) / "jobs",
            session_owner=self.manager.session_owner,
        )
        await queue.startup()
        try:
            with acting_as(ALICE):
                job = await queue.enqueue_run(session_id, AgentRunRequest(provider="openai", goal="look", max_steps=1))
            for _ in range(100):
                record = await queue.store.get(job["id"])
                if record.status in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.02)
        finally:
            await queue.shutdown()

        self.assertEqual((record.status, ran_as), ("completed", ["alice"]), record.error)
        with acting_as(BOB):
            self.assertEqual(await queue.list_jobs(), [])
            for call in (queue.get_job, queue.cancel_job, queue.discard_job, queue.resume_job):
                with self.assertRaises(KeyError):
                    await call(job["id"])
        with acting_as(ALICE):
            self.assertEqual([item["id"] for item in await queue.list_jobs()], [job["id"]])

    async def test_cron_jobs_fire_as_their_owner_and_open_only_their_profiles(self) -> None:
        profiles = Path(self.manager.settings.auth_root) / "profiles"
        for name, owner in (("alice-login", "alice"), ("carol-login", "carol")):
            (profiles / name).mkdir(parents=True)
            (profiles / name / "profile.json").write_text(json.dumps({"owner": owner}), encoding="utf-8")
        queued_as: list[str] = []

        async def enqueue_run(session_id, _request):
            queued_as.append(get_current_operator().id)
            return {"id": f"job-{session_id}"}

        job_queue = SimpleNamespace(enqueue_run=enqueue_run, on_finish=lambda *_args: None)
        cron = CronService(store_path=Path(self.tmp.name) / "crons.json", job_queue=job_queue, manager=self.manager)
        created_session = {}

        async def create_session(**kwargs):
            created_session["owner"] = get_current_operator().id
            self.manager.auth_profiles.require_access(kwargs["auth_profile"], action="open")
            return {"id": "cron-session"}

        self.manager.create_session = create_session  # type: ignore[method-assign]

        with acting_as(ALICE):
            with self.assertRaises(PermissionError):
                await cron.create_job(name="x", goal="g", auth_profile="carol-login")
            job = await cron.create_job(name="daily", goal="check", auth_profile="alice-login")
            with self.assertRaises(PermissionError):
                await cron.update_job(job["id"], {"auth_profile": "carol-login"})
        self.assertEqual(job["owner"], "alice")

        with acting_as(BOB):
            self.assertEqual(await cron.list_jobs(), [])
            for call in (cron.get_job, cron.trigger_job):
                with self.assertRaises(KeyError):
                    await call(job["id"])
            self.assertFalse(await cron.delete_job(job["id"]))

        # Fired by the scheduler, which has no operator of its own.
        await cron._scheduled_run(job["id"])
        self.assertEqual((created_session["owner"], queued_as), ("alice", ["alice"]))

    async def test_shutdown_closes_every_operators_sessions(self) -> None:
        owned = await self._create(ALICE)
        unowned = await self._create(None)

        await self.manager.shutdown()

        self.assertNotIn(owned, self.manager.sessions)
        self.assertNotIn(unowned, self.manager.sessions)


class RestAndArtifactOwnershipTests(unittest.TestCase):
    """The production app: the app-wide dependency, the /artifacts guard, share links."""

    def test_rest_routes_artifacts_and_share_links(self) -> None:
        import app.main as main_module

        manager = main_module.manager
        session_id = "owned-by-alice"
        artifact_dir = Path(manager.settings.artifact_root) / session_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "trace.zip").write_bytes(b"trace")
        manager.sessions[session_id] = BrowserSession(
            id=session_id,
            name=session_id,
            created_at=datetime.now(UTC),
            context=object(),  # type: ignore[arg-type]
            page=FakePage(),  # type: ignore[arg-type]
            artifact_dir=artifact_dir,
            auth_dir=artifact_dir / "auth",
            upload_dir=artifact_dir / "uploads",
            takeover_url="http://127.0.0.1:6080/vnc.html",
            trace_path=artifact_dir / "trace.zip",
            owner="alice",
        )
        try:
            with ExitStack() as stack:
                stack.enter_context(
                    patch.object(
                        main_module, "validate_runtime_policy", return_value=SimpleNamespace(errors=[], warnings=[])
                    )
                )
                for service in (main_module.manager, main_module.job_queue, main_module.cron_service):
                    for method_name in ("startup", "shutdown"):
                        stack.enter_context(patch.object(service, method_name, new=AsyncMock()))
                stack.enter_context(patch.object(main_module.maintenance, "startup", new=AsyncMock()))
                stack.enter_context(patch.object(main_module.maintenance, "shutdown", new=AsyncMock()))
                client = stack.enter_context(TestClient(main_module.app))
                identity = stack.enter_context(patch("app.session_ownership.get_current_operator"))

                identity.return_value = BOB
                # Routes that resolve the session, routes that read its files,
                # a query-string session id, and the static mount.
                for path in (
                    f"/sessions/{session_id}",
                    f"/sessions/{session_id}/witness",
                    f"/sessions/{session_id}/audit",
                    f"/approvals?session_id={session_id}",
                    f"/artifacts/{session_id}/trace.zip",
                ):
                    with self.subTest(path=path, operator="bob"):
                        self.assertEqual(client.get(path).status_code, 404)

                identity.return_value = ALICE
                for path in (f"/sessions/{session_id}/audit", f"/artifacts/{session_id}/trace.zip"):
                    with self.subTest(path=path, operator="alice"):
                        self.assertEqual(client.get(path).status_code, 200)

                # A share link's viewer is no operator; the token is the credential.
                identity.side_effect = get_current_operator
                token = main_module.share_manager.create_token(session_id, ttl_seconds=60)["token"]
                self.assertEqual(client.get(f"/share/{token}").status_code, 200)
        finally:
            manager.sessions.pop(session_id, None)


if __name__ == "__main__":
    unittest.main()
