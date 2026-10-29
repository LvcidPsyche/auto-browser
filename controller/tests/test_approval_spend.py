"""An approval that let its action start is spent, whatever fails after.

The claim on an approval was only in memory, and it was consumed only once the
whole action had returned normally: a click that went through and then failed
(the settle after it, the observation, a captcha) released the claim, and the
same approval_id clicked again (AUTH-16). Claiming now spends the approval
(status `executed`, persisted). It is given back only when the action pipeline
refused before the operation began (for an upload, before the transfer), since
then nothing ran.

The click, the typing and the upload here really happen against a fake page
(they are counted); only the page-observation helpers are stubbed.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

from playwright.async_api import Error as PlaywrightError

from app.action_errors import BrowserActionError, SessionNotFoundError
from app.approvals import ApprovalRequiredError, ApprovalStore, held_for_execution
from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.models import BrowserActionDecision, McpToolCallRequest
from app.orchestrator import BrowserOrchestrator
from app.providers.base import ProviderDecision
from app.tool_gateway.gateway import McpToolGateway
from app.utils import UTC
from app.witness import WitnessPolicyOutcome

SESSION = "session-1"
START_URL = "https://example.com"
OFF_LIST_URL = "https://elsewhere.test/"
# How long a test waits for a paused claim, and for the call once it is let go.
CLAIM_WAIT_SECONDS = 5.0
# What the given-back note says of an approval whose session closed: it cannot be run, so no retry with its id.
CLOSED_SESSION_NOTE = "but its session has closed, so nothing can run it"
PAST = (datetime.now(UTC) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
# Long enough for a loaded machine; what the tests wait on is an event, not a duration.
WAIT_SECONDS = 30


class FakeKeyboard:
    def __init__(self) -> None:
        self.typed: list[str] = []

    async def press(self, _key: str) -> None:
        return None

    async def type(self, text: str) -> None:
        self.typed.append(text)


class FakeLocator:
    def __init__(self, page: "FakePage") -> None:
        self.page = page
        self.first = self

    async def scroll_into_view_if_needed(self) -> None:
        return None

    async def bounding_box(self):
        return None

    async def get_attribute(self, _name: str) -> None:
        return None

    async def evaluate(self, *_args: Any) -> bool:
        return True  # focus checks: the field has focus

    async def click(self, **_: object) -> None:
        self.page.clicks += 1
        await self.page.after_click()

    async def set_input_files(self, path: str) -> None:
        self.page.uploads.append(path)
        if self.page.upload_error is not None:
            raise self.page.upload_error


class FakePage:
    def __init__(self) -> None:
        self.url = START_URL
        self.clicks = 0
        self.uploads: list[str] = []
        self.evaluated: list[str] = []
        self.evaluated_args: list[Any] = []
        self.keyboard = FakeKeyboard()
        # What a click does after it lands: navigate away, wait, expire the approval.
        self.click_navigates_to: str | None = None
        self.click_started = asyncio.Event()
        self.click_hold: asyncio.Event | None = None
        self.on_click: Any = None
        self.upload_error: BaseException | None = None
        self.evaluate_error: BaseException | None = None

    def locator(self, _selector: str) -> FakeLocator:
        return FakeLocator(self)

    async def after_click(self) -> None:
        self.click_started.set()
        if self.click_navigates_to:
            self.url = self.click_navigates_to
        if self.on_click is not None:
            await self.on_click()
        if self.click_hold is not None:
            await self.click_hold.wait()

    async def evaluate(self, expression: str, arg: Any = None) -> Any:
        self.evaluated.append(expression)
        self.evaluated_args.append(arg)
        if self.evaluate_error is not None:
            raise self.evaluate_error
        return "ok"

    async def goto(self, url: str, **_: object) -> None:
        self.url = url

    async def go_back(self, **_: object) -> None:
        self.url = START_URL

    async def title(self) -> str:
        return "Example Domain"


class SpendBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.settings = Settings(_env_file=None)
        for name in ("artifact", "upload", "auth", "approval", "session_store", "audit", "witness"):
            setattr(self.settings, f"{name}_root", str(root / name))
        self.settings.human_typing_min_delay_ms = 0
        self.settings.human_typing_max_delay_ms = 0
        self.manager = BrowserManager(self.settings)
        artifact_dir = Path(self.settings.artifact_root) / SESSION
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.page = FakePage()
        self.session = BrowserSession(
            id=SESSION,
            name=SESSION,
            created_at=datetime.now(UTC),
            context=object(),  # type: ignore[arg-type]
            page=self.page,  # type: ignore[arg-type]
            artifact_dir=artifact_dir,
            auth_dir=Path(self.settings.auth_root) / SESSION,
            upload_dir=Path(self.settings.upload_root) / SESSION,
            takeover_url="http://127.0.0.1:6080/vnc.html",
            trace_path=artifact_dir / "trace.zip",
        )
        self.session.auth_dir.mkdir(parents=True, exist_ok=True)
        self.session.upload_dir.mkdir(parents=True, exist_ok=True)
        (Path(self.settings.upload_root) / "demo.txt").write_text("demo", encoding="utf-8")
        self.manager.sessions[SESSION] = self.session
        # Page-observation helpers need a real browser; stub them (not the approval path).
        self.manager._light_snapshot = AsyncMock(return_value={"url": START_URL})  # type: ignore[method-assign]
        self.manager._observation_payload = AsyncMock(return_value={"url": START_URL})  # type: ignore[method-assign]
        self.manager._maybe_handle_totp = AsyncMock(return_value=None)  # type: ignore[method-assign]
        self.manager._check_bot_challenge = AsyncMock(return_value=None)  # type: ignore[method-assign]
        self.manager._settle = AsyncMock(return_value=None)  # type: ignore[method-assign]
        self.gateway = McpToolGateway(
            manager=self.manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace(), tool_profile="full"
        )

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def approved(self, decision: BrowserActionDecision) -> str:
        with self.assertRaises(ApprovalRequiredError) as ctx:
            await self.manager.execute_decision(SESSION, decision)
        approval_id = ctx.exception.approval.id
        await self.manager.approve(approval_id, comment="ok")
        return approval_id

    async def approved_click(self) -> tuple[BrowserActionDecision, str]:
        decision = BrowserActionDecision(
            action="click", reason="submit payment", selector="#pay", risk_category="payment"
        )
        return decision, await self.approved(decision)

    async def status(self, approval_id: str) -> str:
        return (await self.manager.approvals.get(approval_id)).status

    async def call(self, name: str, **arguments: Any):
        return await self.gateway.call_tool(McpToolCallRequest(name=name, arguments=arguments))

    async def approved_governed(self, action: dict[str, Any]) -> str:
        ask = await self.call("browser.execute_action", session_id=SESSION, workflow_profile="governed", action=action)
        self.assertEqual(ask.structuredContent.get("status"), "approval_required", ask.content[0].text)
        await self.manager.approve(ask.structuredContent["approval_id"], comment="ok")
        return ask.structuredContent["approval_id"]

    async def receipt(self) -> dict[str, Any]:
        # The latest browser_action receipt: the listing is newest first.
        receipts = await self.manager.list_witness_receipts(SESSION)
        return [item for item in receipts if item["event_type"] == "browser_action"][0]


class Auth16Tests(SpendBase):
    async def test_settle_raises_after_click_then_approval_reused(self) -> None:
        decision, approval_id = await self.approved_click()
        self.manager._settle = AsyncMock(side_effect=PlaywrightError("settle failed"))  # type: ignore[method-assign]
        with self.assertRaises(Exception):
            await self.manager.execute_decision(SESSION, decision, approval_id=approval_id)
        self.assertEqual(self.page.clicks, 1)  # the effect happened
        self.assertEqual(await self.status(approval_id), "executed", "approval not consumed after the click ran")
        with self.assertRaises(PermissionError):
            await self.manager.execute_decision(SESSION, decision, approval_id=approval_id)
        self.assertEqual(self.page.clicks, 1, "second click ran on the same approval")

    async def test_after_snapshot_raises_then_approval_reused(self) -> None:
        decision, approval_id = await self.approved_click()
        self.manager._observation_payload = AsyncMock(side_effect=RuntimeError("after-snapshot boom"))  # type: ignore[method-assign]
        with self.assertRaises(RuntimeError):
            await self.manager.execute_decision(SESSION, decision, approval_id=approval_id)
        self.assertEqual(self.page.clicks, 1)
        self.manager._observation_payload = AsyncMock(return_value={"url": START_URL})  # type: ignore[method-assign]
        self.assertEqual(await self.status(approval_id), "executed", "approval not consumed after the click ran")
        with self.assertRaises(PermissionError):
            await self.manager.execute_decision(SESSION, decision, approval_id=approval_id)
        self.assertEqual(self.page.clicks, 1, "second click ran on the same approval")

    async def test_second_execution_actually_happens(self) -> None:
        decision, approval_id = await self.approved_click()
        self.manager._settle = AsyncMock(side_effect=[PlaywrightError("settle failed"), None, None, None])  # type: ignore[method-assign]
        with self.assertRaises(Exception):
            await self.manager.execute_decision(SESSION, decision, approval_id=approval_id)
        with self.assertRaises(PermissionError):
            await self.manager.execute_decision(SESSION, decision, approval_id=approval_id)
        self.assertEqual(self.page.clicks, 1, "approval for ONE payment click ran the click twice")


class GiveBackTests(SpendBase):
    """Refused before the operation, nothing ran: the same approval runs the click once the cause is gone."""

    async def assert_given_back(self, decision: BrowserActionDecision, approval_id: str) -> None:
        with self.assertRaises(BrowserActionError) as ctx:
            await self.manager.execute_decision(SESSION, decision, approval_id=approval_id)
        self.assertEqual(ctx.exception.code, "browser_action_blocked")
        approval = await self.manager.approvals.get(approval_id)
        self.assertEqual((self.page.clicks, approval.status, approval.executed_at), (0, "approved", None))

    async def assert_retry_runs_once(self, decision: BrowserActionDecision, approval_id: str) -> None:
        await self.manager.execute_decision(SESSION, decision, approval_id=approval_id)
        self.assertEqual((self.page.clicks, await self.status(approval_id)), (1, "executed"))

    async def test_executing_an_approval_of_a_closed_session_is_refused_and_leaves_it_approved(self) -> None:
        """B-COMPOSE-3 / B-LC-4: execute_approval claimed the approval before it resolved the session for every
        action but an upload, so an approval of a session that had closed (every approval a governed cron
        run asked for, once the run ended) was spent with nothing run."""
        decision, approval_id = await self.approved_click()
        del self.manager.sessions[SESSION]  # the session closed

        with self.assertRaises(SessionNotFoundError):
            await self.manager.execute_approval(approval_id)

        approval = await self.manager.approvals.get(approval_id)
        self.assertEqual((self.page.clicks, approval.status, approval.executed_at), (0, "approved", None))
        self.manager.sessions[SESSION] = self.session  # the same session again: the approval still runs once
        await self.manager.execute_approval(approval_id)
        self.assertEqual((self.page.clicks, await self.status(approval_id)), (1, "executed"))

    async def test_a_session_that_closes_while_its_approval_is_claimed_gives_the_approval_back(self) -> None:
        """Item 26 (Codex C 3): execute_approval resolves the session before it claims (item 18), and the action
        looks it up again after the claim. A session closed in between was refused there with nothing run, and
        the approval stayed spent. A click and an upload: each looks the session up in its own service."""
        self.settings.require_approval_for_uploads = True
        click = {"action": "click", "reason": "submit payment", "selector": "#pay", "risk_category": "payment"}
        upload = {"action": "upload", "reason": "attach", "selector": "#file", "file_path": "demo.txt"}
        for action in (click, upload):
            with self.subTest(action=action["action"]):
                self.manager.sessions[SESSION] = self.session
                approval_id = await self.approved(BrowserActionDecision(**action))
                answer = await self.execute_while_the_session_closes_during_the_claim(approval_id)

                self.assertTrue(answer.isError, answer.content[0].text)
                self.assertEqual(answer.structuredContent.get("code"), "unknown_session", answer.structuredContent)
                notes = answer.structuredContent.get("approval_given_back", {})
                self.assertEqual(notes.get("status"), "approved", answer.structuredContent)
                # Item 46 (Fable E1-F5): the note advised a retry with this approval_id once the cause is gone,
                # and a closed session never comes back.
                self.assertIn(CLOSED_SESSION_NOTE, notes.get("message", ""))
                self.assertNotIn("Retry with this approval_id", notes.get("message", ""))
                approval = await self.manager.approvals.get(approval_id)
                self.assertEqual((approval.status, approval.executed_at), ("approved", None))
                self.assertEqual((self.page.clicks, self.page.uploads), (0, []))

    async def test_a_session_closed_while_execute_decision_reads_the_approval_gives_it_back(self) -> None:
        """Item 42 (Fable E1-F1): execute_decision resolved the session, then read the approval in a worker
        thread, and the dispatched click looked the session up again outside its pipeline: a close landing
        during that read was raised there unrecorded, and the approval stayed spent with nothing run."""
        _, approval_id = await self.approved_click()
        store = self.manager.approvals
        reading, release = asyncio.Event(), asyncio.Event()
        require_approved = store.require_approved

        async def paused_read(**kwargs: Any) -> Any:
            approval = await require_approved(**kwargs)
            if not reading.is_set():
                reading.set()
                await release.wait()
            return approval

        store.require_approved = paused_read  # type: ignore[method-assign]
        call = asyncio.create_task(self.call("browser.execute_approval", approval_id=approval_id))
        await asyncio.wait_for(reading.wait(), CLAIM_WAIT_SECONDS)
        del self.manager.sessions[SESSION]  # after execute_decision's lookup, before the click's own
        release.set()
        answer = await asyncio.wait_for(call, CLAIM_WAIT_SECONDS)

        await self.assert_answered_given_back(answer, approval_id)

    async def test_an_upload_whose_file_goes_while_its_approval_is_claimed_gives_the_approval_back(self) -> None:
        """Item 42: execute_approval checks the upload's file, then claims, and the upload resolves the file
        again: one deleted during the claim was refused there, outside the pipeline, unrecorded, and the
        approval stayed spent."""
        self.settings.require_approval_for_uploads = True
        upload = {"action": "upload", "reason": "attach", "selector": "#file", "file_path": "demo.txt"}
        approval_id = await self.approved(BrowserActionDecision(**upload))
        staged = Path(self.settings.upload_root) / "demo.txt"

        answer = await self.execute_while_the_claim_is_written(approval_id, staged.unlink)

        await self.assert_answered_given_back(answer, approval_id)

    async def test_a_navigate_allowed_hosts_refuses_is_refused_before_an_approval_is_asked_for(self) -> None:
        """Item 43 (Fable E1-F2): navigate checks ALLOWED_HOSTS only once it runs, so an operator was asked to
        approve a navigation that could never run, and its approval was then spent with nothing run. The
        runtime's ask (execute_decision) and the governed one (browser.execute_action) both check first."""
        decision = BrowserActionDecision(action="navigate", reason="go pay", url=OFF_LIST_URL, risk_category="payment")
        with self.subTest(ask="runtime"):
            with self.assertRaisesRegex(PermissionError, "not allowlisted"):
                await self.manager.execute_decision(SESSION, decision)
            self.assertEqual(await self.manager.approvals.list(), [])
        with self.subTest(ask="governed"):
            answer = await self.call(
                "browser.execute_action",
                session_id=SESSION,
                workflow_profile="governed",
                action={"action": "navigate", "reason": "go", "url": OFF_LIST_URL, "risk_category": "write"},
            )
            self.assertTrue(answer.isError, answer.structuredContent)
            self.assertIn("not allowlisted", answer.content[0].text)
            self.assertEqual(await self.manager.approvals.list(), [])

    async def test_control_a_navigate_whose_host_leaves_the_list_after_its_approve_gives_the_approval_back(
        self,
    ) -> None:
        """Item 43, the other order: refused once the approval is claimed, and given back (item 42's record)."""
        decision = BrowserActionDecision(
            action="navigate", reason="go pay", url=f"{START_URL}/pay", risk_category="payment"
        )
        approval_id = await self.approved(decision)
        self.settings.allowed_hosts = "localhost"

        answer = await self.call("browser.execute_approval", approval_id=approval_id)

        await self.assert_answered_given_back(answer, approval_id)
        self.assertEqual(self.page.url, START_URL)

    async def test_a_governed_upload_whose_file_goes_after_the_gateways_check_gives_the_approval_back(self) -> None:
        """Item 55 (Codex F 4): execute_decision checks the upload's file again before it dispatches, outside
        _dispatch_recorded: under the gateway's claim, a file gone since the gateway's check was refused there
        unrecorded, and the approval stayed spent with nothing transferred."""
        self.settings.require_approval_for_uploads = False
        upload = {"action": "upload", "reason": "attach", "selector": "#file", "file_path": "demo.txt"}
        approval_id = await self.approved_governed(upload)
        staged = Path(self.settings.upload_root) / "demo.txt"

        def execute() -> Any:
            return self.call(
                "browser.execute_action",
                session_id=SESSION,
                workflow_profile="governed",
                action=upload,
                approval_id=approval_id,
            )

        answer = await self.execute_while_the_claim_is_written(approval_id, staged.unlink, tool=execute)

        await self.assert_answered_given_back(answer, approval_id)

    async def assert_answered_given_back(self, answer: Any, approval_id: str) -> None:
        self.assertTrue(answer.isError, answer.content[0].text)
        notes = answer.structuredContent.get("approval_given_back", {})
        self.assertEqual(notes.get("status"), "approved", answer.structuredContent)
        approval = await self.manager.approvals.get(approval_id)
        self.assertEqual((approval.status, approval.executed_at), ("approved", None))
        self.assertEqual((self.page.clicks, self.page.uploads), (0, []))

    async def execute_while_the_session_closes_during_the_claim(self, approval_id: str):
        """browser.execute_approval of ``approval_id``, its session closed while the claim (executed) is written."""

        def close() -> None:
            del self.manager.sessions[SESSION]

        return await self.execute_while_the_claim_is_written(approval_id, close)

    async def execute_while_the_claim_is_written(self, approval_id: str, meanwhile: Any, *, tool: Any = None):
        """``tool`` (browser.execute_approval of ``approval_id`` when None), ``meanwhile()`` run while the claim
        (executed) is written."""
        store = self.manager.approvals
        writing, release = asyncio.Event(), asyncio.Event()
        persist = store._persist

        async def paused_claim(approval: Any) -> None:
            if approval.id == approval_id and approval.status == "executed" and not writing.is_set():
                writing.set()
                await release.wait()
            await persist(approval)

        store._persist = paused_claim  # type: ignore[method-assign]
        try:
            call = asyncio.create_task(
                tool() if tool else self.call("browser.execute_approval", approval_id=approval_id)
            )
            await asyncio.wait_for(writing.wait(), CLAIM_WAIT_SECONDS)
            meanwhile()
            release.set()
            return await asyncio.wait_for(call, CLAIM_WAIT_SECONDS)
        finally:
            store._persist = persist  # type: ignore[method-assign]

    async def test_a_page_off_the_list_gives_the_approval_back(self) -> None:
        decision, approval_id = await self.approved_click()
        self.page.url = OFF_LIST_URL

        await self.assert_given_back(decision, approval_id)

        self.page.url = START_URL
        await self.assert_retry_runs_once(decision, approval_id)

    async def test_a_witness_that_is_not_ready_gives_the_approval_back(self) -> None:
        decision, approval_id = await self.approved_click()
        self.session.witness_remote_state.required = True

        await self.assert_given_back(decision, approval_id)

        self.session.witness_remote_state.required = False
        await self.assert_retry_runs_once(decision, approval_id)

    async def test_a_witness_policy_block_gives_the_approval_back(self) -> None:
        decision, approval_id = await self.approved_click()
        evaluate = self.manager.witness_policy.evaluate_action
        self.manager.witness_policy.evaluate_action = Mock(  # type: ignore[method-assign]
            return_value=WitnessPolicyOutcome(profile="normal", should_block=True, block_reason="not here")
        )

        await self.assert_given_back(decision, approval_id)

        self.manager.witness_policy.evaluate_action = evaluate  # type: ignore[method-assign]
        await self.assert_retry_runs_once(decision, approval_id)

    async def test_an_approval_given_back_can_be_rejected(self) -> None:
        decision, approval_id = await self.approved_click()
        self.page.url = OFF_LIST_URL
        await self.assert_given_back(decision, approval_id)

        await self.manager.reject(approval_id, comment="changed my mind")

        self.page.url = START_URL
        with self.assertRaisesRegex(PermissionError, "is not approved"):
            await self.manager.execute_decision(SESSION, decision, approval_id=approval_id)
        self.assertEqual(self.page.clicks, 0)

    async def test_sensitive_text_survives_a_give_back_and_is_dropped_once_spent(self) -> None:
        decision = BrowserActionDecision(
            action="type", reason="card", selector="#card", text="4111", sensitive=True, risk_category="payment"
        )
        approval_id = await self.approved(decision)
        self.page.url = OFF_LIST_URL
        with self.assertRaises(BrowserActionError):
            await self.manager.execute_approval(approval_id)

        self.page.url = START_URL
        await self.manager.execute_approval(approval_id)

        self.assertEqual("".join(self.page.keyboard.typed), "4111")
        with self.assertRaisesRegex(PermissionError, "no longer holds"):
            self.manager.approvals.executable_action(await self.manager.approvals.get(approval_id))


