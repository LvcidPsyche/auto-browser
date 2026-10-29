"""A governed tool call's approval is minted by the gateway alone, and spent by that tool call alone.

The gateway approves a governed tool call that is not a browser action (eval_js,
set_cookies, ...) through a stand-in action: request_human_takeover whose text is
"<tool> sha256:<digest of the arguments>". Matching compared field values only, so
an agent could ask for that same stand-in through browser.execute_action, with a
reason of its own choosing: the dashboard showed "tool call browser.eval_js
(arguments under Reason)" over a benign expression, and the approval then ran
another one (MCP-01). POST /approvals/{id}/execute on a stand-in answered a 400
"Unsupported action" (MCP-38).

These go through McpToolGateway.call_tool over a real BrowserManager and
ApprovalStore; only the page is a fake that records what it evaluates.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI

from app import events
from app.action_errors import BrowserActionError
from app.approvals import ApprovalRequiredError, ApprovalStore
from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.models import BrowserActionDecision, McpToolCallRequest
from app.orchestrator import BrowserOrchestrator
from app.providers.base import ProviderDecision
from app.routes.operations import create_operations_router
from app.routes.sessions import create_sessions_router
from app.tool_gateway.gateway import McpToolGateway
from app.tool_inputs import EvalJsInput
from app.utils import UTC

SESSION = "session-1"
BENIGN = "() => document.title"
MALICIOUS = "() => document.cookie"
ATTACKER_COOKIES = [{"name": "sid", "value": "attacker", "url": "https://example.com"}]
TAKEOVER_REFUSED = "request_human_takeover is not an action to execute; use browser.request_human_takeover"


class FakePage:
    def __init__(self) -> None:
        self.url = "https://example.com"
        self.evaluated: list[str] = []

    async def title(self) -> str:
        return "Example Domain"

    async def evaluate(self, expression: str) -> Any:
        self.evaluated.append(expression)
        return "executed"


class FakeContext:
    def __init__(self) -> None:
        self.cookies: list[dict[str, Any]] = []

    async def add_cookies(self, cookies: list[dict[str, Any]]) -> None:
        self.cookies.extend(cookies)


def stand_in_text(tool: str, args: dict[str, Any]) -> str:
    """What the gateway puts in a stand-in's `text`, computed independently, as a forger would."""
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"{tool} sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


class ApprovalBindingBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        settings = Settings(_env_file=None)
        for name in ("artifact", "upload", "auth", "approval", "session_store", "audit"):
            setattr(settings, f"{name}_root", str(root / name))
        self.manager = BrowserManager(settings)
        artifact_dir = Path(settings.artifact_root) / SESSION
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.page = FakePage()
        self.context = FakeContext()
        session = BrowserSession(
            id=SESSION,
            name=SESSION,
            created_at=datetime.now(UTC),
            context=self.context,  # type: ignore[arg-type]
            page=self.page,  # type: ignore[arg-type]
            artifact_dir=artifact_dir,
            auth_dir=Path(settings.auth_root) / SESSION,
            upload_dir=Path(settings.upload_root) / SESSION,
            takeover_url="http://127.0.0.1:6080/vnc.html",
            trace_path=artifact_dir / "trace.zip",
        )
        session.auth_dir.mkdir(parents=True, exist_ok=True)
        session.upload_dir.mkdir(parents=True, exist_ok=True)
        self.manager.sessions[SESSION] = session
        self.gateway = McpToolGateway(
            manager=self.manager,
            orchestrator=object(),
            job_queue=object(),
            tool_profile="full",
        )

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def call(self, name: str, **arguments: Any):
        return await self.gateway.call_tool(McpToolCallRequest(name=name, arguments=arguments))

    async def forged_pending(self, tool: str, args: dict[str, Any], *, kind: str):
        """A pending takeover shaped like the gateway's stand-in, minted as an action approval.

        browser.execute_action no longer queues one; this is what one asked for
        before the fix (or stored by an older release) looks like.
        """
        forged = BrowserActionDecision(
            action="request_human_takeover",
            reason=f"Approve {tool} with harmless arguments",
            risk_category=kind,
            text=stand_in_text(tool, args),
        )
        return await self.manager.approvals.create_or_reuse_pending(
            session_id=SESSION, kind=kind, reason=forged.reason, action=forged
        )

    async def forged_approval(self, tool: str, args: dict[str, Any], *, kind: str) -> str:
        approval = await self.forged_pending(tool, args, kind=kind)
        await self.manager.approve(approval.id, comment="looks fine")
        return approval.id

    async def approved_eval_js(self, expression: str = BENIGN) -> str:
        first = await self.call("browser.eval_js", session_id=SESSION, expression=expression)
        self.assertTrue(first.isError)
        approval_id = first.structuredContent["approval"]["id"]
        await self.manager.approve(approval_id)
        return approval_id


