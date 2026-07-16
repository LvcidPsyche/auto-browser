from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import patch

import httpx
from auto_browser_client import AutoBrowserClient
from auto_browser_client.client import AutoBrowserError


def _json_body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content.decode("utf-8"))


def _make_client(
    responder,
    *,
    token: str | None = None,
    base_url: str = "http://auto-browser.test",
) -> tuple[AutoBrowserClient, list[httpx.Request]]:
    """Client wired to a MockTransport, mirroring how _client() builds the real one."""
    requests: list[httpx.Request] = []
    client = AutoBrowserClient(base_url, token=token)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responder(request)

    client._sync_client = httpx.Client(
        base_url=client.base_url,
        headers=client._headers,
        transport=httpx.MockTransport(handler),
    )
    return client, requests


def _ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


class ClientConstructionTests(unittest.TestCase):
    def test_token_sends_bearer_authorization_header(self) -> None:
        client, requests = _make_client(_ok, token="sekret")
        client.health()
        self.assertEqual(requests[0].headers["Authorization"], "Bearer sekret")

    def test_no_token_sends_no_authorization_header(self) -> None:
        client, requests = _make_client(_ok)
        client.health()
        self.assertNotIn("Authorization", requests[0].headers)

    def test_trailing_slash_stripped_from_base_url(self) -> None:
        client = AutoBrowserClient("http://auto-browser.test/")
        self.assertEqual(client.base_url, "http://auto-browser.test")

    def test_sync_context_manager_opens_and_closes_client(self) -> None:
        with AutoBrowserClient("http://auto-browser.test") as client:
            self.assertIsInstance(client._sync_client, httpx.Client)
        self.assertIsNone(client._sync_client)


class ClientErrorTests(unittest.TestCase):
    def test_http_error_raises_with_status_and_json_detail(self) -> None:
        client, _ = _make_client(lambda _r: httpx.Response(404, json={"detail": "Not found"}))
        with self.assertRaises(AutoBrowserError) as ctx:
            client.get_session("missing")
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.detail, {"detail": "Not found"})
        self.assertIn("HTTP 404", str(ctx.exception))

    def test_http_error_with_non_json_body_falls_back_to_text(self) -> None:
        client, _ = _make_client(lambda _r: httpx.Response(502, text="bad gateway"))
        with self.assertRaises(AutoBrowserError) as ctx:
            client.list_sessions()
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(ctx.exception.detail, "bad gateway")


class SessionRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client, self.requests = _make_client(_ok)

    def test_create_session_omits_unset_optionals(self) -> None:
        self.client.create_session()
        self.assertEqual(self.requests[-1].url.path, "/sessions")
        self.assertEqual(_json_body(self.requests[-1]), {})

    def test_create_session_sends_provided_fields(self) -> None:
        self.client.create_session(name="n", start_url="https://example.com", auth_profile="p")
        self.assertEqual(
            _json_body(self.requests[-1]),
            {"name": "n", "start_url": "https://example.com", "auth_profile": "p"},
        )

    def test_get_and_close_session_routes_and_methods(self) -> None:
        self.client.get_session("s-1")
        self.client.close_session("s-1")
        self.assertEqual(self.requests[0].method, "GET")
        self.assertEqual(self.requests[0].url.path, "/sessions/s-1")
        self.assertEqual(self.requests[1].method, "DELETE")
        self.assertEqual(self.requests[1].url.path, "/sessions/s-1")

    def test_observe_sends_preset_and_limit(self) -> None:
        self.client.observe("s-1", preset="fast", limit=10)
        self.assertEqual(self.requests[-1].url.path, "/sessions/s-1/observe")
        self.assertEqual(_json_body(self.requests[-1]), {"preset": "fast", "limit": 10})

    def test_screenshot_and_diff_routes(self) -> None:
        self.client.screenshot("s-1", label="probe")
        self.client.screenshot_diff("s-1")
        self.assertEqual(self.requests[0].url.path, "/sessions/s-1/screenshot")
        self.assertEqual(_json_body(self.requests[0]), {"label": "probe"})
        self.assertEqual(self.requests[1].url.path, "/sessions/s-1/screenshot/compare")


class AgentAndApprovalRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client, self.requests = _make_client(_ok)

    def test_agent_step_merges_extra_kwargs(self) -> None:
        self.client.agent_step("s-1", provider="openai", goal="g", workflow_profile="fast")
        self.assertEqual(self.requests[-1].url.path, "/sessions/s-1/agent/step")
        self.assertEqual(
            _json_body(self.requests[-1]),
            {"provider": "openai", "goal": "g", "workflow_profile": "fast"},
        )

    def test_agent_run_defaults_max_steps(self) -> None:
        self.client.agent_run("s-1", provider="openai", goal="g")
        self.assertEqual(
            _json_body(self.requests[-1]),
            {"provider": "openai", "goal": "g", "max_steps": 6},
        )

    def test_list_approvals_filters_as_query_params(self) -> None:
        self.client.list_approvals(status="pending", session_id="s-1")
        request = self.requests[-1]
        self.assertEqual(request.url.path, "/approvals")
        self.assertEqual(dict(request.url.params), {"status": "pending", "session_id": "s-1"})

    def test_approve_and_reject_routes(self) -> None:
        self.client.approve("a-1", comment="ok")
        self.client.reject("a-2")
        self.assertEqual(self.requests[0].url.path, "/approvals/a-1/approve")
        self.assertEqual(_json_body(self.requests[0]), {"comment": "ok"})
        self.assertEqual(self.requests[1].url.path, "/approvals/a-2/reject")
        self.assertEqual(_json_body(self.requests[1]), {"comment": None})


class AuthProfileRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client, self.requests = _make_client(_ok)

    def test_auth_profile_routes(self) -> None:
        self.client.list_auth_profiles()
        self.client.save_auth_profile("s-1", "github")
        self.client.import_auth_profile("/tmp/p.tar.gz", overwrite=True)
        self.assertEqual(self.requests[0].url.path, "/auth-profiles")
        self.assertEqual(self.requests[1].url.path, "/sessions/s-1/auth-profiles")
        self.assertEqual(_json_body(self.requests[1]), {"profile_name": "github"})
        self.assertEqual(self.requests[2].url.path, "/auth-profiles/import")
        self.assertEqual(
            _json_body(self.requests[2]),
            {"archive_path": "/tmp/p.tar.gz", "overwrite": True},
        )


class StreamEventsTests(unittest.TestCase):
    def test_stream_events_parses_data_blocks_and_skips_bad_json(self) -> None:
        class FakeStream:
            status_code = 200

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return None

            def iter_text(self):
                yield 'data: {"a": 1}\n\n'
                yield "data: not-json\n\n"
                yield 'data: {"b": 2}\n'
                yield "\n"

        captured: dict[str, Any] = {}

        def fake_stream(method, url, **kwargs):
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = kwargs.get("headers")
            return FakeStream()

        client = AutoBrowserClient("http://auto-browser.test", token="sekret")
        with patch("auto_browser_client.client.httpx.stream", side_effect=fake_stream):
            events = list(client.stream_events("s-1"))

        self.assertEqual(events, [{"a": 1}, {"b": 2}])
        self.assertEqual(captured["method"], "GET")
        self.assertEqual(captured["url"], "http://auto-browser.test/sessions/s-1/events")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer sekret")


class AsyncClientContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_error_raises_autobrowser_error(self) -> None:
        client = AutoBrowserClient("http://auto-browser.test")

        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"detail": "Not permitted"})

        client._async_client = httpx.AsyncClient(
            base_url=client.base_url,
            transport=httpx.MockTransport(handler),
        )
        try:
            with self.assertRaises(AutoBrowserError) as ctx:
                await client.async_navigate("s-1", "https://example.com")
            self.assertEqual(ctx.exception.status_code, 403)
        finally:
            await client._async_client.aclose()

    async def test_async_context_manager_opens_and_closes_client(self) -> None:
        async with AutoBrowserClient("http://auto-browser.test") as client:
            self.assertIsInstance(client._async_client, httpx.AsyncClient)
        self.assertIsNone(client._async_client)

    async def test_async_session_and_approval_routes(self) -> None:
        requests: list[httpx.Request] = []
        client = AutoBrowserClient("http://auto-browser.test")

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"ok": True})

        client._async_client = httpx.AsyncClient(
            base_url=client.base_url,
            transport=httpx.MockTransport(handler),
        )
        try:
            await client.async_create_session(start_url="https://example.com")
            await client.async_get_session("s-1")
            await client.async_close_session("s-1")
            await client.async_observe("s-1", preset="rich", limit=5)
            await client.async_approve("a-1")
        finally:
            await client._async_client.aclose()

        self.assertEqual(
            [(r.method, r.url.path) for r in requests],
            [
                ("POST", "/sessions"),
                ("GET", "/sessions/s-1"),
                ("DELETE", "/sessions/s-1"),
                ("POST", "/sessions/s-1/observe"),
                ("POST", "/approvals/a-1/approve"),
            ],
        )
        self.assertEqual(_json_body(requests[0]), {"start_url": "https://example.com"})
        self.assertEqual(_json_body(requests[3]), {"preset": "rich", "limit": 5})


if __name__ == "__main__":
    unittest.main()
