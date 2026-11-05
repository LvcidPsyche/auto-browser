"""Approval webhook targets are checked after DNS, and the request goes to the address that was checked (STORE-20).

The old check read an IP literal only: 0x7f.1 and 2130706433 passed it, and a name that resolved to 127.0.0.1 or
169.254.169.254 was posted to. The full URL (path and query often carry a token) was logged on an HTTP error.
"""

from __future__ import annotations

import json
import socket
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app import webhooks
from app.models import ApprovalRecord, BrowserActionDecision

_PUBLIC = "93.184.216.34"


def _approval() -> ApprovalRecord:
    return ApprovalRecord(
        id="approval-w",
        session_id="session-1",
        kind="write",
        status="pending",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        reason="needs review",
        action=BrowserActionDecision(action="click", reason="click", selector="button", risk_category="write"),
    )


class WebhookTargetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.client = Mock()
        self.client.post = AsyncMock(return_value=Mock(status_code=200))
        patcher = patch.object(webhooks, "get_client", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _resolver(self, *addresses: str) -> AsyncMock:
        mock = AsyncMock(return_value=list(addresses))
        patcher = patch.object(webhooks, "_resolve_host", mock)
        patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    async def _send(self, url: str, **kwargs) -> None:
        with patch.object(webhooks.logger, "warning"):
            await webhooks.dispatch_approval_event(_approval(), webhook_url=url, **kwargs)

    async def test_a_name_that_resolves_to_a_private_address_is_not_posted_to(self) -> None:
        for address in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "0.0.0.0", "::1", "fe80::1", "192.168.1.1"):
            with self.subTest(address=address):
                self._resolver(address)
                await self._send("https://rebind.example.com/hook")
        self.assertEqual(self.client.post.await_count, 0)

    async def test_one_private_answer_among_public_ones_refuses_the_target(self) -> None:
        self._resolver(_PUBLIC, "127.0.0.1")
        await self._send("https://mixed.example.com/hook")
        self.assertEqual(self.client.post.await_count, 0)

    async def test_numeric_spellings_of_loopback_are_refused_without_a_lookup(self) -> None:
        resolver = self._resolver(_PUBLIC)
        for url in (
            "http://2130706433/hook",
            "http://0x7f.1/hook",
            "http://127.1/hook",
            "http://0177.0.0.1/hook",
            "http://localhost./hook",
            "http://%31%32%37.0.0.1/hook",
        ):
            with self.subTest(url=url):
                await self._send(url)
        self.assertEqual(self.client.post.await_count, 0)
        resolver.assert_not_awaited()

    async def test_an_unresolvable_name_is_not_posted_to(self) -> None:
        patcher = patch.object(webhooks, "_resolve_host", AsyncMock(side_effect=socket.gaierror("no such host")))
        patcher.start()
        self.addCleanup(patcher.stop)
        await self._send("https://nowhere.example.com/hook")
        self.assertEqual(self.client.post.await_count, 0)

    async def test_the_request_goes_to_the_address_that_was_checked(self) -> None:
        self._resolver(_PUBLIC)
        await self._send("https://hooks.example.com:8443/approval?x=1", webhook_secret="s")
        call = self.client.post.await_args
        self.assertEqual(call.args[0], f"https://{_PUBLIC}:8443/approval?x=1")
        self.assertEqual(call.kwargs["headers"]["Host"], "hooks.example.com:8443")
        self.assertEqual(call.kwargs["extensions"], {"sni_hostname": "hooks.example.com"})
        self.assertIn("X-Webhook-Signature", call.kwargs["headers"])

    async def test_an_http_target_is_pinned_without_sni_and_keeps_userinfo(self) -> None:
        self._resolver(_PUBLIC)
        await self._send("http://u:p@hooks.example.com/approval")
        call = self.client.post.await_args
        self.assertEqual(call.args[0], f"http://u:p@{_PUBLIC}/approval")
        self.assertEqual(call.kwargs["headers"]["Host"], "hooks.example.com")
        self.assertEqual(call.kwargs.get("extensions", {}), {})

    async def test_an_ipv6_address_is_bracketed_in_the_pinned_url(self) -> None:
        self._resolver("2606:2800:220:1:248:1893:25c8:1946")
        await self._send("https://hooks.example.com/approval")
        self.assertEqual(self.client.post.await_args.args[0], "https://[2606:2800:220:1:248:1893:25c8:1946]/approval")

    async def test_a_public_ip_literal_is_posted_to_as_written(self) -> None:
        resolver = self._resolver()
        await self._send(f"https://{_PUBLIC}/approval")
        self.assertEqual(self.client.post.await_args.args[0], f"https://{_PUBLIC}/approval")
        resolver.assert_not_awaited()

    async def test_the_operator_can_allow_private_targets(self) -> None:
        self._resolver("10.0.0.5")
        await self._send("http://n8n.internal:5678/hook", allow_private_targets=True)
        self.assertEqual(self.client.post.await_args.args[0], "http://10.0.0.5:5678/hook")

    async def test_the_logs_carry_the_scheme_and_host_only(self) -> None:
        self._resolver(_PUBLIC)
        self.client.post = AsyncMock(return_value=Mock(status_code=500))
        url = "https://user:topsecret@hooks.example.com/services/T0KEN/path?token=abc123"
        with self.assertLogs("app.webhooks", level="WARNING") as captured:
            await webhooks.dispatch_approval_event(_approval(), webhook_url=url)
        text = "\n".join(captured.output)
        self.assertIn("https://hooks.example.com", text)
        for leaked in ("topsecret", "T0KEN", "abc123", "/services", "user:"):
            self.assertNotIn(leaked, text)

    async def test_a_blocked_target_logs_the_host_only(self) -> None:
        with self.assertLogs("app.webhooks", level="WARNING") as captured:
            await webhooks.dispatch_approval_event(_approval(), webhook_url="http://127.0.0.1:8080/secret?token=abc")
        text = "\n".join(captured.output)
        self.assertNotIn("token=abc", text)
        self.assertNotIn("/secret", text)

    async def test_the_body_is_unchanged(self) -> None:
        self._resolver(_PUBLIC)
        await self._send("https://hooks.example.com/approval")
        body = json.loads(self.client.post.await_args.kwargs["content"])
        self.assertEqual(body["approval_id"], "approval-w")


if __name__ == "__main__":
    unittest.main()
