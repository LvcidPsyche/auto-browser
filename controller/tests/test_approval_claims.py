"""What a claimed approval covers: one action, run by the execution that claimed it.

The claim on an approval is shared with every task the execution runs in, so a
layer inside it does not claim again. That sharing let a task that had entered
the execution act after its owner gave the approval back (the approval was then
approved again, and a retry acted a second time), and let one action approval
cover any number of operations run under it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import httpx
from fastapi import FastAPI

from app.action_errors import BrowserActionError
from app.approvals import ApprovalRequiredError, held_action_approval, held_for_execution, refused_before_effect
from app.models import BrowserActionDecision
from app.orchestrator import BrowserOrchestrator
from app.providers.base import ProviderDecision
from app.routes.operations import create_operations_router
from app.witness import WitnessPolicyOutcome
from tests.test_approval_spend import OFF_LIST_URL, SESSION, START_URL, SpendBase

# Long enough for a loaded machine; the events below are what the tests wait on.
WAIT_SECONDS = 30


class OneExecutionTests(SpendBase):
    async def test_a_child_that_entered_the_hold_cannot_act_once_the_approval_is_given_back(self) -> None:
        decision, approval_id = await self.approved_click()
        reached, go = asyncio.Event(), asyncio.Event()

        async def snapshot(*_: Any, **__: Any) -> dict[str, Any]:
            reached.set()
            await go.wait()
            return {"url": START_URL}

        self.manager._light_snapshot = snapshot  # type: ignore[method-assign]
        refusal = PermissionError("refused before the operation")

        with self.assertRaises(PermissionError):
            async with held_for_execution(self.manager.approvals, approval_id, may_give_back=True):
                # Entered while the hold is live: past every approval check, waiting in the pipeline.
                child = asyncio.create_task(self.manager.execute_decision(SESSION, decision, approval_id=approval_id))
                await asyncio.wait_for(reached.wait(), WAIT_SECONDS)
                refused_before_effect(refusal)
                raise refusal
        go.set()

        with self.assertRaises(BrowserActionError) as ctx:
            await child
        self.assertIsInstance(ctx.exception.__cause__, PermissionError)
        self.assertIn("released", str(ctx.exception.__cause__))
        self.assertEqual((self.page.clicks, await self.status(approval_id)), (0, "approved"))

        await self.manager.execute_decision(SESSION, decision, approval_id=approval_id)
        self.assertEqual((self.page.clicks, await self.status(approval_id)), (1, "executed"))

    async def test_a_child_released_while_the_give_back_is_recorded_cannot_act(self) -> None:
        decision, approval_id = await self.approved_click()
        reached, go = asyncio.Event(), asyncio.Event()

        async def snapshot(*_: Any, **__: Any) -> dict[str, Any]:
            if not go.is_set():
                reached.set()
                await go.wait()
            return {"url": START_URL}

        self.manager._light_snapshot = snapshot  # type: ignore[method-assign]
        children: list[asyncio.Task] = []
        append = self.manager.audit.append

        async def append_once_the_child_ran(**event: Any) -> Any:
            if event.get("event_type") == "approval_given_back":
                go.set()  # the child resumes while the give-back is being recorded
                await asyncio.wait(children, timeout=WAIT_SECONDS)
            return await append(**event)

        self.manager.audit.append = append_once_the_child_ran  # type: ignore[method-assign]
        refusal = PermissionError("refused before the operation")

        with self.assertRaises(PermissionError):
            async with held_for_execution(self.manager.approvals, approval_id, may_give_back=True):
                child = asyncio.create_task(self.manager.execute_decision(SESSION, decision, approval_id=approval_id))
                children.append(child)
                await asyncio.wait_for(reached.wait(), WAIT_SECONDS)
                refused_before_effect(refusal)
                raise refusal

        with self.assertRaises(BrowserActionError) as ctx:
            # Bounded: with no give-back audit, nothing lets the child go, and the test fails, not hangs.
            await asyncio.wait_for(child, WAIT_SECONDS)
        self.assertIn("released", str(ctx.exception.__cause__))
        self.assertEqual((self.page.clicks, await self.status(approval_id)), (0, "approved"))

    async def test_an_action_approval_covers_one_operation(self) -> None:
        async def in_a_child(decision: Any, approval_id: str) -> None:
            await asyncio.create_task(self.manager.execute_decision(SESSION, decision, approval_id=approval_id))

        async def in_the_same_task(_decision: Any, _approval_id: str) -> None:
            await self.manager.click(SESSION, selector="#pay")

        for second in (in_a_child, in_the_same_task):
            with self.subTest(second=second.__name__):
                decision, approval_id = await self.approved_click()
                self.page.clicks = 0

                async with held_for_execution(self.manager.approvals, approval_id):
                    await self.manager.execute_decision(SESSION, decision, approval_id=approval_id)
                    with self.assertRaises(BrowserActionError) as ctx:
                        await second(decision, approval_id)

                self.assertIn("one action", str(ctx.exception.__cause__))
                self.assertEqual((self.page.clicks, await self.status(approval_id)), (1, "executed"))

    async def test_a_tool_call_approval_may_run_several_operations(self) -> None:
        # harness.start_convergence runs a whole attempt loop under one approval.
        decision, _ = await self.approved_click()
        approval = await self.manager.approvals.create_or_reuse_pending(
            session_id=SESSION,
            kind="write",
            reason="Approve harness.start_convergence with {}",
            action=decision.model_copy(update={"action": "request_human_takeover", "text": "harness.x sha256:0"}),
            origin="tool_call",
        )
        await self.manager.approve(approval.id)

        async with held_for_execution(self.manager.approvals, approval.id):
            await self.manager.click(SESSION, selector="#pay")
            await self.manager.click(SESSION, selector="#pay")

        self.assertEqual((self.page.clicks, await self.status(approval.id)), (2, "executed"))


class GiveBackRecordTests(SpendBase):
    """A give-back stands once its record is written: what goes wrong recording it does not undo it."""

    SIGN_IN = BrowserActionDecision(
        action="type",
        reason="sign in",
        selector="#password",
        text="hunter2",
        sensitive=True,
        risk_category="account_change",
    )

    async def refused_while_the_give_back_audit(self, raises: BaseException) -> str:
        approval_id = await self.approved(self.SIGN_IN)
        append = self.manager.audit.append

        async def failing_append(**event: Any) -> Any:
            if event.get("event_type") == "approval_given_back":
                raise raises
            return await append(**event)

        self.manager.audit.append = failing_append  # type: ignore[method-assign]
        self.page.url = OFF_LIST_URL  # refused before the typing begins
        return approval_id

    async def assert_still_approved_with_its_text(self, approval_id: str) -> None:
        approval = await self.manager.approvals.get(approval_id)
        self.assertEqual(approval.status, "approved")
        self.assertEqual(self.manager.approvals.executable_action(approval).text, "hunter2")

    async def test_an_audit_that_fails_is_logged_and_the_refusal_still_reaches_the_agent(self) -> None:
        approval_id = await self.refused_while_the_give_back_audit(OSError("disk full"))

        with self.assertLogs("app.approvals", level="ERROR"), self.assertRaises(BrowserActionError) as ctx:
            await self.manager.execute_decision(SESSION, self.SIGN_IN, approval_id=approval_id)

        self.assertEqual(ctx.exception.payload["approval_given_back"]["status"], "approved")
        await self.assert_still_approved_with_its_text(approval_id)

    async def test_a_cancellation_while_it_is_audited_keeps_the_sensitive_text(self) -> None:
        approval_id = await self.refused_while_the_give_back_audit(asyncio.CancelledError())

        with self.assertRaises(asyncio.CancelledError):
            await self.manager.execute_decision(SESSION, self.SIGN_IN, approval_id=approval_id)

        await self.assert_still_approved_with_its_text(approval_id)


class GiveBackScopeTests(SpendBase):
    """Only a hold around exactly one action call gives its approval back: nothing else in it is watched."""

    async def test_a_handler_that_changes_the_page_and_then_runs_a_refused_action_stays_spent(self) -> None:
        spec = self.gateway._registry.get("browser.eval_js")
        self.assertIsNotNone(spec)
        original = spec.handler
        self.addCleanup(setattr, spec, "handler", original)

        async def handler(payload: Any) -> dict[str, Any]:
            await self.page.evaluate("document.forms[0].submit()")  # a change outside the pipeline
            self.page.url = OFF_LIST_URL
            return await self.manager.wait(SESSION, 0)  # then refused before its own effect

        spec.handler = handler
        ask = await self.call("browser.eval_js", session_id=SESSION, expression="1")
        approval_id = ask.structuredContent["approval_id"]
        await self.manager.approve(approval_id)

        refused = await self.call("browser.eval_js", session_id=SESSION, expression="1", approval_id=approval_id)

        self.assertEqual(refused.structuredContent.get("code"), "browser_action_blocked", refused.content[0].text)
        self.assertEqual(self.page.evaluated, ["document.forms[0].submit()"])
        self.assertEqual(await self.status(approval_id), "executed")

    async def test_a_governed_execute_action_refused_before_its_effect_is_given_back(self) -> None:
        action = {"action": "click", "reason": "save", "selector": "#save", "risk_category": "write"}
        approval_id = await self.approved_governed(action)
        arguments = {"session_id": SESSION, "workflow_profile": "governed", "action": action}
        self.page.url = OFF_LIST_URL

        refused = await self.call("browser.execute_action", **arguments, approval_id=approval_id)

        self.assertEqual(refused.structuredContent.get("code"), "browser_action_blocked", refused.content[0].text)
        self.assertEqual((self.page.clicks, await self.status(approval_id)), (0, "approved"))
        self.page.url = START_URL
        done = await self.call("browser.execute_action", **arguments, approval_id=approval_id)
        self.assertFalse(done.isError, done.content[0].text)
        self.assertEqual((self.page.clicks, await self.status(approval_id)), (1, "executed"))

    async def test_a_governed_orchestrator_step_refused_before_its_effect_is_given_back(self) -> None:
        decision = BrowserActionDecision(action="click", reason="save", selector="#save", risk_category="write")
        with self.assertRaises(ApprovalRequiredError) as ctx:
            await self.manager.require_governed_approval(SESSION, decision, approval_id=None)
        approval_id = ctx.exception.approval.id
        await self.manager.approve(approval_id)
        orchestrator = BrowserOrchestrator(self.manager, SimpleNamespace())

        async def step():
            return await orchestrator._execute_decision(
                session_id=SESSION,
                goal="save",
                observation={},
                provider_decision=ProviderDecision(provider="openai", model="m", decision=decision),
                upload_approved=False,
                approval_id=approval_id,
                workflow_profile="governed",
            )

        self.page.url = OFF_LIST_URL
        with self.assertRaises(BrowserActionError):
            await step()
        self.assertEqual((self.page.clicks, await self.status(approval_id)), (0, "approved"))

        self.page.url = START_URL
        self.assertEqual((await step()).status, "acted")
        self.assertEqual((self.page.clicks, await self.status(approval_id)), (1, "executed"))

    async def test_an_upload_refused_before_the_transfer_is_given_back(self) -> None:
        with self.assertRaises(ApprovalRequiredError) as ctx:
            await self.manager.upload(SESSION, selector="#file", file_path="demo.txt", approved=False)
        approval_id = ctx.exception.approval.id
        await self.manager.approve(approval_id)
        upload = {"selector": "#file", "file_path": "demo.txt", "approved": False, "approval_id": approval_id}

        self.page.url = OFF_LIST_URL
        with self.assertRaises(BrowserActionError):
            await self.manager.upload(SESSION, **upload)
        self.assertEqual((self.page.uploads, await self.status(approval_id)), ([], "approved"))

        self.page.url = START_URL
        await self.manager.upload(SESSION, **upload)
        self.assertEqual((len(self.page.uploads), await self.status(approval_id)), (1, "executed"))


class InnerRefusalTests(SpendBase):
    """An approval check that refuses inside a hold, before anything ran, gives the outer approval back."""

    PAY = {"action": "click", "reason": "pay", "selector": "#pay", "risk_category": "payment"}

    async def governed_pay(self, **ids: str):
        arguments = {"session_id": SESSION, "workflow_profile": "governed", "action": self.PAY}
        return await self.call("browser.execute_action", **arguments, **ids)

    async def test_an_inner_approval_still_to_ask_for_leaves_the_governed_approval_approved(self) -> None:
        governed = await self.approved_governed(self.PAY)

        asked = await self.governed_pay(governed_approval_id=governed)

        self.assertEqual(asked.structuredContent.get("status"), "approval_required", asked.content[0].text)
        inner = asked.structuredContent["approval_id"]
        self.assertNotEqual(inner, governed)
        self.assertEqual((self.page.clicks, await self.status(governed)), (0, "approved"))
        await self.manager.approve(inner)
        done = await self.governed_pay(governed_approval_id=governed, approval_id=inner)
        self.assertFalse(done.isError, done.content[0].text)
        self.assertEqual(
            (self.page.clicks, await self.status(governed), await self.status(inner)), (1, "executed", "executed")
        )

    async def test_an_inner_approval_that_is_refused_leaves_the_governed_approval_approved(self) -> None:
        governed = await self.approved_governed(self.PAY)
        undecided = (await self.governed_pay(governed_approval_id=governed)).structuredContent["approval_id"]

        refused = await self.governed_pay(governed_approval_id=governed, approval_id=undecided)

        self.assertEqual(refused.structuredContent.get("code"), "not_permitted", refused.content[0].text)
        self.assertEqual((self.page.clicks, await self.status(governed)), (0, "approved"))

    async def test_an_inner_claim_that_is_refused_leaves_the_outer_approval_approved(self) -> None:
        decision, outer = await self.approved_click()
        undecided = await self.manager.approvals.create_or_reuse_pending(
            session_id=SESSION, kind="write", reason="r", action=decision.model_copy(update={"selector": "#other"})
        )

        with self.assertRaises(PermissionError):
            async with held_for_execution(self.manager.approvals, outer, may_give_back=True):
                async with held_for_execution(self.manager.approvals, undecided.id, may_give_back=True):
                    self.fail("the inner claim of an undecided approval went through")

        self.assertEqual(await self.status(outer), "approved")


class RejectDuringClaimTests(SpendBase):
    """An operator who rejects a claimed approval before its action starts is not told it already ran."""

    async def claimed_and_waiting(self) -> tuple[Any, str, asyncio.Event, asyncio.Task]:
        decision, approval_id = await self.approved_click()
        reached, go = asyncio.Event(), asyncio.Event()

        async def snapshot(*_: Any, **__: Any) -> dict[str, Any]:
            if not go.is_set():
                reached.set()
                await go.wait()
            return {"url": self.page.url}

        self.manager._light_snapshot = snapshot  # type: ignore[method-assign]
        task = asyncio.create_task(self.manager.execute_decision(SESSION, decision, approval_id=approval_id))
        await asyncio.wait_for(reached.wait(), WAIT_SECONDS)
        self.assertEqual(await self.status(approval_id), "executed")  # claimed, the click not yet begun
        return decision, approval_id, go, task

    async def test_a_reject_while_the_claim_waits_applies_when_the_action_does_not_start(self) -> None:
        decision, approval_id, go, task = await self.claimed_and_waiting()

        answer = await self.manager.reject(approval_id, comment="changed my mind")

        self.assertEqual(answer["status"], "executed")
        self.assertIn("the rejection stops the action unless it has begun", answer["message"])
        self.page.url = OFF_LIST_URL  # the click is then refused before it begins
        go.set()
        with self.assertRaises(BrowserActionError) as ctx:
            await task
        self.assertEqual(ctx.exception.payload["approval_given_back"]["status"], "rejected")
        approval = await self.manager.approvals.get(approval_id)
        self.assertEqual(
            (approval.status, approval.decision_comment, approval.decided_via, approval.executed_at),
            ("rejected", "changed my mind", "operator", None),
        )
        self.page.url = START_URL
        with self.assertRaisesRegex(PermissionError, "is not approved"):
            await self.manager.execute_decision(SESSION, decision, approval_id=approval_id)
        self.assertEqual(self.page.clicks, 0)

    async def until_claimed(self, approval_id: str) -> None:
        async def claimed() -> None:
            while approval_id not in self.manager.approvals._claims:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(claimed(), WAIT_SECONDS)

    async def rest_decision(self, approval_id: str, decision: str) -> httpx.Response:
        app = FastAPI()
        app.include_router(create_operations_router(manager=self.manager, proxy_store=None, cron_service=None))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post(f"/approvals/{approval_id}/{decision}", json={"comment": "stop"})

    async def test_a_reject_while_the_claim_waits_for_the_session_stops_the_action(self) -> None:
        decision, approval_id = await self.approved_click()

        async with self.session.lock:  # another action is running in the session
            task = asyncio.create_task(self.manager.execute_decision(SESSION, decision, approval_id=approval_id))
            await self.until_claimed(approval_id)
            answer = await self.manager.reject(approval_id, comment="not this one")

        with self.assertRaises(BrowserActionError):
            await task
        self.assertIn("the rejection stops the action unless it has begun", answer["message"])
        approval = await self.manager.approvals.get(approval_id)
        self.assertEqual(
            (self.page.clicks, approval.status, approval.decision_comment), (0, "rejected", "not this one")
        )

    async def test_a_reject_during_a_running_tool_call_is_refused_with_why(self) -> None:
        ask = await self.call("browser.eval_js", session_id=SESSION, expression="pay()")
        approval_id = ask.structuredContent["approval_id"]
        await self.manager.approve(approval_id)
        entered, go = asyncio.Event(), asyncio.Event()
        evaluate = self.page.evaluate

        async def running_evaluate(expression: str, arg: Any = None) -> Any:
            if expression == "pay()":
                entered.set()  # the script is running in the page
                await go.wait()
            return await evaluate(expression, arg)

        self.page.evaluate = running_evaluate  # type: ignore[method-assign]
        call = asyncio.create_task(
            self.call("browser.eval_js", session_id=SESSION, expression="pay()", approval_id=approval_id)
        )
        await asyncio.wait_for(entered.wait(), WAIT_SECONDS)
        try:
            with self.assertRaises(PermissionError) as refused:
                await self.manager.reject(approval_id, comment="stop")
            rest = await self.rest_decision(approval_id, "reject")
            mcp = await self.call("browser.reject_approval", approval_id=approval_id, comment="stop")
        finally:
            go.set()
            done = await call

        self.assertIn("the tool call that is running", str(refused.exception))
        self.assertEqual((rest.status_code, rest.json()), (409, {"detail": str(refused.exception)}))
        self.assertEqual(mcp.structuredContent.get("error"), str(refused.exception), mcp.content[0].text)
        self.assertFalse(done.isError, done.content[0].text)
        self.assertEqual(await self.status(approval_id), "executed")
        events = await self.manager.list_audit_events(limit=50, event_type="approval_decision")
        self.assertNotIn("deferred", [event["status"] for event in events])

    async def test_a_deferred_reject_is_audited_as_deferred(self) -> None:
        _, approval_id, go, task = await self.claimed_and_waiting()

        await self.manager.reject(approval_id, comment="hold on")
        go.set()
        with self.assertRaises(BrowserActionError):  # the rejection stops the action
            await task

        events = await self.manager.list_audit_events(limit=50, event_type="approval_decision")
        self.assertIn(
            ("deferred", "reject", approval_id, "hold on", "action"),
            [
                (
                    event["status"],
                    event["action"],
                    event["approval_id"],
                    event["details"].get("comment"),
                    event["details"].get("origin"),
                )
                for event in events
            ],
        )

    async def test_an_approve_withdraws_a_deferred_reject(self) -> None:
        _, approval_id, go, task = await self.claimed_and_waiting()
        await self.manager.reject(approval_id, comment="wait")

        answer = await self.manager.approve(approval_id, comment="go ahead after all")
        go.set()
        await task

        self.assertIn("withdrawn", answer["message"])
        self.assertEqual((self.page.clicks, await self.status(approval_id)), (1, "executed"))
        # The trail says the operator withdrew their reject, not that they approved anew.
        events = await self.manager.list_audit_events(limit=50, event_type="approval_decision")
        self.assertEqual(
            [(event["approval_id"], event["details"]["comment"]) for event in events if event["status"] == "withdrawn"],
            [(approval_id, "go ahead after all")],
        )

    async def test_an_approve_of_a_spent_approval_answers_why_over_rest(self) -> None:
        decision, approval_id = await self.approved_click()
        await self.manager.execute_decision(SESSION, decision, approval_id=approval_id)

        with self.assertRaises(PermissionError) as refused:
            await self.manager.approve(approval_id, comment="again")
        rest = await self.rest_decision(approval_id, "approve")

        self.assertIn("has already been spent: its action started", str(refused.exception))
        self.assertEqual((rest.status_code, rest.json()), (409, {"detail": str(refused.exception)}))

    async def test_a_rejection_waiting_on_an_action_cancelled_before_it_began_applies(self) -> None:
        # Round 4: a cancellation before the action begins gives the approval back, so the reject
        # applies, as its answer said. It used to stay executed, audited as a rejection not applied.
        _, approval_id, _, task = await self.claimed_and_waiting()
        await self.manager.reject(approval_id, comment="stop")

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        approval = await self.manager.approvals.get(approval_id)
        self.assertEqual((approval.status, approval.decision_comment), ("rejected", "stop"))
        events = await self.manager.list_audit_events(limit=50, event_type="approval_decision")
        self.assertNotIn("not_applied", [event["status"] for event in events])

    async def test_a_rejection_that_stops_the_action_shows_in_its_receipts_and_audit(self) -> None:
        _, approval_id, go, task = await self.claimed_and_waiting()
        await self.manager.reject(approval_id, comment="not this one")

        go.set()
        with self.assertRaises(BrowserActionError):
            await task

        blocked = (await self.receipt())["approval"]
        self.assertEqual((blocked["approval_id"], blocked["status"]), (approval_id, "rejected"))
        rejections = [
            (
                item["status"],
                item["approval"]["status"],
                item["metadata"].get("comment"),
                item["metadata"].get("decided_via"),
            )
            for item in await self.manager.list_witness_receipts(SESSION)
            if item["event_type"] == "approval"
            and item["action"] == "reject"
            and item["approval"]["approval_id"] == approval_id
        ]
        self.assertEqual(rejections, [("rejected", "rejected", "not this one", "operator")])
        given_back = await self.manager.list_audit_events(limit=50, event_type="approval_given_back")
        self.assertEqual(
            [
                (event["status"], event["details"].get("comment"), event["details"].get("decided_via"))
                for event in given_back
            ],
            [("rejected", "not this one", "operator")],
        )

    async def test_an_agent_still_cannot_reject_a_claimed_approval(self) -> None:
        _, approval_id, go, task = await self.claimed_and_waiting()
        try:
            with self.assertRaisesRegex(PermissionError, "already been"):
                await self.manager.reject(approval_id, comment="no", decided_via="agent")
        finally:
            self.page.url = OFF_LIST_URL
            go.set()
            with self.assertRaises(BrowserActionError):
                await task
        self.assertEqual(await self.status(approval_id), "approved")


class PolicyBlockRollbackTests(SpendBase):
    """A refusal before the operation began has nothing of the action's to roll back."""

    async def test_a_block_before_the_operation_does_not_navigate_back(self) -> None:
        decision = BrowserActionDecision(action="go_back", reason="back to the cart", risk_category="account_change")
        approval_id = await self.approved(decision)
        go_backs: list[str] = []

        async def counted_go_back(**_: object) -> None:
            go_backs.append(self.page.url)
            self.page.url = START_URL

        async def snapshot(*_: Any, **__: Any) -> dict[str, Any]:
            seen = self.page.url
            self.page.url = "https://example.com/next"  # the page moves itself just after it was read
            return {"url": seen}

        self.page.go_back = counted_go_back  # type: ignore[method-assign]
        self.manager._light_snapshot = snapshot  # type: ignore[method-assign]
        evaluate = self.manager.witness_policy.evaluate_action
        self.manager.witness_policy.evaluate_action = Mock(  # type: ignore[method-assign]
            return_value=WitnessPolicyOutcome(profile="normal", should_block=True, block_reason="not here")
        )

        with self.assertRaises(BrowserActionError):
            await self.manager.execute_decision(SESSION, decision, approval_id=approval_id)

        self.assertEqual((go_backs, await self.status(approval_id)), ([], "approved"))
        self.manager.witness_policy.evaluate_action = evaluate  # type: ignore[method-assign]
        await self.manager.execute_decision(SESSION, decision, approval_id=approval_id)
        self.assertEqual((len(go_backs), await self.status(approval_id)), (1, "executed"))

    async def test_a_block_after_the_operation_still_rolls_its_navigation_back(self) -> None:
        decision, approval_id = await self.approved_click()
        self.page.click_navigates_to = OFF_LIST_URL
        go_backs: list[str] = []

        async def counted_go_back(**_: object) -> None:
            go_backs.append(self.page.url)
            self.page.url = START_URL

        self.page.go_back = counted_go_back  # type: ignore[method-assign]

        with self.assertRaises(BrowserActionError):
            await self.manager.execute_decision(SESSION, decision, approval_id=approval_id)

        self.assertEqual((go_backs, self.page.url), ([OFF_LIST_URL], START_URL))