class ForgedStandInTests(ApprovalBindingBase):
    async def test_mcp01_forged_takeover_approval_is_not_spendable_on_eval_js(self) -> None:
        # Agent: ask for an approval via browser.execute_action. The action is a
        # request_human_takeover whose text is the stand-in for MALICIOUS eval_js
        # arguments, while the reason shows the operator a benign expression.
        forged_text = stand_in_text("browser.eval_js", {"expression": MALICIOUS})
        ask = await self.call(
            "browser.execute_action",
            session_id=SESSION,
            workflow_profile="governed",
            action={
                "action": "request_human_takeover",
                "reason": f'Approve browser.eval_js with {{"expression": "{BENIGN}"}}',
                "text": forged_text,
            },
        )

        self.assertTrue(ask.isError)
        # Nothing reaches the operator's queue: there is no approval to deceive them with.
        self.assertEqual([approval.id for approval in await self.manager.approvals.list()], [], ask.content[0].text)
        self.assertEqual(self.page.evaluated, [])

    async def test_a_forged_action_approval_is_not_spendable_on_eval_js(self) -> None:
        approval_id = await self.forged_approval("browser.eval_js", {"expression": MALICIOUS}, kind="write")

        spend = await self.call("browser.eval_js", session_id=SESSION, expression=MALICIOUS, approval_id=approval_id)

        self.assertEqual(
            self.page.evaluated,
            [],
            "the page ran an expression the operator never saw, on an approval minted as a browser action",
        )
        self.assertTrue(spend.isError)
        self.assertEqual((await self.manager.approvals.get(approval_id)).status, "approved")

    async def test_a_forged_action_approval_is_not_spendable_on_set_cookies(self) -> None:
        # The same trick on another tool: the forger controls the text, so a
        # stand-in for any governed tool can be minted, not only eval_js.
        approval_id = await self.forged_approval(
            "browser.set_cookies", {"cookies": ATTACKER_COOKIES}, kind="account_change"
        )

        spend = await self.call(
            "browser.set_cookies",
            session_id=SESSION,
            workflow_profile="governed",
            cookies=ATTACKER_COOKIES,
            approval_id=approval_id,
        )

        self.assertEqual(self.context.cookies, [], "cookies were set on an approval minted as a browser action")
        self.assertTrue(spend.isError)
        self.assertIn("is for a browser action, not a tool call", spend.content[0].text)

    async def test_a_takeover_is_refused_as_an_action_before_any_approval(self) -> None:
        takeover = BrowserActionDecision(
            action="request_human_takeover",
            reason="look at this",
            risk_category="payment",
            text=stand_in_text("browser.eval_js", {"expression": MALICIOUS}),
        )
        # A takeover the runtime would not ask about (risk "write") is refused by execute_decision itself.
        unapproved = takeover.model_copy(update={"risk_category": "write"})
        calls = {
            "execute_decision": lambda: self.manager.execute_decision(SESSION, takeover),
            "execute_decision without approval": lambda: self.manager.execute_decision(SESSION, unapproved),
            "require_governed_approval": lambda: self.manager.require_governed_approval(
                SESSION, takeover, approval_id=None
            ),
        }
        for name, call in calls.items():
            with self.subTest(path=name), self.assertRaisesRegex(ValueError, TAKEOVER_REFUSED):
                await call()
        self.assertEqual(await self.manager.approvals.list(), [])

    async def test_done_is_refused_as_an_action_before_any_approval(self) -> None:
        # Nothing executes `done` either: queued, it only put agent-written text before an operator.
        done = BrowserActionDecision(
            action="done",
            reason='Approve browser.set_cookies with {"cookies": "<redacted>"}',
            risk_category="payment",
            text=stand_in_text("browser.set_cookies", {"cookies": []}),
        )
        calls = {
            "execute_decision": lambda: self.manager.execute_decision(SESSION, done),
            "execute_decision without approval": lambda: self.manager.execute_decision(
                SESSION, done.model_copy(update={"risk_category": "write"})
            ),
            "require_governed_approval": lambda: self.manager.require_governed_approval(
                SESSION, done, approval_id=None
            ),
            # Kinds that need no approval: the check returned before the refusal.
            "require_governed_approval, risk read": lambda: self.manager.require_governed_approval(
                SESSION, done.model_copy(update={"risk_category": "read"}), approval_id=None
            ),
            "require_decision_approval, no kind": lambda: self.manager.actions.require_decision_approval(
                SESSION, done.model_copy(update={"risk_category": "write"}), approval_id=None
            ),
        }
        for name, call in calls.items():
            with self.subTest(path=name), self.assertRaisesRegex(ValueError, "done is not an action to execute"):
                await call()
        self.assertEqual(await self.manager.approvals.list(), [])

    async def test_an_approval_for_done_is_refused_by_execute_approval_before_its_claim(self) -> None:
        # One queued before this boundary existed.
        done = BrowserActionDecision(action="done", reason="r", risk_category="payment")
        pending = await self.manager.approvals.create_or_reuse_pending(
            session_id=SESSION, kind="payment", reason="r", action=done
        )
        await self.manager.approve(pending.id)

        with self.assertRaisesRegex(PermissionError, "nothing can run it"):
            await self.manager.execute_approval(pending.id)

        self.assertEqual((await self.manager.approvals.get(pending.id)).status, "approved")

    async def test_a_genuine_tool_call_approval_runs_its_call_once(self) -> None:
        approval_id = await self.approved_eval_js()
        self.assertEqual((await self.manager.approvals.get(approval_id)).origin, "tool_call")

        spend = await self.call("browser.eval_js", session_id=SESSION, expression=BENIGN, approval_id=approval_id)
        replay = await self.call("browser.eval_js", session_id=SESSION, expression=BENIGN, approval_id=approval_id)

        self.assertFalse(spend.isError, spend.content[0].text)
        self.assertTrue(replay.isError)
        self.assertEqual(self.page.evaluated, [BENIGN])
        self.assertEqual((await self.manager.approvals.get(approval_id)).status, "executed")

    async def test_a_genuine_set_cookies_approval_still_sets_them(self) -> None:
        arguments = {"session_id": SESSION, "workflow_profile": "governed", "cookies": ATTACKER_COOKIES}
        ask = await self.call("browser.set_cookies", **arguments)
        approval_id = ask.structuredContent["approval_id"]
        await self.manager.approve(approval_id)

        spend = await self.call("browser.set_cookies", **arguments, approval_id=approval_id)

        self.assertFalse(spend.isError, spend.content[0].text)
        self.assertEqual(self.context.cookies, ATTACKER_COOKIES)