class StaysSpentTests(SpendBase):
    """Once the operation began, every way the action can end leaves the approval spent."""

    async def assert_spent(self, decision: BrowserActionDecision, approval_id: str, raises: type = BrowserActionError):
        with self.assertRaises(raises) as ctx:
            await self.manager.execute_decision(SESSION, decision, approval_id=approval_id)
        self.assertEqual((self.page.clicks, await self.status(approval_id)), (1, "executed"))
        with self.assertRaisesRegex(PermissionError, "was spent when its action started"):
            await self.manager.execute_decision(SESSION, decision, approval_id=approval_id)
        self.assertEqual(self.page.clicks, 1, "the same approval clicked again")
        return ctx.exception

    async def test_a_runtime_url_block_after_the_click_stays_spent(self) -> None:
        decision, approval_id = await self.approved_click()
        self.page.click_navigates_to = OFF_LIST_URL

        refused = await self.assert_spent(decision, approval_id)

        self.assertEqual((refused.code, refused.status_code), ("browser_action_blocked", 403))

    async def test_a_captcha_after_the_click_stays_spent(self) -> None:
        decision, approval_id = await self.approved_click()
        self.manager._check_bot_challenge = AsyncMock(return_value={"signal": "captcha"})  # type: ignore[method-assign]

        refused = await self.assert_spent(decision, approval_id)

        self.assertEqual(refused.code, "captcha_detected")

    async def test_a_failure_recording_the_click_stays_spent(self) -> None:
        decision, approval_id = await self.approved_click()
        self.manager._persist_session = AsyncMock(side_effect=OSError("disk full"))  # type: ignore[method-assign]

        await self.assert_spent(decision, approval_id, raises=OSError)

    async def test_a_click_cancelled_mid_operation_stays_spent(self) -> None:
        decision, approval_id = await self.approved_click()
        self.page.click_hold = asyncio.Event()
        task = asyncio.create_task(self.manager.execute_decision(SESSION, decision, approval_id=approval_id))
        await asyncio.wait_for(self.page.click_started.wait(), WAIT_SECONDS)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.page.click_hold = None
        self.assertEqual(await self.status(approval_id), "executed")
        with self.assertRaisesRegex(PermissionError, "was spent when its action started"):
            await self.manager.execute_decision(SESSION, decision, approval_id=approval_id)
        self.assertEqual(self.page.clicks, 1)

    async def test_a_restart_mid_click_finds_the_approval_spent(self) -> None:
        decision, approval_id = await self.approved_click()
        self.page.click_hold = asyncio.Event()
        task = asyncio.create_task(self.manager.execute_decision(SESSION, decision, approval_id=approval_id))
        await asyncio.wait_for(self.page.click_started.wait(), WAIT_SECONDS)
        try:
            restarted = ApprovalStore(self.settings.approval_root)
            await restarted.startup()

            self.assertEqual((await restarted.get(approval_id)).status, "executed")
            with self.assertRaisesRegex(PermissionError, "was spent when its action started"):
                await restarted.claim_execution(approval_id)
        finally:
            self.page.click_hold.set()
            await task

    async def test_an_operator_cannot_decide_an_approval_while_its_action_runs(self) -> None:
        decision, approval_id = await self.approved_click()
        self.page.click_hold = asyncio.Event()
        task = asyncio.create_task(self.manager.execute_decision(SESSION, decision, approval_id=approval_id))
        await asyncio.wait_for(self.page.click_started.wait(), WAIT_SECONDS)
        try:
            for decide in (self.manager.approve, self.manager.reject):
                with self.subTest(decide=decide.__name__), self.assertRaisesRegex(PermissionError, "already been"):
                    await decide(approval_id)
        finally:
            self.page.click_hold.set()
            await task
        self.assertEqual(await self.status(approval_id), "executed")

    async def test_an_approval_that_expires_during_its_click_is_not_refused_afterwards(self) -> None:
        decision, approval_id = await self.approved_click()

        async def expire() -> None:
            approval = await self.manager.approvals.get(approval_id)
            approval.approved_expires_at = PAST
            await self.manager.approvals.file_store.upsert(approval)

        self.page.on_click = expire

        await self.manager.execute_decision(SESSION, decision, approval_id=approval_id)

        self.assertEqual((self.page.clicks, await self.status(approval_id)), (1, "executed"))

    async def test_a_totp_step_that_fails_after_the_click_stays_spent(self) -> None:
        decision, approval_id = await self.approved_click()

        async def totp(_session: Any) -> None:
            self.page.keyboard.typed.append("123456")  # the autofill typed a live code, then failed
            raise PlaywrightError("the code field went away")

        self.manager._maybe_handle_totp = totp  # type: ignore[method-assign]

        await self.assert_spent(decision, approval_id)

        self.assertEqual(self.page.keyboard.typed, ["123456"])

    async def test_a_tool_call_that_fails_after_its_effect_stays_spent(self) -> None:
        ask = await self.call("browser.eval_js", session_id=SESSION, expression="1")
        approval_id = ask.structuredContent["approval_id"]
        await self.manager.approve(approval_id)
        self.page.evaluate_error = PlaywrightError("Execution context was destroyed")

        failed = await self.call("browser.eval_js", session_id=SESSION, expression="1", approval_id=approval_id)
        replay = await self.call("browser.eval_js", session_id=SESSION, expression="1", approval_id=approval_id)

        self.assertTrue(failed.isError)
        self.assertTrue(replay.isError)
        self.assertEqual(self.page.evaluated, ["1"])
        self.assertEqual(await self.status(approval_id), "executed")


