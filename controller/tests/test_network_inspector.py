from __future__ import annotations

import asyncio
import unittest

from app.network_inspector import NetworkInspector


class FakeRequest:
    def __init__(
        self,
        *,
        url: str = "https://example.com/api",
        method: str = "GET",
        resource_type: str = "xhr",
        headers: dict[str, str] | None = None,
        post_data: str | None = None,
        failure: str = "net::ERR_FAILED",
    ) -> None:
        self.url = url
        self.method = method
        self.resource_type = resource_type
        self.headers = headers or {}
        self.post_data = post_data
        self._failure = failure

    def failure(self) -> str:
        return self._failure


class FakeResponse:
    def __init__(
        self,
        request: FakeRequest,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> None:
        self.request = request
        self.status = status
        self.headers = headers or {}
        self._body = body

    async def body(self) -> bytes:
        return self._body


class FakePage:
    def __init__(self) -> None:
        self.listeners: dict[str, object] = {}

    def on(self, event: str, callback) -> None:
        self.listeners[event] = callback

    def remove_listener(self, event: str, callback) -> None:
        self.listeners.pop(event, None)


class FakeScrubber:
    def network_body(self, body, content_type: str):
        return "[redacted]", ["secret"] if "secret" in str(body).lower() else []


class NetworkInspectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_request_masks_headers_scrubs_bodies_and_fires_hook(self) -> None:
        inspector = NetworkInspector("session-1", scrubber=FakeScrubber())
        matched: list[dict] = []

        async def hook(entry: dict) -> None:
            matched.append(entry)

        inspector.register_hook("https://example.com/*", hook)
        request = FakeRequest(
            method="POST",
            headers={"authorization": "Bearer secret", "content-type": "application/json"},
            post_data='{"secret":"token"}',
        )

        await inspector._handle_request(request)
        await inspector._handle_response(
            FakeResponse(
                request,
                status=201,
                headers={"set-cookie": "sid=secret", "content-type": "application/json"},
                body=b'{"secret":"response"}',
            )
        )
        await inspector._handle_request_finished(request)

        entries = inspector.entries()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["status"], 201)
        self.assertEqual(entry["request_headers"]["authorization"], "[MASKED]")
        self.assertEqual(entry["response_headers"]["set-cookie"], "[MASKED]")
        self.assertEqual(entry["request_body"], "[redacted]")
        self.assertEqual(entry["response_body"], "[redacted]")
        self.assertTrue(entry["pii_redacted"])
        self.assertEqual(matched[0]["id"], entry["id"])
        self.assertEqual(inspector.summary()["hooks"], 1)
        self.assertTrue(inspector.remove_hook("https://example.com/*"))
        self.assertFalse(inspector.remove_hook("https://example.com/*"))

    async def test_skips_static_resources_records_failures_and_flushes_pending(self) -> None:
        inspector = NetworkInspector("session-2", capture_bodies=False)
        await inspector._handle_request(FakeRequest(resource_type="image"))
        self.assertEqual(inspector.summary()["total"], 0)

        failed = FakeRequest(url="https://example.com/fail")
        await inspector._handle_request(failed)
        await inspector._handle_request_failed(failed)
        failure = inspector.entries()[0]
        self.assertTrue(failure["failed"])
        self.assertEqual(failure["failure_text"], "net::ERR_FAILED")

        pending = FakeRequest(url="https://example.com/pending")
        await inspector._handle_request(pending)
        await inspector._flush_pending()
        flushed = inspector.entries(url_contains="pending")[0]
        self.assertTrue(flushed["failed"])
        self.assertEqual(flushed["failure_text"], "session detached")

        page = FakePage()
        inspector.attach(page)
        self.assertIn("request", page.listeners)
        inspector.detach()
        await asyncio.sleep(0)
        self.assertEqual(page.listeners, {})
        inspector.clear()
        self.assertEqual(inspector.entries(), [])


if __name__ == "__main__":
    unittest.main()