class ToolCallPreviewTests(ApprovalBindingBase):
    """The reason an operator reads for a tool call: no hidden reordering, and no silent cut."""

    async def reason_for(self, expression: str) -> str:
        ask = await self.call("browser.eval_js", session_id=SESSION, expression=expression)
        return ask.structuredContent["approval"]["reason"]

    async def test_every_character_past_ascii_is_written_as_an_escape(self) -> None:
        # A bidi override, a zero-width space, a homoglyph host and an invisible Hangul-filler name.
        reason = await self.reason_for("send(/*\u202e*/ 'x')\u200b; fetch('https://\u0430pi.bank.example/?' + \u3164)")

        for escape in ("\\u202e", "\\u200b", "\\u0430", "\\u3164"):
            self.assertIn(escape, reason)
        self.assertTrue(reason.isascii(), reason)

    async def test_a_set_cookies_preview_hides_only_each_cookies_value(self) -> None:
        cookie = {"name": "sid", "value": "attacker-session", "domain": "bank.example", "path": "/", "secure": True}

        ask = await self.call("browser.set_cookies", session_id=SESSION, cookies=[cookie], workflow_profile="governed")

        reason = ask.structuredContent["approval"]["reason"]
        self.assertNotIn("attacker-session", reason)
        for shown in ('"name": "sid"', '"domain": "bank.example"', '"path": "/"', '"secure": true', '"<redacted>"'):
            self.assertIn(shown, reason)

    async def test_a_cut_preview_counts_the_characters_view_all_shows(self) -> None:
        # Round 4 (Q8): it counted what its one-line form left out, beside a View all button counting the
        # indented text, so one call had two totals. It gives the button's now, which the record keeps.
        ask = await self.call("browser.eval_js", session_id=SESSION, expression="x" * 1000)
        approval = ask.structuredContent["approval"]

        note = f"… ({approval['arguments_size']} characters in all: View all shows them)"
        self.assertTrue(approval["reason"].endswith(note), approval["reason"][-80:])