class UploadSpendTests(SpendBase):
    """An upload's change begins with the transfer: once that is called, the approval is spent."""

    UPLOAD = {"action": "upload", "reason": "attach", "selector": "#file", "file_path": "demo.txt"}

    async def test_an_upload_that_delivers_and_then_raises_stays_spent(self) -> None:
        with self.assertRaises(ApprovalRequiredError) as ctx:
            await self.manager.upload(SESSION, selector="#file", file_path="demo.txt", approved=False)
        approval_id = ctx.exception.approval.id
        await self.manager.approve(approval_id)
        # The file went in, and then the answer was lost.
        self.page.upload_error = PlaywrightError("Timeout 30000ms exceeded.")

        with self.assertRaises(BrowserActionError):
            await self.manager.upload(
                SESSION, selector="#file", file_path="demo.txt", approved=False, approval_id=approval_id
            )

        self.assertEqual(len(self.page.uploads), 1)
        self.assertEqual(await self.status(approval_id), "executed")

    async def test_a_governed_upload_failing_after_the_transfer_stays_spent_under_both_settings(self) -> None:
        # F4: with REQUIRE_APPROVAL_FOR_UPLOADS off there is no upload approval
        # inside the gateway's, and the gateway's was released after the file went in.
        self.manager._check_bot_challenge = AsyncMock(return_value={"signal": "captcha"})  # type: ignore[method-assign]
        for required in (True, False):
            with self.subTest(require_approval_for_uploads=required):
                self.settings.require_approval_for_uploads = required
                self.page.uploads.clear()
                action = {**self.UPLOAD, "reason": f"attach ({required})"}
                approval_id = await self.approved_governed(action)
                arguments = {"session_id": SESSION, "workflow_profile": "governed", "action": action}

                failed = await self.call("browser.execute_action", **arguments, approval_id=approval_id)
                replay = await self.call("browser.execute_action", **arguments, approval_id=approval_id)

                self.assertEqual(failed.structuredContent.get("code"), "captcha_detected", failed.content[0].text)
                self.assertTrue(replay.isError)
                self.assertEqual(len(self.page.uploads), 1)
                self.assertEqual(await self.status(approval_id), "executed")


