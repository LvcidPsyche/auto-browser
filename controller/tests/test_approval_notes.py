"""What an agent is told about the approvals a refusal gave back, whatever the refusal and whoever answers.

Each layer that holds an approval gives it back on a refusal before the effect,
and the answer says so in ``approval_given_back`` (the outermost layer's) and
``approvals_given_back`` (every one, innermost first). The outer layer's note
replaced the inner one's, so an agent told only of the governed approval asked
again for a runtime approval it still had; and a refusal that was not a
BrowserActionError carried no note at all.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from playwright.async_api import Error as PlaywrightError

from app.action_errors import BrowserActionError, SessionNotFoundError
from app.approvals import ApprovalRequiredError, held_for_execution
from app.models import BrowserActionDecision
from app.orchestrator import BrowserOrchestrator
from app.providers.base import ProviderDecision
from tests.test_approval_spend import OFF_LIST_URL, PAST, SESSION, START_URL, WAIT_SECONDS, SpendBase

PAY = {"action": "click", "reason": "pay", "selector": "#pay", "risk_category": "payment"}


class FakeAdapter:
    default_model = "m"

    def __init__(self, decision: BrowserActionDecision) -> None:
        self.decision = decision

    async def decide(self, **_: Any) -> ProviderDecision:
        return ProviderDecision(provider="openai", model="m", decision=self.decision)


class GivenBackNoteTests(SpendBase):
    async def governed_pay(self, **ids: str):
        return await self.call(
            "browser.execute_action", session_id=SESSION, workflow_profile="governed", action=PAY, **ids
        )

    async def test_every_approval_given_back_is_named(self) -> None:
        governed = await self.approved_governed(PAY)
        inner = (await self.governed_pay(governed_approval_id=governed)).structuredContent["approval_id"]
        await self.manager.approve(inner)
        self.page.url = OFF_LIST_URL

        refused = await self.governed_pay(governed_approval_id=governed, approval_id=inner)

        named = [(note["approval_id"], note["status"]) for note in refused.structuredContent["approvals_given_back"]]
        self.assertEqual(named, [(inner, "approved"), (governed, "approved")], refused.content[0].text)
        self.assertEqual(refused.structuredContent["approval_given_back"]["approval_id"], governed)

    async def test_a_plain_permission_error_refusal_carries_the_note(self) -> None:
        governed = await self.approved_governed(PAY)
        undecided = (await self.governed_pay(governed_approval_id=governed)).structuredContent["approval_id"]

        refused = await self.governed_pay(governed_approval_id=governed, approval_id=undecided)

        self.assertEqual(refused.structuredContent.get("code"), "not_permitted", refused.content[0].text)
        note = refused.structuredContent.get("approval_given_back") or {}
        self.assertEqual((note.get("approval_id"), note.get("status")), (governed, "approved"), refused.content[0].text)
        self.assertIn(note["message"], refused.content[0].text)

    async def test_an_approval_that_expired_while_its_action_waited_says_so(self) -> None:
        decision, approval_id = await self.approved_click()
        reached, go = asyncio.Event(), asyncio.Event()

        async def snapshot(*_: Any, **__: Any) -> dict[str, Any]:
            if not go.is_set():
                reached.set()
                await go.wait()
            return {"url": START_URL}

        self.manager._light_snapshot = snapshot  # type: ignore[method-assign]
        task = asyncio.create_task(self.manager.execute_decision(SESSION, decision, approval_id=approval_id))
        await asyncio.wait_for(reached.wait(), WAIT_SECONDS)
        approval = await self.manager.approvals.get(approval_id)
        approval.approved_expires_at = PAST  # it expires while the claim waits
        await self.manager.approvals.file_store.upsert(approval)
        self.page.url = OFF_LIST_URL  # then the click is refused before it begins

        go.set()
        with self.assertRaises(BrowserActionError) as ctx:
            await task

        note = ctx.exception.payload["approval_given_back"]
        self.assertEqual((note["approval_id"], note["status"]), (approval_id, "expired"))
        self.assertIn("expired", note["message"])
        self.assertIn("repeat the call without approval_id", note["message"])
        self.assertNotIn("Retry with this approval_id", note["message"])

    async def test_an_agent_step_refused_before_its_effect_carries_the_note(self) -> None:
        decision = BrowserActionDecision(action="click", reason="save", selector="#save", risk_category="write")
        with self.assertRaises(ApprovalRequiredError) as ctx:
            await self.manager.require_governed_approval(SESSION, decision, approval_id=None)
        approval_id = ctx.exception.approval.id
        await self.manager.approve(approval_id)
        orchestrator = BrowserOrchestrator(self.manager, SimpleNamespace(get=lambda _name: FakeAdapter(decision)))
        self.manager.observe = AsyncMock(return_value={"url": START_URL})  # type: ignore[method-assign]
        orchestrator._append_agent_log = AsyncMock(return_value=None)  # type: ignore[method-assign]
        self.page.url = OFF_LIST_URL

        result = await orchestrator.step(
            session_id=SESSION,
            provider_name="openai",
            goal="save",
            approval_id=approval_id,
            workflow_profile="governed",
        )

        self.assertEqual(result.status, "error", result.error)
        note = (result.execution or {}).get("approval_given_back") or {}
        self.assertEqual((note.get("approval_id"), note.get("status")), (approval_id, "approved"), result.execution)
        self.assertEqual(await self.status(approval_id), "approved")


class InnerFailureTests(SpendBase):
    """Whatever the approval check or claim inside a hold raises, nothing has run: the outer approval is given back."""

    async def test_every_refusal_the_gateway_answers_names_the_approval_it_gave_back(self) -> None:
        # The gateway answers each kind of refusal in its own shape; each must carry the note, or the agent
        # asks again for an approval it still has. PermissionError and KeyError have their own tests.
        governed = await self.approved_governed(PAY)
        require_approved = self.manager.approvals.require_approved
        refusals = {
            "session not found": SessionNotFoundError(SESSION),
            "value error": ValueError("Provide source_selector or source_x/source_y"),
            "lookup miss": FileNotFoundError("Auth profile not found: work"),
            "os file error": FileNotFoundError(2, "No such file or directory", "/data/x"),
            "os permission error": PermissionError(13, "Permission denied", "/data/x"),
            "browser error": PlaywrightError("Target page, context or browser has been closed"),
            "anything else": LookupError("unexpected"),
        }
        for kind, refusal in refusals.items():

            async def inner_refused(*args: Any, refusal: Exception = refusal, **kwargs: Any) -> Any:
                if kwargs.get("approval_id") == "000000000000":  # the runtime approval, inside the governed hold
                    raise refusal
                return await require_approved(*args, **kwargs)

            with self.subTest(kind), patch.object(self.manager.approvals, "require_approved", inner_refused):
                refused = await self.call(
                    "browser.execute_action",
                    session_id=SESSION,
                    workflow_profile="governed",
                    action=PAY,
                    governed_approval_id=governed,
                    approval_id="000000000000",
                )

                note = refused.structuredContent.get("approval_given_back") or {}
                self.assertEqual(note.get("approval_id"), governed, refused.content[0].text)
                self.assertIn(note["message"], refused.content[0].text)
                self.assertEqual((self.page.clicks, await self.status(governed)), (0, "approved"))

    async def test_an_unknown_inner_approval_id_gives_the_governed_approval_back(self) -> None:
        governed = await self.approved_governed(PAY)
        arguments = {"session_id": SESSION, "workflow_profile": "governed", "action": PAY}

        refused = await self.call(
            "browser.execute_action", **arguments, governed_approval_id=governed, approval_id="000000000000"
        )

        self.assertTrue(refused.isError, refused.content[0].text)
        self.assertEqual((self.page.clicks, await self.status(governed)), (0, "approved"))
        self.assertEqual(refused.structuredContent.get("approval_given_back", {}).get("approval_id"), governed)

    async def test_an_inner_claim_of_an_unknown_approval_gives_the_outer_one_back(self) -> None:
        _, outer = await self.approved_click()

        with self.assertRaises(KeyError):
            async with held_for_execution(self.manager.approvals, outer, may_give_back=True):
                async with held_for_execution(self.manager.approvals, "000000000000", may_give_back=True):
                    self.fail("the inner claim of an unknown approval went through")

        self.assertEqual(await self.status(outer), "approved")