class ApprovalEventTests(ApprovalBindingBase):
    """The SSE approval event says what the approval is for, apart from the reason an agent wrote."""

    async def test_the_event_carries_the_origin_and_a_summary_of_the_action(self) -> None:
        queue = events.subscribe(SESSION)
        self.addCleanup(events.unsubscribe, SESSION, queue)
        click = BrowserActionDecision(
            action="click", reason="Approve browser.eval_js with {}", x=640, y=410, risk_category="payment"
        )

        with self.assertRaises(ApprovalRequiredError):
            await self.manager.execute_decision(SESSION, click)
        await self.call("browser.eval_js", session_id=SESSION, expression=BENIGN)

        payloads = [json.loads(queue.get_nowait()) for _ in range(queue.qsize())]
        approvals = [payload for payload in payloads if payload["event"] == "approval"]
        self.assertEqual(
            [(payload["origin"], payload["action_summary"]) for payload in approvals],
            [("action", "click · x=640 · y=410"), ("tool_call", "tool call browser.eval_js")],
        )


class ApprovalRecordTrailTests(ApprovalBindingBase):
    """Receipts and audit entries say whether an approval was a tool call's, and which tool's."""

    async def trail(self, approval_id: str) -> list[tuple[str, Any, Any]]:
        receipts = await self.manager.list_witness_receipts(SESSION)
        audit = await self.manager.audit.list(limit=50)
        return [
            *[
                (f"receipt {item['action']}", item["target"].get("origin"), item["target"].get("tool"))
                for item in reversed(receipts)
                if item["approval"]["approval_id"] == approval_id
            ],
            *[
                (f"audit {event.action}", event.details.get("origin"), event.details.get("tool"))
                for event in reversed(audit)
                if event.approval_id == approval_id
            ],
        ]

    async def test_a_tool_calls_approval_is_named_in_its_receipts_and_audit(self) -> None:
        approved = await self.approved_eval_js()
        ask = await self.call("browser.eval_js", session_id=SESSION, expression="() => 2")
        rejected = ask.structuredContent["approval_id"]
        await self.manager.reject(rejected, comment="no")

        tool = ("tool_call", "browser.eval_js")
        self.assertEqual(
            await self.trail(approved),
            [("receipt approval_requested", *tool), ("receipt approve", *tool), ("audit approve", *tool)],
        )
        self.assertEqual(
            await self.trail(rejected),
            [("receipt approval_requested", *tool), ("receipt reject", *tool), ("audit reject", *tool)],
        )

    async def test_an_action_approval_is_named_as_one(self) -> None:
        click = BrowserActionDecision(action="click", reason="pay", element_id="el-a", risk_category="payment")
        with self.assertRaises(ApprovalRequiredError) as ctx:
            await self.manager.execute_decision(SESSION, click)
        approval_id = ctx.exception.approval.id
        await self.manager.approve(approval_id)

        self.assertEqual(
            await self.trail(approval_id),
            [
                ("receipt approval_requested", "action", None),
                ("receipt approve", "action", None),
                ("audit approve", "action", None),
            ],
        )