class NestedHoldTests(SpendBase):
    """Each path that holds an approval, with a layer inside it that checks the same approval again."""

    PAY = {"action": "click", "reason": "pay", "selector": "#pay", "risk_category": "payment"}
    UPLOAD = {"action": "upload", "reason": "attach", "selector": "#file", "file_path": "demo.txt"}

    async def assert_ran_once(self, approval_id: str, replay) -> None:
        self.assertEqual((self.page.clicks + len(self.page.uploads), await self.status(approval_id)), (1, "executed"))
        try:
            refused = (await replay()).isError
        except PermissionError:
            refused = True
        self.assertTrue(refused, "the same approval ran its action again")
        self.assertEqual(self.page.clicks + len(self.page.uploads), 1)

    async def test_the_gateway_holds_and_execute_decision_checks_again(self) -> None:
        for action in (self.PAY, self.UPLOAD):
            with self.subTest(action=action["action"]):
                self.page.clicks, self.page.uploads = 0, []
                approval_id = await self.approved_governed(action)
                arguments = {"session_id": SESSION, "workflow_profile": "governed", "action": action}

                done = await self.call("browser.execute_action", **arguments, approval_id=approval_id)

                self.assertFalse(done.isError, done.content[0].text)
                await self.assert_ran_once(
                    approval_id, lambda: self.call("browser.execute_action", **arguments, approval_id=approval_id)
                )

    async def test_the_orchestrator_holds_and_execute_decision_checks_again(self) -> None:
        decision = BrowserActionDecision(**self.PAY)
        with self.assertRaises(ApprovalRequiredError) as ctx:
            await self.manager.require_governed_approval(SESSION, decision, approval_id=None)
        approval_id = ctx.exception.approval.id
        await self.manager.approve(approval_id)
        orchestrator = BrowserOrchestrator(self.manager, SimpleNamespace())

        async def step():
            return await orchestrator._execute_decision(
                session_id=SESSION,
                goal="pay",
                observation={},
                provider_decision=ProviderDecision(provider="openai", model="m", decision=decision),
                upload_approved=False,
                approval_id=approval_id,
                workflow_profile="governed",
            )

        self.assertEqual((await step()).status, "acted")
        await self.assert_ran_once(approval_id, step)

    async def test_execute_approval_holds_and_the_action_checks_again(self) -> None:
        for action in (self.PAY, self.UPLOAD):
            with self.subTest(action=action["action"]):
                self.page.clicks, self.page.uploads = 0, []
                approval_id = await self.approved(BrowserActionDecision(**action))

                result = await self.manager.execute_approval(approval_id)

                self.assertEqual(result["approval"]["status"], "executed")
                await self.assert_ran_once(approval_id, lambda: self.manager.execute_approval(approval_id))

    async def test_an_approval_that_expires_after_its_claim_still_runs_its_action(self) -> None:
        # The outer layer's claim checked the expiry; the check inside it must not refuse a run under way.
        _, approval_id = await self.approved_click()
        store = self.manager.approvals
        claim = store.claim_execution

        async def claim_then_expire(claimed_id: str):
            hold = await claim(claimed_id)
            approval = await store.get(claimed_id)
            approval.approved_expires_at = PAST
            await store.file_store.upsert(approval)
            return hold

        store.claim_execution = claim_then_expire  # type: ignore[method-assign]

        await self.manager.execute_approval(approval_id)

        self.assertEqual((self.page.clicks, await self.status(approval_id)), (1, "executed"))

    async def test_the_effect_mark_outlives_later_runs_and_reaches_the_outer_hold(self) -> None:
        # Two pipeline runs under one outer approval, the first under an approval of its own:
        # the first click marks both, so the second run's refusal gives neither back.
        outer = await self.manager.approvals.create_or_reuse_pending(
            session_id=SESSION, kind="write", reason="r", action=BrowserActionDecision(action="wait", reason="r")
        )
        await self.manager.approve(outer.id)
        decision, inner_id = await self.approved_click()

        with self.assertRaises(BrowserActionError):
            async with held_for_execution(self.manager.approvals, outer.id):
                await self.manager.execute_decision(SESSION, decision, approval_id=inner_id)
                self.page.url = OFF_LIST_URL
                await self.manager.wait(SESSION, 0)

        self.assertEqual((await self.status(outer.id), await self.status(inner_id)), ("executed", "executed"))


