"""Governed tools that name no session ask for approval in a controller-wide scope.

`workflow_profile=governed` asked for an approval only when a tool call carried a
session_id. browser.delete_memory_profile and browser.cdp_attach name none, so
they ran unapproved even under `governed`. Their approvals now belong to the
CONTROLLER_SCOPE sentinel instead of a session, and because no session owner can
decide who may see them, the approval records who asked (`owner`) and that
operator alone may list, decide, reuse and spend it.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from fastapi import Depends, FastAPI

from app.action_errors import SessionNotFoundError
from app.approvals import CONTROLLER_SCOPE, ApprovalStore
from app.browser_manager import BrowserManager, BrowserSession
from app.models import ApprovalRecord, BrowserActionDecision, McpToolCallRequest, OperatorIdentity
from app.routes._utils import require_session_access
from app.session_ownership import acting_as
from app.tool_gateway.gateway import McpToolGateway
from app.utils import UTC
from tests.test_browser_manager_create_session import _settings

ALICE = OperatorIdentity(id="alice", source="token")
BOB = OperatorIdentity(id="bob", source="token")
DELETE_ARGS = {"profile_name": "shared-login"}


class FakePage:
    url = "https://example.com"

    async def title(self) -> str:
        return "Example"


class ControllerScopeApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = BrowserManager(_settings(Path(self.tmp.name)))
        self.delete = AsyncMock(return_value=True)
        self.manager.memory = SimpleNamespace(delete=self.delete)  # type: ignore[assignment]
        self.gateway = McpToolGateway(
            manager=self.manager,
            orchestrator=SimpleNamespace(),
            job_queue=SimpleNamespace(),
            tool_profile="full",
        )

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def _call(self, operator: OperatorIdentity | None, name: str, arguments: dict, **extra):
        with acting_as(operator):
            return await self.gateway.call_tool(McpToolCallRequest(name=name, arguments={**arguments, **extra}))

    async def _delete(self, operator: OperatorIdentity | None, profile: str = "shared-login", **extra):
        return await self._call(operator, "browser.delete_memory_profile", {"profile_name": profile}, **extra)

    async def _request(self, operator: OperatorIdentity | None, profile: str = "shared-login") -> str:
        pending = await self._delete(operator, profile, workflow_profile="governed")
        self.assertEqual(pending.structuredContent.get("status"), "approval_required", pending.structuredContent)
        return pending.structuredContent["approval_id"]

    async def _approve(self, operator: OperatorIdentity | None, approval_id: str) -> None:
        with acting_as(operator):
            await self.manager.approve(approval_id, comment="ok")

    # --- the gap itself -------------------------------------------------

    async def test_sessionless_governed_tool_needs_approval_then_runs_once(self) -> None:
        pending = await self._delete(None, workflow_profile="governed")

        self.assertTrue(pending.isError)
        self.assertEqual(pending.structuredContent["status"], "approval_required")
        approval = pending.structuredContent["approval"]
        self.assertEqual((approval["session_id"], approval["kind"]), (CONTROLLER_SCOPE, "destructive"))
        self.delete.assert_not_awaited()

        approval_id = pending.structuredContent["approval_id"]
        await self._approve(None, approval_id)
        ran = await self._delete(None, workflow_profile="governed", approval_id=approval_id)

        self.assertFalse(ran.isError, ran.content[0].text)
        self.delete.assert_awaited_once()
        self.assertEqual((await self.manager.approvals.get(approval_id)).status, "executed")
        replay = await self._delete(None, workflow_profile="governed", approval_id=approval_id)
        self.assertTrue(replay.isError)
        self.delete.assert_awaited_once()

    async def test_an_unapproved_or_pending_approval_does_not_run_the_tool(self) -> None:
        approval_id = await self._request(None)

        still_pending = await self._delete(None, workflow_profile="governed", approval_id=approval_id)

        self.assertTrue(still_pending.isError)
        self.delete.assert_not_awaited()

    async def test_an_approval_covers_exactly_the_arguments_it_was_given(self) -> None:
        approval_id = await self._request(None, "shared-login")
        await self._approve(None, approval_id)

        other = await self._delete(None, "another-profile", workflow_profile="governed", approval_id=approval_id)

        self.assertTrue(other.isError)
        self.delete.assert_not_awaited()

    async def test_fast_profile_is_unchanged(self) -> None:
        ran = await self._delete(None)

        self.assertFalse(ran.isError, ran.content[0].text)
        self.delete.assert_awaited_once()
        self.assertEqual(await self.manager.approvals.list(), [])

    async def test_cdp_attach_is_governed_too(self) -> None:
        pending = await self._call(
            None, "browser.cdp_attach", {"cdp_url": "http://127.0.0.1:9222"}, workflow_profile="governed"
        )

        self.assertEqual(pending.structuredContent["status"], "approval_required", pending.structuredContent)
        self.assertEqual(pending.structuredContent["approval"]["kind"], "account_change")
        self.assertEqual(pending.structuredContent["approval"]["session_id"], CONTROLLER_SCOPE)

    async def test_the_request_is_audited_and_needs_no_session(self) -> None:
        approval_id = await self._request(None)

        events = await self.manager.audit.list(event_type="approval")

        self.assertEqual(
            [(event.action, event.session_id, event.approval_id) for event in events],
            [("approval_requested", CONTROLLER_SCOPE, approval_id)],
        )

    async def test_deciding_a_controller_approval_is_audited_without_a_session(self) -> None:
        approval_id = await self._request(None)
        await self._approve(None, approval_id)

        events = await self.manager.audit.list(event_type="approval_decision")

        self.assertEqual([(event.status, event.session_id) for event in events], [("approved", CONTROLLER_SCOPE)])

    async def test_the_audit_names_the_tool_a_controller_approval_is_for(self) -> None:
        approval_id = await self._request(None)
        await self._approve(None, approval_id)

        events = [
            *await self.manager.audit.list(event_type="approval"),
            *await self.manager.audit.list(event_type="approval_decision"),
        ]

        self.assertEqual(
            [(event.action, event.details.get("origin"), event.details.get("tool")) for event in events],
            [
                ("approval_requested", "tool_call", "browser.delete_memory_profile"),
                ("approve", "tool_call", "browser.delete_memory_profile"),
            ],
        )

    async def test_a_controller_approval_cannot_be_run_through_the_execute_endpoint(self) -> None:
        approval_id = await self._request(None)
        await self._approve(None, approval_id)

        with self.assertRaises(PermissionError) as ctx:
            await self.manager.execute_approval(approval_id)

        self.assertIn("controller-wide", str(ctx.exception))
        self.assertEqual((await self.manager.approvals.get(approval_id)).status, "approved")

    async def test_the_sentinel_is_not_a_session_a_caller_can_name(self) -> None:
        decision = BrowserActionDecision(action="request_human_takeover", reason="x", risk_category="destructive")
        controller_id = await self._request(None)
        await self._approve(None, controller_id)

        # No approval id: the session lookup refuses it, as any unknown session.
        with self.assertRaises(SessionNotFoundError):
            await self.manager.require_governed_approval(CONTROLLER_SCOPE, decision, approval_id=None)
        # With one: the sentinel must not stand in for "no session" and match.
        with self.assertRaises(SessionNotFoundError):
            await self.manager.require_governed_approval(CONTROLLER_SCOPE, decision, approval_id=controller_id)

    # --- who asked -----------------------------------------------------

    async def test_the_approval_records_the_verified_operator_who_asked(self) -> None:
        alice_id = await self._request(ALICE)
        unowned_id = await self._request(None, "another-profile")
        header_id = await self._request(OperatorIdentity(id="alice", source="header"), "third-profile")

        owners = {
            approval_id: (await self.manager.approvals.get(approval_id)).owner
            for approval_id in (alice_id, unowned_id, header_id)
        }

        self.assertEqual(owners, {alice_id: "alice", unowned_id: None, header_id: None})

    async def test_old_approval_records_load_without_an_owner(self) -> None:
        record = ApprovalRecord.model_validate(
            {
                "id": "old",
                "session_id": "abc123def456",
                "kind": "write",
                "status": "pending",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "reason": "r",
                "action": {"action": "click", "reason": "r", "selector": "#a"},
            }
        )
        self.assertIsNone(record.owner)

        root = Path(self.tmp.name) / "legacy"
        root.mkdir()
        (root / "old.json").write_text(record.model_dump_json(exclude={"owner"}), encoding="utf-8")
        self.assertNotIn("owner", json.loads((root / "old.json").read_text(encoding="utf-8")))
        store = ApprovalStore(root)
        await store.startup()
        self.assertIsNone((await store.get("old")).owner)

    async def test_another_operator_cannot_see_or_decide_a_controller_approval(self) -> None:
        approval_id = await self._request(ALICE)

        with acting_as(BOB):
            self.assertEqual(await self.manager.list_approvals(), [])
            self.assertEqual(await self.manager.list_approvals(session_id=CONTROLLER_SCOPE), [])
            for call in (self.manager.get_approval, self.manager.approve, self.manager.reject):
                with self.assertRaises(KeyError):
                    await call(approval_id)
            with self.assertRaises(KeyError):
                await self.manager.execute_approval(approval_id)
        self.assertEqual((await self.manager.approvals.get(approval_id)).status, "pending")

        with acting_as(ALICE):
            self.assertEqual([item["id"] for item in await self.manager.list_approvals()], [approval_id])
            self.assertEqual(
                [item["id"] for item in await self.manager.list_approvals(session_id=CONTROLLER_SCOPE)],
                [approval_id],
            )
            self.assertEqual((await self.manager.approve(approval_id))["status"], "approved")

    async def test_the_mcp_listing_by_scope_answers_each_operator_with_their_own(self) -> None:
        alice_id = await self._request(ALICE)
        bob_id = await self._request(BOB)

        for operator, expected in ((ALICE, alice_id), (BOB, bob_id)):
            with self.subTest(operator=operator.id):
                listed = await self._call(operator, "browser.list_approvals", {"session_id": CONTROLLER_SCOPE})
                self.assertFalse(listed.isError, listed.content[0].text)
                self.assertEqual([item["id"] for item in listed.structuredContent], [expected])

    async def test_another_operators_identical_request_gets_its_own_approval(self) -> None:
        alice_id = await self._request(ALICE)

        bob_id = await self._request(BOB)
        alice_again = await self._request(ALICE)

        self.assertNotEqual(alice_id, bob_id)
        self.assertEqual(alice_again, alice_id)
        with acting_as(BOB):
            self.assertEqual([item["id"] for item in await self.manager.list_approvals()], [bob_id])

    async def test_an_unowned_request_is_not_joined_by_an_operator(self) -> None:
        unowned_id = await self._request(None)

        alice_id = await self._request(ALICE)

        self.assertNotEqual(unowned_id, alice_id)

    async def test_another_operator_cannot_spend_an_approved_controller_approval(self) -> None:
        alice_id = await self._request(ALICE)
        await self._approve(ALICE, alice_id)

        stolen = await self._delete(BOB, workflow_profile="governed", approval_id=alice_id)

        self.assertTrue(stolen.isError)
        self.delete.assert_not_awaited()
        self.assertEqual((await self.manager.approvals.get(alice_id)).status, "approved")
        mine = await self._delete(ALICE, workflow_profile="governed", approval_id=alice_id)
        self.assertFalse(mine.isError, mine.content[0].text)
        self.delete.assert_awaited_once()

    async def test_another_operators_rejection_does_not_block_my_agent_approving(self) -> None:
        bob_id = await self._request(BOB)
        with acting_as(BOB):
            await self.manager.reject(bob_id)
        alice_id = await self._request(ALICE)

        with acting_as(ALICE):
            approved = await self.manager.approve(alice_id, decided_via="agent")

        self.assertEqual(approved["status"], "approved")

    async def test_operators_own_rejection_still_blocks_their_agent(self) -> None:
        first = await self._request(ALICE)
        with acting_as(ALICE):
            await self.manager.reject(first)
        second = await self._request(ALICE)

        with acting_as(ALICE), self.assertRaises(PermissionError):
            await self.manager.approve(second, decided_via="agent")

    # --- session approvals keep the owner-of-the-session rule ---------------

    async def test_session_approvals_still_follow_the_sessions_owner(self) -> None:
        artifact_dir = Path(self.manager.settings.artifact_root) / "unowned-1"
        artifact_dir.mkdir(parents=True)
        self.manager.sessions["unowned-1"] = BrowserSession(
            id="unowned-1",
            name="unowned-1",
            created_at=datetime.now(UTC),
            context=object(),  # type: ignore[arg-type]
            page=FakePage(),  # type: ignore[arg-type]
            artifact_dir=artifact_dir,
            auth_dir=artifact_dir / "auth",
            upload_dir=artifact_dir / "uploads",
            takeover_url="http://127.0.0.1:6080/vnc.html",
            trace_path=artifact_dir / "trace.zip",
        )
        decision = BrowserActionDecision(action="click", reason="post", element_id="op-post", risk_category="post")
        with acting_as(BOB):
            approval = await self.manager.approvals.create_or_reuse_pending(
                session_id="unowned-1", kind="post", reason="post it", action=decision
            )
        self.assertEqual(approval.owner, "bob")

        # The session is unowned, so every operator may decide it, though bob asked.
        with acting_as(ALICE):
            self.assertEqual([item["id"] for item in await self.manager.list_approvals()], [approval.id])
            self.assertEqual((await self.manager.approve(approval.id))["status"], "approved")
            used = await self.manager.approvals.require_approved(
                approval_id=approval.id, session_id="unowned-1", kind="post", action=decision
            )
        self.assertEqual(used.id, approval.id)
        with acting_as(ALICE):
            again = await self.manager.approvals.create_or_reuse_pending(
                session_id="unowned-1",
                kind="post",
                reason="post it",
                action=decision.model_copy(update={"element_id": "op-other"}),
            )
        with acting_as(BOB):
            reused = await self.manager.approvals.create_or_reuse_pending(
                session_id="unowned-1",
                kind="post",
                reason="post it",
                action=decision.model_copy(update={"element_id": "op-other"}),
            )
        self.assertEqual(reused.id, again.id)


class ControllerScopeListingRouteTests(unittest.IsolatedAsyncioTestCase):
    """GET /approvals?session_id=controller passes the app-wide session check."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = BrowserManager(_settings(Path(self.tmp.name)))
        self.app = FastAPI(dependencies=[Depends(require_session_access)])
        self.app.state.browser_manager = self.manager

        @self.app.get("/approvals")
        async def list_approvals(status: str | None = None, session_id: str | None = None):
            return await self.manager.list_approvals(status=status, session_id=session_id)

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def _get(self, operator: OperatorIdentity | None, path: str) -> httpx.Response:
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with acting_as(operator):
                return await client.get(path)

    async def test_the_sentinel_lists_only_my_controller_approvals(self) -> None:
        decision = BrowserActionDecision(action="request_human_takeover", reason="x", risk_category="destructive")
        ids = {}
        for operator in (ALICE, BOB):
            with acting_as(operator):
                ids[operator.id] = (
                    await self.manager.approvals.create_or_reuse_pending(
                        session_id=CONTROLLER_SCOPE, kind="destructive", reason="x", action=decision
                    )
                ).id
        self.assertNotEqual(ids["alice"], ids["bob"])

        for operator in (ALICE, BOB):
            with self.subTest(operator=operator.id):
                response = await self._get(operator, f"/approvals?session_id={CONTROLLER_SCOPE}")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual([item["id"] for item in response.json()], [ids[operator.id]])

    async def test_an_unknown_real_session_id_is_still_listed_empty_not_an_error(self) -> None:
        response = await self._get(ALICE, "/approvals?session_id=abcdef123456")

        self.assertEqual((response.status_code, response.json()), (200, []))


if __name__ == "__main__":
    unittest.main()