class OriginIsolationTests(ApprovalBindingBase):
    """The origin is part of what an approval is for: matching and reuse compare it.

    A rejection counts whatever its origin: it only ever restricts, and a
    takeover-shaped action approval can now only come from before the upgrade.
    """

    async def test_a_forged_pending_approval_is_not_reused_for_the_tool_call(self) -> None:
        forged = await self.forged_pending("browser.eval_js", {"expression": BENIGN}, kind="write")

        ask = await self.call("browser.eval_js", session_id=SESSION, expression=BENIGN)

        approval_id = ask.structuredContent["approval_id"]
        self.assertNotEqual(approval_id, forged.id)
        self.assertEqual((await self.manager.approvals.get(approval_id)).origin, "tool_call")

    async def test_rejecting_a_takeover_approval_of_the_same_call_still_blocks_the_tool_call(self) -> None:
        self.manager.settings.autonomous_approvals = True
        forged = await self.forged_pending("browser.eval_js", {"expression": BENIGN}, kind="write")
        await self.manager.reject(forged.id, comment="not mine")
        ask = await self.call("browser.eval_js", session_id=SESSION, expression=BENIGN)

        approved = await self.call("browser.approve_approval", approval_id=ask.structuredContent["approval_id"])

        self.assertTrue(approved.isError)
        self.assertIn("an operator rejected this action", approved.content[0].text)

    async def test_an_operators_rejection_still_blocks_the_same_tool_call(self) -> None:
        self.manager.settings.autonomous_approvals = True
        first = await self.call("browser.eval_js", session_id=SESSION, expression=BENIGN)
        await self.manager.reject(first.structuredContent["approval_id"], comment="no")
        again = await self.call("browser.eval_js", session_id=SESSION, expression=BENIGN)

        approved = await self.call("browser.approve_approval", approval_id=again.structuredContent["approval_id"])

        self.assertTrue(approved.isError)
        self.assertIn("an operator rejected this action", approved.content[0].text)

    async def test_a_tool_call_approval_is_not_spendable_as_a_browser_action(self) -> None:
        approval_id = await self.approved_eval_js()
        approval = await self.manager.approvals.get(approval_id)

        with self.assertRaisesRegex(PermissionError, "is for a tool call, not a browser action"):
            await self.manager.approvals.require_approved(
                approval_id=approval_id, session_id=SESSION, kind="write", action=approval.action
            )

    async def test_a_caller_cannot_inject_an_origin(self) -> None:
        injected = {
            "browser.execute_action": {
                "session_id": SESSION,
                "workflow_profile": "governed",
                "action": {"action": "click", "reason": "r", "element_id": "el-a", "origin": "tool_call"},
            },
            "browser.eval_js": {"session_id": SESSION, "expression": BENIGN, "origin": "action"},
        }
        for tool, arguments in injected.items():
            with self.subTest(tool=tool):
                response = await self.call(tool, **arguments)
                self.assertTrue(response.isError)
                self.assertIn("Invalid arguments", response.content[0].text)
        self.assertEqual(await self.manager.approvals.list(), [])