class ReceiptTests(SpendBase):
    """A failed action's receipt says what happened to its approval, apart from how the action ended."""

    async def fail_click(self, decision: BrowserActionDecision, approval_id: str) -> BrowserActionError:
        with self.assertRaises(BrowserActionError) as ctx:
            await self.manager.execute_decision(SESSION, decision, approval_id=approval_id)
        return ctx.exception

    async def test_a_block_before_the_click_records_the_approval_given_back(self) -> None:
        decision, approval_id = await self.approved_click()
        self.page.url = OFF_LIST_URL

        await self.fail_click(decision, approval_id)

        receipt = await self.receipt()
        self.assertEqual((receipt["status"], receipt["approval"]["status"]), ("blocked", "approved"))
        self.assertNotIn("after_change", receipt["target"])

    async def test_a_block_after_the_click_records_it_spent_and_names_the_step(self) -> None:
        decision, approval_id = await self.approved_click()
        self.page.click_navigates_to = OFF_LIST_URL

        refused = await self.fail_click(decision, approval_id)

        receipt = await self.receipt()
        self.assertEqual((receipt["status"], receipt["approval"]["status"]), ("blocked", "executed"))
        self.assertIn("elsewhere.test", receipt["target"]["after_change"])
        self.assertIn("do not repeat the action", receipt["target"]["after_change"])
        self.assertEqual((refused.code, refused.status_code), ("browser_action_blocked", 403))

    async def test_a_failure_after_the_click_records_it_spent(self) -> None:
        failures = {
            "captcha": (AsyncMock(return_value={"signal": "x"}), AsyncMock(return_value=None)),
            "playwright": (AsyncMock(return_value=None), AsyncMock(side_effect=PlaywrightError("gone"))),
        }
        for name, (challenge, settle) in failures.items():
            with self.subTest(failure=name):
                decision = BrowserActionDecision(
                    action="click", reason=f"pay {name}", selector="#pay", risk_category="payment"
                )
                approval_id = await self.approved(decision)
                self.manager._check_bot_challenge = challenge  # type: ignore[method-assign]
                self.manager._settle = settle  # type: ignore[method-assign]

                await self.fail_click(decision, approval_id)

                receipt = await self.receipt()
                self.assertEqual((receipt["status"], receipt["approval"]["status"]), ("failed", "executed"))

    async def test_a_failed_action_without_an_approval_records_no_approval_status(self) -> None:
        self.page.url = OFF_LIST_URL

        with self.assertRaises(BrowserActionError):
            await self.manager.wait(SESSION, 0)

        receipt = await self.receipt()
        self.assertEqual((receipt["status"], receipt["approval"]["status"]), ("blocked", None))


if __name__ == "__main__":
    unittest.main()