class MessageTests(SpendBase):
    """What the agent is told about its approval: spent and how to ask again, or still approved."""

    async def test_a_spent_approval_says_when_it_was_spent_and_how_to_ask_again(self) -> None:
        decision, approval_id = await self.approved_click()
        await self.manager.execute_decision(SESSION, decision, approval_id=approval_id)
        executed_at = (await self.manager.approvals.get(approval_id)).executed_at
        expected = (
            f"approval {approval_id} was spent when its action started (executed_at {executed_at}); "
            "repeat the call without approval_id to ask for a new approval"
        )

        with self.assertRaises(PermissionError) as check:
            await self.manager.execute_decision(SESSION, decision, approval_id=approval_id)
        with self.assertRaises(PermissionError) as claim:
            await self.manager.approvals.claim_execution(approval_id)

        self.assertEqual((str(check.exception), str(claim.exception)), (expected, expected))
        self.assertEqual(self.page.clicks, 1)

    async def test_a_refusal_that_gave_the_approval_back_says_it_is_still_approved(self) -> None:
        action = {"action": "click", "reason": "save", "selector": "#save", "risk_category": "write"}
        approval_id = await self.approved_governed(action)
        self.page.url = OFF_LIST_URL

        refused = await self.call(
            "browser.execute_action",
            session_id=SESSION,
            workflow_profile="governed",
            action=action,
            approval_id=approval_id,
        )

        note = refused.structuredContent["approval_given_back"]
        self.assertEqual((note["approval_id"], note["status"]), (approval_id, "approved"), refused.content[0].text)
        self.assertIn("Retry with this approval_id", note["message"])

    async def test_an_approval_to_ask_for_inside_a_governed_call_says_the_governed_one_is_still_approved(self) -> None:
        action = {"action": "click", "reason": "pay", "selector": "#pay", "risk_category": "payment"}
        governed = await self.approved_governed(action)

        asked = await self.call(
            "browser.execute_action",
            session_id=SESSION,
            workflow_profile="governed",
            action=action,
            governed_approval_id=governed,
        )

        self.assertEqual(asked.structuredContent.get("status"), "approval_required", asked.content[0].text)
        self.assertEqual(asked.structuredContent["approval_given_back"]["approval_id"], governed)