class LegacyRecordTests(ApprovalBindingBase):
    """A record stored before origin existed loads as an action approval, and is not spent on a tool."""

    def strip_origin(self, approval_id: str) -> None:
        path = Path(self.manager.settings.approval_root) / f"{approval_id}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record.pop("origin", None)
        path.write_text(json.dumps(record), encoding="utf-8")

    async def test_a_stand_in_approved_before_the_upgrade_must_be_asked_for_again(self) -> None:
        approval_id = await self.approved_eval_js()
        self.strip_origin(approval_id)

        spend = await self.call("browser.eval_js", session_id=SESSION, expression=BENIGN, approval_id=approval_id)
        again = await self.call("browser.eval_js", session_id=SESSION, expression=BENIGN)

        self.assertEqual(self.page.evaluated, [])
        self.assertIn("is for a browser action, not a tool call", spend.content[0].text)
        self.assertEqual(again.structuredContent.get("status"), "approval_required")
        self.assertNotEqual(again.structuredContent["approval_id"], approval_id)

    async def test_a_rejection_stored_before_the_upgrade_still_blocks_self_approval(self) -> None:
        self.manager.settings.autonomous_approvals = True
        first = await self.call("browser.eval_js", session_id=SESSION, expression=BENIGN)
        rejected = first.structuredContent["approval_id"]
        await self.manager.reject(rejected, comment="no")
        self.strip_origin(rejected)
        again = await self.call("browser.eval_js", session_id=SESSION, expression=BENIGN)

        approved = await self.call("browser.approve_approval", approval_id=again.structuredContent["approval_id"])

        self.assertEqual((await self.manager.approvals.get(rejected)).origin, "action")
        self.assertTrue(approved.isError)
        self.assertIn("an operator rejected this action", approved.content[0].text)
        self.assertEqual(self.page.evaluated, [])

    async def test_both_backends_load_a_record_without_origin_as_an_action(self) -> None:
        stand_in, _ = McpToolGateway._governed_call_decision(
            self.gateway._registry.get("browser.eval_js"), EvalJsInput(session_id=SESSION, expression=BENIGN)
        )
        for backend in ("file", "sqlite"):
            with self.subTest(backend=backend):
                root = Path(self.tempdir.name) / backend
                db_path = root / "operator.db" if backend == "sqlite" else None
                store = ApprovalStore(root / "approvals", db_path=str(db_path) if db_path else None)
                await store.startup()
                approval = await store.create_or_reuse_pending(
                    session_id=SESSION, kind="write", reason="r", action=stand_in, origin="tool_call"
                )
                await store.approve(approval.id)
                record = json.loads((await store.get(approval.id)).model_dump_json())
                record.pop("origin")
                if db_path is None:
                    (root / "approvals" / f"{approval.id}.json").write_text(json.dumps(record), encoding="utf-8")
                else:
                    with sqlite3.connect(db_path) as conn:
                        conn.execute("UPDATE approvals SET payload = ? WHERE id = ?", (json.dumps(record), approval.id))
                    conn.close()

                self.assertEqual((await store.get(approval.id)).origin, "action")
                with self.assertRaisesRegex(PermissionError, "is for a browser action, not a tool call"):
                    await store.require_approved(
                        approval_id=approval.id, session_id=SESSION, kind="write", action=stand_in, origin="tool_call"
                    )


