"""An approval covers exactly the action it was asked for: replays of it with other arguments are refused.

TEST-11: the binding was tested at the store, and the gateway's governed paths
only with a mocked approval service, so no test replayed an approval through
the layers that use it: another file for an upload, another url for a
navigation, another operator's approval over REST, another value for a
governed storage write. These go through the real gateway, routes, manager
and ApprovalStore over a fake page.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI

from app.action_errors import SessionNotFoundError
from app.browser_manager import BrowserSession
from app.models import BrowserActionDecision, OperatorIdentity
from app.routes._utils import require_session_access
from app.routes.sessions import create_sessions_router
from app.session_ownership import acting_as
from app.utils import UTC
from tests.test_approval_spend import SESSION, SpendBase

ALICE = OperatorIdentity(id="alice", source="token")
BOB = OperatorIdentity(id="bob", source="token")


class ReplayTests(SpendBase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        (Path(self.settings.upload_root) / "other.txt").write_text("other", encoding="utf-8")
        self.navigations: list[str] = []
        goto = self.page.goto

        async def recorded_goto(url: str, **kwargs: Any) -> None:
            self.navigations.append(url)
            await goto(url, **kwargs)

        self.page.goto = recorded_goto  # type: ignore[method-assign]

    async def governed(self, action: dict[str, Any], approval_id: str):
        return await self.call(
            "browser.execute_action",
            session_id=SESSION,
            workflow_profile="governed",
            action=action,
            approval_id=approval_id,
        )

    async def test_an_upload_approval_does_not_cover_another_file(self) -> None:
        with self.subTest(path="manager.upload"):
            approval_id = await self.approved(
                BrowserActionDecision(action="upload", reason="attach", selector="#file", file_path="demo.txt")
            )

            with self.assertRaisesRegex(PermissionError, "does not match the requested action"):
                await self.manager.upload(
                    SESSION, selector="#file", file_path="other.txt", approved=False, approval_id=approval_id
                )
            self.assertEqual(self.page.uploads, [])
            await self.manager.upload(
                SESSION, selector="#file", file_path="demo.txt", approved=False, approval_id=approval_id
            )
            self.assertEqual([Path(path).name for path in self.page.uploads], ["demo.txt"])

        with self.subTest(path="gateway"):
            self.page.uploads.clear()
            action = {"action": "upload", "reason": "attach it", "selector": "#file", "file_path": "demo.txt"}
            approval_id = await self.approved_governed(action)

            replay = await self.governed({**action, "file_path": "other.txt"}, approval_id)

            self.assertTrue(replay.isError)
            self.assertIn("does not match the requested action", replay.content[0].text)
            self.assertEqual(self.page.uploads, [])
            self.assertEqual(await self.status(approval_id), "approved")

    async def test_a_navigation_approval_does_not_cover_another_url(self) -> None:
        action = {
            "action": "navigate",
            "reason": "go",
            "url": "https://example.com/a",
            "risk_category": "account_change",
        }
        approval_id = await self.approved_governed(action)

        replay = await self.governed({**action, "url": "https://example.com/b"}, approval_id)
        done = await self.governed(action, approval_id)

        self.assertTrue(replay.isError)
        self.assertIn("does not match the requested action", replay.content[0].text)
        self.assertFalse(done.isError, done.content[0].text)
        self.assertEqual(self.navigations, ["https://example.com/a"])

    async def test_a_governed_storage_write_covers_only_its_value(self) -> None:
        arguments = {
            "session_id": SESSION,
            "workflow_profile": "governed",
            "storage_type": "local",
            "key": "theme",
            "value": "dark",
        }
        ask = await self.call("browser.set_local_storage", **arguments)
        approval_id = ask.structuredContent["approval_id"]
        await self.manager.approve(approval_id)

        other = await self.call("browser.set_local_storage", **{**arguments, "value": "x"}, approval_id=approval_id)
        done = await self.call("browser.set_local_storage", **arguments, approval_id=approval_id)
        replay = await self.call("browser.set_local_storage", **arguments, approval_id=approval_id)

        self.assertTrue(other.isError)
        self.assertFalse(done.isError, done.content[0].text)
        self.assertTrue(replay.isError)
        self.assertEqual(self.page.evaluated_args, [["theme", "dark"]])


class OperatorReplayTests(SpendBase):
    """REST /sessions/{id}/actions/*: one operator's approval does not run an action for another."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.session.owner = "alice"
        artifact_dir = Path(self.settings.artifact_root) / "session-2"
        artifact_dir.mkdir(parents=True)
        self.manager.sessions["session-2"] = BrowserSession(
            id="session-2",
            name="session-2",
            created_at=datetime.now(UTC),
            context=object(),  # type: ignore[arg-type]
            page=self.page,  # type: ignore[arg-type]
            artifact_dir=artifact_dir,
            auth_dir=Path(self.settings.auth_root) / "session-2",
            upload_dir=Path(self.settings.upload_root) / "session-2",
            takeover_url="http://127.0.0.1:6080/vnc.html",
            trace_path=artifact_dir / "trace.zip",
            owner="bob",
        )
        self.app = FastAPI(dependencies=[Depends(require_session_access)])
        self.app.state.browser_manager = self.manager
        self.app.include_router(create_sessions_router(manager=self.manager))

    async def post(self, operator: OperatorIdentity, path: str, body: dict[str, Any]) -> httpx.Response:
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with acting_as(operator):
                return await client.post(path, json=body)

    async def test_another_operators_approval_runs_nothing_for_them(self) -> None:
        action = {"action": "click", "reason": "pay", "selector": "#pay", "risk_category": "payment"}
        with acting_as(ALICE):
            approval_id = await self.approved(BrowserActionDecision(**action))
        body = {"action": action, "approval_id": approval_id}

        # Alice's session answers Bob as if it did not exist (the app maps this to 404).
        with self.assertRaises(SessionNotFoundError):
            await self.post(BOB, f"/sessions/{SESSION}/actions/execute", body)
        on_bobs_session = await self.post(BOB, "/sessions/session-2/actions/execute", body)
        by_alice = await self.post(ALICE, f"/sessions/{SESSION}/actions/execute", body)

        self.assertEqual((on_bobs_session.status_code, on_bobs_session.json()), (403, {"detail": "Not permitted"}))
        self.assertEqual(by_alice.status_code, 200, by_alice.text)
        self.assertEqual(self.page.clicks, 1)
