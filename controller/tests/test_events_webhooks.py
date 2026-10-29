from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app import events, webhooks
from app.models import ApprovalRecord, BrowserActionDecision
from app.version import __version__


class EventsTests(unittest.TestCase):
    def tearDown(self) -> None:
        for session_id, queues in list(events._SESSION_QUEUES.items()):
            for queue in list(queues):
                events.unsubscribe(session_id, queue)
        for queue in list(events._GLOBAL_QUEUES):
            events.unsubscribe_all(queue)

    def test_session_and_global_subscribers_receive_public_events(self) -> None:
        session_queue = events.subscribe("session-1")
        global_queue = events.subscribe_all()

        events.emit_observe("session-1", "https://example.com", "Example", "/s.png")
        events.emit_action("session-1", "click", "ok", {"selector": "button"})
        events.emit_approval(
            "session-1", "approval-1", "write", "pending", "needs review", origin="action", summary="click"
        )
        events.emit_session("session-1", "closed")

        session_payloads = [json.loads(session_queue.get_nowait()) for _ in range(4)]
        global_payloads = [json.loads(global_queue.get_nowait()) for _ in range(4)]
        self.assertEqual([item["event"] for item in session_payloads], ["observe", "action", "approval", "session"])
        self.assertEqual([item["event"] for item in global_payloads], ["observe", "action", "approval", "session"])

        events.unsubscribe("session-1", session_queue)
        events.unsubscribe_all(global_queue)
        self.assertNotIn("session-1", events._SESSION_QUEUES)
        self.assertEqual(events._GLOBAL_QUEUES, [])

    def test_the_reason_an_agent_wrote_is_shown_with_its_code_points(self) -> None:
        queue = events.subscribe("session-1")

        events.emit_approval(
            "session-1", "approval-1", "payment", "pending", "refund\u202e evil", origin="action", summary="click"
        )

        self.assertEqual(json.loads(queue.get_nowait())["reason"], "refund[U+202E] evil")

    def test_a_new_subscriber_does_not_inherit_a_departed_ones_drops(self) -> None:
        before = events.dropped_event_count()
        queue = events.subscribe("session-1")
        for _ in range(queue.maxsize + 3):
            events.emit_action("session-1", "click", "ok")
        self.assertEqual(events.dropped_event_count() - before, 3)

        events.unsubscribe("session-1", queue)
        self.assertNotIn(id(queue), events._DROPPED_EVENTS)
        # The process-wide total keeps counting what was dropped.
        self.assertEqual(events.dropped_event_count() - before, 3)

    def test_session_closed_event_is_recognised_for_its_own_session_only(self) -> None:
        queue = events.subscribe("session-1")
        events.emit_session("session-1", "closed")
        payload = queue.get_nowait()
        self.assertTrue(events.is_session_closed_event(payload, "session-1"))
        self.assertFalse(events.is_session_closed_event(payload, "session-2"))
        self.assertFalse(events.is_session_closed_event("not json", "session-1"))


class WebhookTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_posts_signed_approval_event_and_ignores_failures(self) -> None:
        approval = ApprovalRecord(
            id="approval-1",
            session_id="session-1",
            kind="write",
            status="pending",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            reason="needs review",
            action=BrowserActionDecision(action="click", reason="click", selector="button", risk_category="write"),
        )
        client = Mock()
        client.post = AsyncMock(return_value=Mock(status_code=500))

        with patch.object(webhooks, "get_client", return_value=client):
            await webhooks.dispatch_approval_event(
                approval,
                webhook_url="https://hooks.example.com/approval",
                webhook_secret="secret",
                extra={"source": "test"},
            )
            client.post.side_effect = RuntimeError("network down")
            await webhooks.dispatch_approval_event(approval, webhook_url="https://hooks.example.com/approval")

        first_call = client.post.await_args_list[0]
        self.assertEqual(first_call.args[0], "https://hooks.example.com/approval")
        self.assertIn("X-Webhook-Signature", first_call.kwargs["headers"])
        self.assertEqual(first_call.kwargs["headers"]["User-Agent"], f"auto-browser/{__version__}")
        self.assertIn(b'"source":"test"', first_call.kwargs["content"])
        self.assertEqual(client.post.await_count, 2)

    async def test_dispatch_blocks_disallowed_webhook_targets(self) -> None:
        approval = ApprovalRecord(
            id="approval-2",
            session_id="session-1",
            kind="write",
            status="pending",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            reason="needs review",
            action=BrowserActionDecision(action="click", reason="click", selector="button", risk_category="write"),
        )
        client = Mock()
        client.post = AsyncMock(return_value=Mock(status_code=200))

        with (
            patch.object(webhooks, "get_client", return_value=client),
            patch.object(webhooks.logger, "warning") as mock_warning,
        ):
            blocked_urls = [
                "http://127.0.0.1:8080/approval",
                "http://[::1]/approval",
                "http://localhost/approval",
                "http://0.0.0.0/approval",
                "http://[::]/approval",
                "http://224.0.0.1/approval",
                "http://169.254.169.254/approval",
                "http://metadata.google.internal/approval",
            ]
            for blocked_url in blocked_urls:
                await webhooks.dispatch_approval_event(approval, webhook_url=blocked_url)

        self.assertEqual(client.post.await_count, 0)
        self.assertEqual(mock_warning.call_count, len(blocked_urls))

    async def test_the_reason_an_agent_wrote_is_shown_with_its_code_points(self) -> None:
        approval = ApprovalRecord(
            id="approval-7",
            session_id="session-1",
            kind="payment",
            status="pending",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            reason="approve the refund\u202e\u2028to mallory",
            action=BrowserActionDecision(action="click", reason="r", selector="#refund"),
        )
        client = Mock()
        client.post = AsyncMock(return_value=Mock(status_code=200))

        with patch.object(webhooks, "get_client", return_value=client):
            await webhooks.dispatch_approval_event(approval, webhook_url="https://hooks.example.com/approval")

        body = json.loads(client.post.await_args.kwargs["content"])
        self.assertEqual(body["reason"], "approve the refund[U+202E][U+2028]to mallory")

    async def test_the_body_says_what_the_approval_is_for_apart_from_its_reason(self) -> None:
        # The same agent-written reason on a tool call's approval and on a coordinate click.
        reason = 'Approve browser.set_cookies with {"cookies": [{"name": "sid", "value": "<redacted>"}]}'
        tool_call = ApprovalRecord(
            id="approval-3",
            session_id="session-1",
            kind="account_change",
            status="pending",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            reason=reason,
            action=BrowserActionDecision(
                action="request_human_takeover", reason=reason, text="browser.set_cookies sha256:" + "ab" * 32
            ),
            origin="tool_call",
        )
        click = tool_call.model_copy(
            update={
                "id": "approval-4",
                "action": BrowserActionDecision(action="click", reason=reason, x=640, y=410),
                "origin": "action",
            }
        )
        pick = tool_call.model_copy(
            update={
                "id": "approval-5",
                "action": BrowserActionDecision(action="select_option", reason=reason, element_id="e12", value="all"),
                "origin": "action",
            }
        )
        # The stand-in's shape asked for as an action (or stored before origin existed): no tool call.
        forged = tool_call.model_copy(update={"id": "approval-6", "origin": "action"})
        client = Mock()
        client.post = AsyncMock(return_value=Mock(status_code=200))

        with patch.object(webhooks, "get_client", return_value=client):
            for approval in (tool_call, click, pick, forged):
                await webhooks.dispatch_approval_event(approval, webhook_url="https://hooks.example.com/approval")

        bodies = [json.loads(call.kwargs["content"]) for call in client.post.await_args_list]
        self.assertEqual(
            [(body["origin"], body["action_summary"]) for body in bodies],
            [
                ("tool_call", "tool call browser.set_cookies"),
                ("action", "click · x=640 · y=410"),
                ("action", 'select_option · element_id="e12" · value="all" (matches an option\'s value or label)'),
                # Shown, with what reads it: no action does (round 4, item 8).
                (
                    "action",
                    'request_human_takeover · text="browser.set_cookies sha256:'
                    + "ab" * 32
                    + '" (not used by request_human_takeover)',
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