class LegitimateTakeoverTests(ApprovalBindingBase):
    """Takeover itself is not refused: only as an action to approve and execute."""

    async def test_the_takeover_tool_asks_for_a_tool_call_approval_when_governed(self) -> None:
        arguments = {"session_id": SESSION, "workflow_profile": "governed", "reason": "solve the puzzle"}
        ask = await self.call("browser.request_human_takeover", **arguments)
        approval_id = ask.structuredContent["approval_id"]
        await self.manager.approve(approval_id)

        done = await self.call("browser.request_human_takeover", **arguments, approval_id=approval_id)

        self.assertFalse(done.isError, done.content[0].text)
        self.assertEqual(done.structuredContent["reason"], "solve the puzzle")
        self.assertEqual((await self.manager.approvals.get(approval_id)).origin, "tool_call")

    async def test_the_orchestrator_hands_over_on_a_takeover_decision(self) -> None:
        decision = BrowserActionDecision(action="request_human_takeover", reason="needs a human")
        orchestrator = BrowserOrchestrator(self.manager, SimpleNamespace())

        step = await orchestrator._execute_decision(
            session_id=SESSION,
            goal="g",
            observation={},
            provider_decision=ProviderDecision(provider="openai", model="m", decision=decision),
            upload_approved=False,
            approval_id=None,
            workflow_profile="governed",
        )

        self.assertEqual(step.status, "takeover")
        self.assertEqual(step.execution["reason"], "needs a human")

    async def test_a_bot_challenge_still_requests_takeover(self) -> None:
        self.manager._light_snapshot = AsyncMock(return_value={"url": self.page.url})  # type: ignore[method-assign]
        self.manager._maybe_handle_totp = AsyncMock(return_value=None)  # type: ignore[method-assign]
        self.manager._check_bot_challenge = AsyncMock(return_value={"signal": "captcha"})  # type: ignore[method-assign]
        takeover = AsyncMock(wraps=self.manager.request_human_takeover)
        self.manager.request_human_takeover = takeover  # type: ignore[method-assign]

        with self.assertRaises(BrowserActionError) as ctx:
            await self.manager.wait(SESSION, 0)

        self.assertEqual(ctx.exception.code, "captcha_detected")
        takeover.assert_awaited_once_with(SESSION, reason="Bot challenge detected: captcha")


