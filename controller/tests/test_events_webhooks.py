from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app import events, webhooks
from app.models import ApprovalRecord, BrowserActionDecision


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
        events.emit_approval("session-1", "approval-1", "write", "pending", "needs review")
        events.emit_session("session-1", "closed")

        session_payloads = [json.loads(session_queue.get_nowait()) for _ in range(4)]
        global_payloads = [json.loads(global_queue.get_nowait()) for _ in range(4)]
        self.assertEqual([item["event"] for item in session_payloads], ["observe", "action", "approval", "session"])
        self.assertEqual([item["event"] for item in global_payloads], ["observe", "action", "approval", "session"])

        events.unsubscribe("session-1", session_queue)
        events.unsubscribe_all(global_queue)
        self.assertNotIn("session-1", events._SESSION_QUEUES)
        self.assertEqual(events._GLOBAL_QUEUES, [])


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
        self.assertEqual(first_call.kwargs["headers"]["User-Agent"], "auto-browser/1.0.5")
        self.assertIn(b'"source":"test"', first_call.kwargs["content"])
        self.assertEqual(client.post.await_count, 2)


if __name__ == "__main__":
    unittest.main()