class GovernedUploadFileTests(SpendBase):
    """A governed upload's file is checked before anyone is asked to approve it, or the approval is claimed."""

    UPLOAD = {"action": "upload", "reason": "attach", "selector": "#file", "file_path": "later.txt"}

    def stage(self) -> None:
        (self.session.upload_dir / "later.txt").write_text("later", encoding="utf-8")

    async def test_the_gateway_asks_for_no_approval_of_a_missing_file(self) -> None:
        arguments = {"session_id": SESSION, "workflow_profile": "governed", "action": self.UPLOAD}

        missing = await self.call("browser.execute_action", **arguments)

        self.assertEqual(missing.structuredContent.get("code"), "not_found", missing.content[0].text)
        self.assertEqual(await self.manager.approvals.list(), [])
        self.stage()
        approval_id = await self.approved_governed(self.UPLOAD)
        done = await self.call("browser.execute_action", **arguments, approval_id=approval_id)
        self.assertFalse(done.isError, done.content[0].text)
        self.assertEqual((len(self.page.uploads), await self.status(approval_id)), (1, "executed"))

    async def test_an_approved_governed_upload_whose_file_went_missing_keeps_its_approval(self) -> None:
        self.stage()
        approval_id = await self.approved_governed(self.UPLOAD)
        (self.session.upload_dir / "later.txt").unlink()
        arguments = {"session_id": SESSION, "workflow_profile": "governed", "action": self.UPLOAD}

        missing = await self.call("browser.execute_action", **arguments, approval_id=approval_id)

        self.assertEqual(missing.structuredContent.get("code"), "not_found", missing.content[0].text)
        self.assertEqual(await self.status(approval_id), "approved")

    async def test_the_orchestrator_asks_for_no_approval_of_a_missing_file(self) -> None:
        orchestrator = BrowserOrchestrator(self.manager, SimpleNamespace())
        decision = BrowserActionDecision(**self.UPLOAD)

        with self.assertRaises(FileNotFoundError):
            await orchestrator._execute_decision(
                session_id=SESSION,
                goal="attach",
                observation={},
                provider_decision=ProviderDecision(provider="openai", model="m", decision=decision),
                upload_approved=False,
                approval_id=None,
                workflow_profile="governed",
            )

        self.assertEqual(await self.manager.approvals.list(), [])

    async def test_the_orchestrator_finds_a_file_in_the_sessions_own_directory(self) -> None:
        self.stage()  # in the session's upload directory, not the shared root
        orchestrator = BrowserOrchestrator(self.manager, SimpleNamespace())
        decision = BrowserActionDecision(**self.UPLOAD)

        with self.assertRaises(ApprovalRequiredError):
            await orchestrator._execute_decision(
                session_id=SESSION,
                goal="attach",
                observation={},
                provider_decision=ProviderDecision(provider="openai", model="m", decision=decision),
                upload_approved=False,
                approval_id=None,
                workflow_profile="governed",
            )

        self.assertEqual([approval.action.file_path for approval in await self.manager.approvals.list()], ["later.txt"])