class ApprovalRouteTests(ApprovalBindingBase):
    """The REST answers: 409 for a stand-in on the execute route, 400 for a takeover as an action."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.app = FastAPI()
        self.app.include_router(create_operations_router(manager=self.manager, proxy_store=None, cron_service=None))
        self.app.include_router(create_sessions_router(manager=self.manager))

    async def post(self, path: str, body: dict[str, Any] | None = None) -> httpx.Response:
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(path, json=body)

    async def test_mcp38_the_execute_route_answers_409_and_does_not_consume(self) -> None:
        approval_id = await self.approved_eval_js()

        response = await self.post(f"/approvals/{approval_id}/execute")

        self.assertEqual((response.status_code, response.json()), (409, {"detail": "Conflict"}))
        self.assertEqual((await self.manager.approvals.get(approval_id)).status, "approved")

    async def test_a_takeover_approved_as_an_action_is_refused_on_its_own_terms_and_not_consumed(self) -> None:
        # What an approved stand-in stored before origin existed loads as: nothing can run it.
        approval_id = await self.forged_approval("browser.eval_js", {"expression": BENIGN}, kind="write")

        with self.assertRaises(PermissionError) as ctx:
            await self.manager.execute_approval(approval_id)
        response = await self.post(f"/approvals/{approval_id}/execute")

        self.assertIn("stored before approvals recorded their origin", str(ctx.exception))
        self.assertIn("without approval_id", str(ctx.exception))
        self.assertNotIn("runs the approved tool call again with this approval_id", str(ctx.exception))
        self.assertEqual((response.status_code, response.json()), (409, {"detail": "Conflict"}))
        self.assertEqual((await self.manager.approvals.get(approval_id)).status, "approved")

    async def test_a_takeover_through_the_actions_route_is_a_400_and_queues_nothing(self) -> None:
        action = {"action": "request_human_takeover", "reason": "r", "risk_category": "payment", "text": "t"}

        response = await self.post(f"/sessions/{SESSION}/actions/execute", {"action": action})

        self.assertEqual((response.status_code, response.json()), (400, {"detail": "Invalid request"}))
        self.assertEqual(await self.manager.approvals.list(), [])

    async def test_an_origin_in_a_rest_action_is_rejected(self) -> None:
        action = {
            "action": "click",
            "reason": "r",
            "element_id": "el-a",
            "risk_category": "post",
            "origin": "tool_call",
        }

        response = await self.post(f"/sessions/{SESSION}/actions/execute", {"action": action})

        self.assertEqual(response.status_code, 422)
        self.assertEqual(await self.manager.approvals.list(), [])


class ExecuteApprovalOnStandInTests(ApprovalBindingBase):
    async def test_mcp38_execute_approval_does_not_consume_a_stand_in(self) -> None:
        approval_id = await self.approved_eval_js()
        with self.assertRaises(PermissionError):
            await self.manager.execute_approval(approval_id)
        self.assertEqual((await self.manager.approvals.get(approval_id)).status, "approved")

    async def test_mcp38_gateway_execute_approval_leaves_the_real_call_spendable(self) -> None:
        approval_id = await self.approved_eval_js()
        refused = await self.call("browser.execute_approval", approval_id=approval_id)
        self.assertTrue(refused.isError)
        spend = await self.call("browser.eval_js", session_id=SESSION, expression=BENIGN, approval_id=approval_id)
        self.assertFalse(spend.isError, f"approved eval_js was refused afterwards: {spend.structuredContent}")
        self.assertEqual(self.page.evaluated, [BENIGN])

    async def test_mcp38_execute_approval_refuses_a_stand_in_with_a_permission_error(self) -> None:
        # The REST route maps PermissionError to 409 (a clear refusal), but ValueError
        # to 400 "Unsupported action: request_human_takeover", a confusing message.
        approval_id = await self.approved_eval_js()
        with self.assertRaisesRegex(PermissionError, "runs the approved tool call again with this approval_id"):
            await self.manager.execute_approval(approval_id)


class ReplayVariantsThroughTheGateway(ApprovalBindingBase):
    """TEST-11: approve action A through the gateway, replay B; the store's binding refuses each."""

    A = {"action": "click", "reason": "r", "element_id": "el-a", "risk_category": "post"}

    VARIANTS = {
        "other_element": {**A, "element_id": "el-b"},
        "other_selector": {**A, "element_id": None, "selector": "#b"},
        "other_kind": {**A, "risk_category": "payment"},
        "other_text": {**A, "action": "type", "text": "x"},
    }

    async def test_replay_variants(self) -> None:
        self.manager.click = AsyncMock(return_value={"ok": True})  # type: ignore[method-assign]
        self.manager.type = AsyncMock(return_value={"ok": True})  # type: ignore[method-assign]
        ask = await self.call("browser.execute_action", session_id=SESSION, action=self.A)
        approval_id = ask.structuredContent["approval"]["id"]
        await self.manager.approve(approval_id)
        for name, variant in self.VARIANTS.items():
            variant = {k: v for k, v in variant.items() if v is not None}
            response = await self.call(
                "browser.execute_action", session_id=SESSION, action=variant, approval_id=approval_id
            )
            self.assertTrue(response.isError, f"{name}: replay of a different action was accepted")
        self.manager.click.assert_not_awaited()
        self.manager.type.assert_not_awaited()
        # A real second session cannot spend session-1's approval: the binding refuses, not a missing session.
        first = self.manager.sessions[SESSION]
        self.manager.sessions["session-2"] = BrowserSession(
            id="session-2",
            name="session-2",
            created_at=datetime.now(UTC),
            context=FakeContext(),  # type: ignore[arg-type]
            page=FakePage(),  # type: ignore[arg-type]
            artifact_dir=first.artifact_dir,
            auth_dir=first.auth_dir,
            upload_dir=first.upload_dir,
            takeover_url=first.takeover_url,
            trace_path=first.trace_path,
        )
        other = await self.call(
            "browser.execute_action", session_id="session-2", action=self.A, approval_id=approval_id
        )
        self.assertIn(f"approval {approval_id} does not belong to session session-2", other.content[0].text)
        # The exact action still runs once.
        ok = await self.call("browser.execute_action", session_id=SESSION, action=self.A, approval_id=approval_id)
        self.assertFalse(ok.isError, ok.structuredContent)


if __name__ == "__main__":
    unittest.main()