class ReceiptStatusTests(SpendBase):
    """A receipt reports an approval's status only for one the action ran under; never a guess."""

    PLAIN = BrowserActionDecision(action="click", reason="open", selector="#open")  # needs no approval

    async def run_plain(self, approval_id: str, *, fails: bool) -> dict[str, Any]:
        self.page.url = OFF_LIST_URL if fails else START_URL
        if fails:
            with self.assertRaises(BrowserActionError):
                await self.manager.execute_decision(SESSION, self.PLAIN, approval_id=approval_id)
        else:
            await self.manager.execute_decision(SESSION, self.PLAIN, approval_id=approval_id)
        return (await self.receipt())["approval"]

    async def test_an_id_nothing_claimed_has_no_status_in_the_receipt(self) -> None:
        decision, spent = await self.approved_click()
        await self.manager.execute_decision(SESSION, decision, approval_id=spent)
        rejected = await self.manager.approvals.create_or_reuse_pending(
            session_id=SESSION, kind="payment", reason="r", action=decision.model_copy(update={"selector": "#other"})
        )
        await self.manager.reject(rejected.id)
        for name, approval_id in (("unused", "000000000000"), ("spent", spent), ("rejected", rejected.id)):
            for fails in (False, True):
                with self.subTest(approval=name, fails=fails):
                    approval = await self.run_plain(approval_id, fails=fails)
                    self.assertEqual((approval["approval_id"], approval["status"]), (approval_id, None))

    async def test_a_give_back_is_audited(self) -> None:
        decision, approval_id = await self.approved_click()
        self.page.url = OFF_LIST_URL

        with self.assertRaises(BrowserActionError):
            await self.manager.execute_decision(SESSION, decision, approval_id=approval_id)

        events = await self.manager.list_audit_events(limit=50, event_type="approval_given_back")
        self.assertEqual(
            [(event["approval_id"], event["status"], event["details"].get("origin")) for event in events],
            [(approval_id, "approved", "action")],
        )

    async def test_a_governed_approval_id_call_names_its_approval_in_the_receipt(self) -> None:
        action = {"action": "click", "reason": "save", "selector": "#save", "risk_category": "write"}
        governed = await self.approved_governed(action)

        done = await self.call(
            "browser.execute_action",
            session_id=SESSION,
            workflow_profile="governed",
            action=action,
            governed_approval_id=governed,
        )

        self.assertFalse(done.isError, done.content[0].text)
        approval = (await self.receipt())["approval"]
        self.assertEqual(
            (approval["required"], approval["approval_id"], approval["status"]), (True, governed, "executed")
        )

    async def test_only_a_live_action_hold_names_its_approval_for_the_receipt(self) -> None:
        decision, approval_id = await self.approved_click()
        tool_call = await self.manager.approvals.create_or_reuse_pending(
            session_id=SESSION,
            kind="write",
            reason="Approve harness.start_convergence with {}",
            action=decision.model_copy(update={"action": "request_human_takeover", "text": "harness.x sha256:0"}),
            origin="tool_call",
        )
        await self.manager.approve(tool_call.id)
        go = asyncio.Event()

        async def once_released() -> str | None:
            await go.wait()
            return held_action_approval()

        async with held_for_execution(self.manager.approvals, approval_id):
            inside = held_action_approval()
            child = asyncio.create_task(once_released())
        async with held_for_execution(self.manager.approvals, tool_call.id):
            under_a_tool_call = held_action_approval()
        go.set()

        self.assertEqual((inside, await child, under_a_tool_call), (approval_id, None, None))
