"""A controller with no token refuses a write a web page sends from another origin (AUTH-24).

The API has no cookies, so CORS was all that stood between a page on another site and a tokenless controller, and CORS
stops a page reading an answer, not sending the request: a form or a no-cors fetch reached POST /maintenance/cleanup and
every action route, and on a request that needs no body nothing else could tell. A browser says where a write came
from, and a request that says nothing (curl, an SDK) is not a page's.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth_policy import BIND_SCOPE_LOOPBACK
from app.mcp_transport import McpHttpTransport
from app.middleware.hosts import origin_matches_host
from app.middleware.http import (
    CROSS_SITE_WRITE_DETAIL,
    RefusalLogWindows,
    SessionNetworkPeerMiddleware,
    install_controller_http_middleware,
)
from app.routes.auth_profiles import create_auth_profiles_router
from app.routes.mcp import create_mcp_router

BASE_URL = "http://127.0.0.1:8000"
SAME_ORIGIN = BASE_URL
FOREIGN_ORIGIN = "https://evil.example"
LISTED_ORIGIN = "https://console.example"
TOKEN = "t" * 40
MIDDLEWARE_LOGGER = "app.middleware.http"
REFUSED = {"detail": CROSS_SITE_WRITE_DETAIL}
WRITES = ("post", "put", "patch", "delete")
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
}


def _settings(token: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        api_bearer_token=token,
        api_allow_tokenless_loopback=token is None,
        api_bind_scope=BIND_SCOPE_LOOPBACK,
        model_fields_set=frozenset({"api_bind_scope"}),
        request_rate_limit_exempt_path_list=[],
        operator_id_header="X-Operator-Id",
        operator_name_header="X-Operator-Name",
        require_operator_id=False,
    )


def _application(*, token: str | None = None) -> FastAPI:
    application = FastAPI()
    settings = _settings(token)

    @application.get("/maintenance/cleanup")
    @application.post("/maintenance/cleanup")
    @application.put("/maintenance/cleanup")
    @application.patch("/maintenance/cleanup")
    @application.delete("/maintenance/cleanup")
    @application.post("/mesh/receive")
    @application.get("/dashboard")
    @application.get("/auth-profiles/{profile_name}")
    @application.get("/auth-profiles/{profile_name}/export")
    async def served() -> dict[str, bool]:
        return {"ok": True}

    gateway = SimpleNamespace(call_tool=None, list_tools=lambda: [])
    transport = McpHttpTransport(
        tool_gateway=gateway, server_name="t", server_title="t", server_version="1", allowed_origins=[LISTED_ORIGIN]
    )
    application.include_router(create_mcp_router(mcp_transport=transport, tool_gateway=gateway))
    install_controller_http_middleware(
        application, settings=settings, rate_limiter=None, metrics=SimpleNamespace(enabled=False)
    )
    return application


def _client(*, token: str | None = None, base_url: str = BASE_URL) -> TestClient:
    return TestClient(_application(token=token), base_url=base_url)


class CrossSiteWriteRefusedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _client()

    def assert_refused(self, method: str, headers: dict[str, str], path: str = "/maintenance/cleanup") -> None:
        response = self.client.request(method, path, headers=headers)
        self.assertEqual((response.status_code, response.json()), (403, REFUSED), f"{method} {headers}")

    def assert_served(self, method: str, headers: dict[str, str], path: str = "/maintenance/cleanup") -> None:
        response = self.client.request(method, path, headers=headers)
        self.assertEqual((response.status_code, response.json()), (200, {"ok": True}), f"{method} {headers}")

    def test_a_write_a_browser_marks_as_cross_site_or_same_site_is_refused(self) -> None:
        for method in WRITES:
            for site in ("cross-site", "same-site"):
                self.assert_refused(method, {"Sec-Fetch-Site": site})

    def test_a_write_from_a_foreign_origin_is_refused(self) -> None:
        for method in WRITES:
            self.assert_refused(method, {"Origin": FOREIGN_ORIGIN})

    def test_a_write_from_another_port_of_the_same_host_is_refused(self) -> None:
        self.assert_refused("post", {"Origin": "http://127.0.0.1:6080"})

    def test_origin_null_is_a_foreign_origin(self) -> None:
        # A sandboxed iframe, a data: page and a redirected form all send this: it is not this controller's own.
        self.assert_refused("post", {"Origin": "null"})

    def test_an_unparseable_origin_is_refused(self) -> None:
        for origin in (
            "",
            "not a url",
            "http://",
            "//127.0.0.1:8000",
            "127.0.0.1:8000",
            "http:127.0.0.1:8000",
            f"{SAME_ORIGIN}/path",
            f"{SAME_ORIGIN}?q=1",
            f"{SAME_ORIGIN}#f",
            "http://user@127.0.0.1:8000",
            "http://127.0.0.1:port",
            "http://127.0.0.1:8000:8000",
            "http://[::1",
            "ftp://127.0.0.1:8000",
            f"{SAME_ORIGIN} {SAME_ORIGIN}",
        ):
            with self.subTest(origin=origin):
                self.assert_refused("post", {"Origin": origin})

    def test_the_answer_is_the_one_fixed_body_whatever_set_it_off(self) -> None:
        answers = {
            self.client.post("/maintenance/cleanup", headers=headers).text
            for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Origin": FOREIGN_ORIGIN}, {"Origin": "null"})
        }
        self.assertEqual(len(answers), 1, answers)

    def test_the_fixed_body_names_no_origin_it_was_sent(self) -> None:
        self.assertNotIn(
            "evil.example", self.client.post("/maintenance/cleanup", headers={"Origin": FOREIGN_ORIGIN}).text
        )

    def test_the_fixed_body_says_what_works_on_a_controller_that_has_no_token(self) -> None:
        # It said "Send a bearer token", and this layer runs only where no token is configured to check one.
        self.assertNotIn("Send a bearer token", CROSS_SITE_WRITE_DETAIL)
        for words in (
            "API_ALLOW_TOKENLESS_LOOPBACK=true",
            "served from the controller's own origin",
            "needs the controller to have a token",
        ):
            self.assertIn(words, CROSS_SITE_WRITE_DETAIL)

    def test_a_token_sent_to_a_controller_that_has_none_does_not_pass_the_refusal(self) -> None:
        # Why the old advice could not work: nothing checks the token here.
        self.assert_refused("post", {"Sec-Fetch-Site": "cross-site", "Authorization": f"Bearer {TOKEN}"})

    def test_a_write_with_neither_header_is_served(self) -> None:
        # curl and the SDKs send neither.
        for method in WRITES:
            self.assert_served(method, {})

    def test_same_origin_and_none_are_served(self) -> None:
        for site in ("same-origin", "none"):
            self.assert_served("post", {"Sec-Fetch-Site": site})

    def test_the_controllers_own_origin_is_served(self) -> None:
        self.assert_served("post", {"Origin": SAME_ORIGIN, "Sec-Fetch-Site": "same-origin"})

    def test_a_read_is_not_this_layers_to_refuse(self) -> None:
        self.assert_served("get", {"Sec-Fetch-Site": "cross-site", "Origin": FOREIGN_ORIGIN})

    def test_a_same_origin_mark_does_not_excuse_a_foreign_origin(self) -> None:
        self.assert_refused("post", {"Sec-Fetch-Site": "same-origin", "Origin": FOREIGN_ORIGIN})

    def test_the_controllers_own_origin_does_not_excuse_a_cross_site_mark(self) -> None:
        self.assert_refused("post", {"Sec-Fetch-Site": "cross-site", "Origin": SAME_ORIGIN})


class OriginComparedAsABrowserMeansItTests(unittest.TestCase):
    """Through a pure function for the table, and through the whole layer for the cases the brief names."""

    def assert_compared(self, host: str, origin: str, *, same: bool) -> None:
        self.assertEqual(origin_matches_host(origin, host), same, f"{origin} against Host {host}")

    def test_the_host_compares_case_insensitively(self) -> None:
        self.assert_compared("localhost:8000", "http://LocalHost:8000", same=True)
        self.assert_compared("LOCALHOST:8000", "http://localhost:8000", same=True)

    def test_the_scheme_compares_case_insensitively(self) -> None:
        self.assert_compared("localhost:8000", "HTTP://localhost:8000", same=True)

    def test_an_ipv6_host_compares_with_its_brackets(self) -> None:
        self.assert_compared("[::1]:8000", "http://[::1]:8000", same=True)

    def test_an_ipv6_literals_case_is_not_part_of_it(self) -> None:
        self.assert_compared("[fe80::a]:8000", "http://[FE80::A]:8000", same=True)

    def test_an_ipv6_host_on_another_port_is_foreign(self) -> None:
        self.assert_compared("[::1]:8000", "http://[::1]:9000", same=False)

    def test_an_ipv6_host_with_no_port_against_one_with_the_default(self) -> None:
        self.assert_compared("[::1]", "http://[::1]:80", same=True)
        self.assert_compared("[::1]:80", "http://[::1]", same=True)

    def test_an_ipv6_address_is_not_a_name_with_colons(self) -> None:
        self.assert_compared("::1", "http://[::1]", same=False)

    def test_the_default_port_equals_no_port_in_either_direction(self) -> None:
        self.assert_compared("localhost:80", "http://localhost", same=True)
        self.assert_compared("localhost", "http://localhost:80", same=True)
        self.assert_compared("localhost:443", "https://localhost", same=True)
        self.assert_compared("localhost", "https://localhost:443", same=True)

    def test_the_default_port_is_the_origins_schemes(self) -> None:
        self.assert_compared("localhost:80", "https://localhost", same=False)
        self.assert_compared("localhost:443", "http://localhost", same=False)

    def test_a_host_with_no_port_is_not_any_port(self) -> None:
        self.assert_compared("localhost", "http://localhost:8000", same=False)
        self.assert_compared("localhost:8000", "http://localhost", same=False)

    def test_a_loopback_name_is_not_a_loopback_address(self) -> None:
        # Each is its own origin to a browser, though both reach this controller.
        self.assert_compared("127.0.0.1:8000", "http://localhost:8000", same=False)

    def test_a_tls_terminating_proxy_keeps_the_origins_scheme_for_a_host_with_no_port(self) -> None:
        # The controller sees plain HTTP and Host: example; the browser's Origin says https, whose default is 443.
        self.assert_compared("example.com", "https://example.com", same=True)

    def test_a_host_header_that_cannot_be_parsed_matches_no_origin(self) -> None:
        for host in ("", "localhost:port", "[::1", "[::1]evil.com", "localhost:8000:8000"):
            with self.subTest(host=host):
                self.assert_compared(host, "http://localhost:8000", same=False)

    def test_a_digit_python_reads_as_a_digit_and_int_refuses_does_not_raise(self) -> None:
        # U+00B2 is isdigit() and int() raises on it: a Host header reaches this as latin-1.
        self.assert_compared("localhost:²", "http://localhost:8000", same=False)
        self.assert_compared("localhost:8000", "http://localhost:²", same=False)

    def test_the_whole_layer_compares_the_same_way(self) -> None:
        client = TestClient(_application(), base_url=BASE_URL)
        for host, origin, status in (
            ("LOCALHOST:8000", "http://localhost:8000", 200),
            ("[::1]:8000", "http://[::1]:8000", 200),
            ("localhost:80", "http://localhost", 200),
            ("localhost", "http://localhost:80", 200),
            ("localhost:8000", "http://localhost:9000", 403),
            ("[::1]:8000", "http://[::1]:9000", 403),
        ):
            with self.subTest(host=host, origin=origin):
                response = client.post("/maintenance/cleanup", headers={"Host": host, "Origin": origin})
                self.assertEqual(response.status_code, status, response.text)


class WhatHasItsOwnCheckIsNotRefusedHereTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _client()

    def test_mesh_receive_is_not_refused_by_this_layer(self) -> None:
        # Signed by its sender (app/routes/extensions/mesh.py), and no page's request.
        response = self.client.post("/mesh/receive", headers={"Sec-Fetch-Site": "cross-site", "Origin": FOREIGN_ORIGIN})
        self.assertEqual((response.status_code, response.json()), (200, {"ok": True}))

    def test_a_browser_mcp_client_on_a_listed_origin_keeps_working(self) -> None:
        response = self.client.post(
            "/mcp", json=INITIALIZE, headers={"Origin": LISTED_ORIGIN, "Sec-Fetch-Site": "cross-site"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"]["serverInfo"]["name"], "t")

    def test_an_mcp_origin_that_is_not_listed_is_refused_by_the_transport_not_here(self) -> None:
        response = self.client.post(
            "/mcp", json=INITIALIZE, headers={"Origin": FOREIGN_ORIGIN, "Sec-Fetch-Site": "cross-site"}
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["message"], f"Forbidden Origin header: {FOREIGN_ORIGIN}")

    def test_delete_mcp_keeps_the_transports_own_check(self) -> None:
        listed = self.client.delete("/mcp", headers={"Origin": LISTED_ORIGIN, "Sec-Fetch-Site": "cross-site"})
        foreign = self.client.delete("/mcp", headers={"Origin": FOREIGN_ORIGIN, "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(listed.status_code, 400, listed.text)  # past the Origin check: no MCP session header
        self.assertEqual(foreign.json()["error"]["message"], f"Forbidden Origin header: {FOREIGN_ORIGIN}")

    def test_a_path_under_mcp_that_skips_the_transport_is_not_exempt(self) -> None:
        # POST /mcp/tools/call runs a tool and never reaches the transport's Origin check.
        for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Origin": LISTED_ORIGIN}):
            response = self.client.post(
                "/mcp/tools/call", json={"name": "browser.list_sessions", "arguments": {}}, headers=headers
            )
            self.assertEqual((response.status_code, response.json()), (403, REFUSED), headers)

    def test_a_path_that_only_starts_like_an_exempt_one_is_not_exempt(self) -> None:
        for path in ("/mesh/receive/x", "/mcpx"):
            response = self.client.post(path, headers={"Sec-Fetch-Site": "cross-site"})
            self.assertEqual((response.status_code, response.json()), (403, REFUSED), path)


class TheExportIsAWriteTests(unittest.TestCase):
    """GET /auth-profiles/{name}/export writes an archive, its sidecar, an audit event and a receipt, and a profile's
    name can be guessed where a session id cannot. A cross-site GET usually has no Origin: Sec-Fetch-Site says so."""

    PATH = "/auth-profiles/shop/export"

    def setUp(self) -> None:
        self.client = _client()

    def assert_refused(self, headers: dict[str, str], path: str = PATH) -> None:
        response = self.client.get(path, headers=headers)
        self.assertEqual((response.status_code, response.json()), (403, REFUSED), f"{path} {headers}")

    def assert_served(self, headers: dict[str, str], path: str = PATH) -> None:
        response = self.client.get(path, headers=headers)
        self.assertEqual((response.status_code, response.json()), (200, {"ok": True}), f"{path} {headers}")

    def test_a_get_a_browser_marks_as_cross_site_or_same_site_is_refused(self) -> None:
        for site in ("cross-site", "same-site"):
            self.assert_refused({"Sec-Fetch-Site": site})

    def test_a_get_from_a_foreign_origin_is_refused(self) -> None:
        self.assert_refused({"Origin": FOREIGN_ORIGIN})

    def test_a_get_with_neither_header_is_served(self) -> None:
        self.assert_served({})

    def test_a_get_from_the_controllers_own_pages_or_the_address_bar_is_served(self) -> None:
        self.assert_served({"Sec-Fetch-Site": "same-origin"})
        self.assert_served({"Sec-Fetch-Site": "none"})
        self.assert_served({"Sec-Fetch-Site": "same-origin", "Origin": SAME_ORIGIN})

    def test_a_cross_site_get_of_anything_else_is_served(self) -> None:
        # A link to the dashboard from another site still opens it.
        for path in ("/dashboard", "/auth-profiles/shop", "/auth-profiles/export"):
            self.assert_served({"Sec-Fetch-Site": "cross-site", "Origin": FOREIGN_ORIGIN}, path)

    def test_what_only_starts_or_ends_like_the_route_is_not_it(self) -> None:
        for path in ("/auth-profiles/shop/exports", "/auth-profiles/a/b/export", "/x/auth-profiles/shop/export"):
            response = self.client.get(path, headers={"Sec-Fetch-Site": "cross-site"})
            self.assertEqual(response.status_code, 404, path)

    def test_everything_the_router_routes_to_it_is_refused(self) -> None:
        # The router's pattern ends in `$`, so a trailing newline routes to the export too.
        path = "/auth-profiles/shop/export%0A"
        self.assert_served({}, path)
        self.assert_refused({"Sec-Fetch-Site": "cross-site"}, path)

    def test_a_controller_with_a_token_leaves_the_token_to_decide(self) -> None:
        client = _client(token=TOKEN)
        headers = {"Authorization": f"Bearer {TOKEN}", "Sec-Fetch-Site": "cross-site"}
        self.assertEqual(client.get(self.PATH, headers=headers).status_code, 200)
        self.assertEqual(client.get(self.PATH, headers={"Sec-Fetch-Site": "cross-site"}).status_code, 401)

    def test_the_route_itself_is_refused_before_anything_is_written(self) -> None:
        export = AsyncMock(return_value={"archive_name": "x"})
        application = FastAPI()
        application.include_router(
            create_auth_profiles_router(manager=SimpleNamespace(export_auth_profile=export), settings=SimpleNamespace())
        )
        install_controller_http_middleware(
            application, settings=_settings(None), rate_limiter=None, metrics=SimpleNamespace(enabled=False)
        )

        response = TestClient(application, base_url=BASE_URL).get(self.PATH, headers={"Sec-Fetch-Site": "cross-site"})

        self.assertEqual((response.status_code, response.json()), (403, REFUSED))
        export.assert_not_called()


class ARootPathIsNotPartOfThePathComparedTests(unittest.TestCase):
    """Behind a prefix (`uvicorn --root-path`) the path carries it and the router matches what follows it."""

    def setUp(self) -> None:
        self.client = TestClient(_application(), base_url=BASE_URL, root_path="/api")

    def test_a_browser_mcp_client_on_a_listed_origin_keeps_working(self) -> None:
        response = self.client.post(
            "/api/mcp", json=INITIALIZE, headers={"Origin": LISTED_ORIGIN, "Sec-Fetch-Site": "cross-site"}
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_an_mcp_origin_that_is_not_listed_is_refused_by_the_transport_not_here(self) -> None:
        response = self.client.post(
            "/api/mcp", json=INITIALIZE, headers={"Origin": FOREIGN_ORIGIN, "Sec-Fetch-Site": "cross-site"}
        )
        self.assertEqual(response.json()["error"]["message"], f"Forbidden Origin header: {FOREIGN_ORIGIN}")

    def test_mesh_receive_is_not_refused_by_this_layer(self) -> None:
        response = self.client.post("/api/mesh/receive", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual((response.status_code, response.json()), (200, {"ok": True}))

    def test_a_path_under_mcp_stays_checked(self) -> None:
        response = self.client.post(
            "/api/mcp/tools/call", json={"name": "browser.list_sessions"}, headers={"Sec-Fetch-Site": "cross-site"}
        )
        self.assertEqual((response.status_code, response.json()), (403, REFUSED))

    def test_the_export_is_refused(self) -> None:
        response = self.client.get("/api/auth-profiles/shop/export", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual((response.status_code, response.json()), (403, REFUSED))

    def test_a_path_the_router_reads_without_the_root_path_is_compared_as_it_reads_it(self) -> None:
        response = self.client.post("/mcp", json=INITIALIZE, headers={"Origin": LISTED_ORIGIN})
        self.assertEqual(response.status_code, 200, response.text)

    def test_a_path_that_only_starts_with_the_root_path_is_not_under_it(self) -> None:
        response = self.client.post("/apimcp", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual((response.status_code, response.json()), (403, REFUSED))

    def test_the_root_path_itself_is_no_exempt_path(self) -> None:
        client = TestClient(_application(), base_url=BASE_URL, root_path="/mcp")
        response = client.post("/mcp", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual((response.status_code, response.json()), (403, REFUSED))


class McpWithATrailingSlashTests(unittest.TestCase):
    """POST /mcp/ is Starlette's redirect to /mcp, which does nothing itself and lands on the transport's own check."""

    def setUp(self) -> None:
        self.client = _client()

    def test_it_answers_the_routers_redirect_to_the_transport(self) -> None:
        for method in ("post", "delete"):
            for origin in (LISTED_ORIGIN, FOREIGN_ORIGIN):
                response = self.client.request(
                    method,
                    "/mcp/",
                    headers={"Origin": origin, "Sec-Fetch-Site": "cross-site"},
                    follow_redirects=False,
                )
                self.assertEqual(response.status_code, 307, response.text)
                self.assertEqual(response.headers["location"], f"{BASE_URL}/mcp")

    def test_the_redirect_lands_on_the_transports_own_check(self) -> None:
        listed = self.client.post(
            "/mcp/", json=INITIALIZE, headers={"Origin": LISTED_ORIGIN, "Sec-Fetch-Site": "cross-site"}
        )
        foreign = self.client.post(
            "/mcp/", json=INITIALIZE, headers={"Origin": FOREIGN_ORIGIN, "Sec-Fetch-Site": "cross-site"}
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(foreign.json()["error"]["message"], f"Forbidden Origin header: {FOREIGN_ORIGIN}")

    def test_it_is_exempt_under_a_root_path_too(self) -> None:
        client = TestClient(_application(), base_url=BASE_URL, root_path="/api")
        response = client.post("/api/mcp/", headers={"Sec-Fetch-Site": "cross-site"}, follow_redirects=False)
        self.assertEqual(response.status_code, 307, response.text)


class AProxyInFrontOfATokenlessControllerTests(unittest.TestCase):
    """The browser's Origin names the proxy, and only the Host it passes on can agree with it."""

    SAME_ORIGIN_THROUGH_PROXY = {"Origin": "http://localhost:8080", "Sec-Fetch-Site": "same-origin"}

    def setUp(self) -> None:
        self.client = _client()

    def test_a_proxy_that_rewrites_host_is_refused(self) -> None:
        response = self.client.post(
            "/maintenance/cleanup", headers={**self.SAME_ORIGIN_THROUGH_PROXY, "Host": "127.0.0.1:8000"}
        )
        self.assertEqual((response.status_code, response.json()), (403, REFUSED))

    def test_a_proxy_that_passes_the_browsers_host_through_is_served(self) -> None:
        response = self.client.post(
            "/maintenance/cleanup", headers={**self.SAME_ORIGIN_THROUGH_PROXY, "Host": "localhost:8080"}
        )
        self.assertEqual((response.status_code, response.json()), (200, {"ok": True}))


class TheRefusalsLogLineTests(unittest.TestCase):
    """What a page on another origin can do to the controller's log: write a line of its own, or fill it."""

    def setUp(self) -> None:
        self.client = _client()

    def refuse(self, path: str = "/maintenance/cleanup") -> None:
        response = self.client.post(path, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual((response.status_code, response.json()), (403, REFUSED))

    def test_a_path_holding_line_breaks_cannot_start_a_line_of_the_log(self) -> None:
        # The path is logged decoded: %0A and %0D arrive as the characters, and with %s they ended the line.
        with self.assertLogs(MIDDLEWARE_LOGGER, level="WARNING") as logs:
            self.refuse("/x/a%0AWARNING:app:forged by a page%0D%0Aline three%1B[2J")

        self.assertEqual(len(logs.records), 1)
        message = logs.records[0].getMessage()
        for character in ("\n", "\r", "\x1b"):
            self.assertNotIn(character, message)
        self.assertIn("forged by a page", message)

    def test_a_flood_from_one_client_writes_one_line_and_the_next_window_says_how_many_it_held_back(self) -> None:
        with self.assertLogs(MIDDLEWARE_LOGGER, level="WARNING") as logs:
            for _ in range(5):
                self.refuse()
            self.assertEqual(len(logs.records), 1, logs.output)
            self.assertNotIn("more", logs.records[0].getMessage())
            with patch("app.utils.REFUSAL_LOG_WINDOW_SECONDS", 0):  # the window is over
                self.refuse()

        self.assertEqual(len(logs.records), 2, logs.output)
        self.assertIn("4 more", logs.records[1].getMessage())

    def test_a_refusal_after_a_quiet_window_is_a_plain_line_again(self) -> None:
        with self.assertLogs(MIDDLEWARE_LOGGER, level="WARNING") as logs:
            self.refuse()
            with patch("app.utils.REFUSAL_LOG_WINDOW_SECONDS", 0):
                self.refuse()

        self.assertEqual(len(logs.records), 2, logs.output)
        self.assertNotIn("more", logs.records[1].getMessage())

    def test_the_sources_remembered_are_bounded(self) -> None:
        windows = RefusalLogWindows()

        with patch("app.utils.REFUSAL_LOG_SOURCES_MAX", 2):
            for source in ("a", "b", "c"):
                self.assertEqual(windows.due(source, now=0.0), 0)
            self.assertIsNone(windows.due("c", now=1.0))
            self.assertIsNone(windows.due("b", now=1.0))
            # The oldest was forgotten, with whatever it had counted: it logs as new.
            self.assertEqual(windows.due("a", now=1.0), 0)

    def test_each_client_has_a_window_of_its_own(self) -> None:
        # One window for every client would pass the flood test above: a second client's first refusal is a line.
        application = _application()
        first, second = (
            TestClient(application, base_url=BASE_URL, client=(address, 50000)) for address in ("10.0.0.1", "10.0.0.2")
        )
        cross_site = {"Sec-Fetch-Site": "cross-site"}

        with self.assertLogs(MIDDLEWARE_LOGGER, level="WARNING") as logs:
            for _ in range(3):
                first.post("/maintenance/cleanup", headers=cross_site)
            for _ in range(2):
                second.post("/maintenance/cleanup", headers=cross_site)
            self.assertEqual(len(logs.records), 2, logs.output)
            with patch("app.utils.REFUSAL_LOG_WINDOW_SECONDS", 0):
                second.post("/maintenance/cleanup", headers=cross_site)

        self.assertIn("1 more", logs.records[2].getMessage())  # its own count: the other client held back 2

    def test_a_path_of_any_length_is_cut_in_the_line(self) -> None:
        # One refused request wrote a line as long as the server lets a request line be: megabytes, over h11.
        with self.assertLogs(MIDDLEWARE_LOGGER, level="WARNING") as logs:
            self.refuse("/x/" + "p" * 5000)

        message = logs.records[0].getMessage()
        self.assertIn("/x/pppp", message)
        self.assertLess(len(message), 400)


LOOPBACK_ONLY = "Invalid host header: this controller runs without an API credential"


class TheHostRefusalsLogLineTests(unittest.TestCase):
    """The same for a request whose Host is no loopback name: a rebinding page's requests, as many as it sends."""

    def refuse(self, client: TestClient, host: str = "rebound.example") -> None:
        response = client.get("/dashboard", headers={"Host": host})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn(LOOPBACK_ONLY, response.json()["detail"])

    def test_fifty_refused_requests_write_one_line_and_the_next_window_says_how_many_it_held_back(self) -> None:
        client = _client()

        with self.assertLogs(MIDDLEWARE_LOGGER, level="WARNING") as logs:
            for _ in range(50):
                self.refuse(client)
            self.assertEqual(len(logs.records), 1, logs.output[:3])
            self.assertNotIn("more", logs.records[0].getMessage())
            with patch("app.utils.REFUSAL_LOG_WINDOW_SECONDS", 0):
                self.refuse(client)

        self.assertEqual(len(logs.records), 2, logs.output)
        self.assertIn("49 more", logs.records[1].getMessage())

    def test_each_client_has_a_window_of_its_own(self) -> None:
        application = _application()
        first, second = (
            TestClient(application, base_url=BASE_URL, client=(address, 50000)) for address in ("10.0.0.1", "10.0.0.2")
        )

        with self.assertLogs(MIDDLEWARE_LOGGER, level="WARNING") as logs:
            for _ in range(3):
                self.refuse(first)
            self.refuse(second)

        self.assertEqual(len(logs.records), 2, logs.output)

    def test_a_refused_host_and_a_refused_write_do_not_share_a_window(self) -> None:
        # Two refusals, two lines: a client's cross-site write is not held back because its Host was just refused.
        client = _client()

        with self.assertLogs(MIDDLEWARE_LOGGER, level="WARNING") as logs:
            self.refuse(client)
            client.post("/maintenance/cleanup", headers={"Sec-Fetch-Site": "cross-site"})

        self.assertEqual(len(logs.records), 2, logs.output)

    def test_a_host_of_any_length_is_cut_in_the_line(self) -> None:
        with self.assertLogs(MIDDLEWARE_LOGGER, level="WARNING") as logs:
            self.refuse(_client(), "h" * 5000 + ".example")

        message = logs.records[0].getMessage()
        self.assertIn("hhhh", message)
        self.assertLess(len(message), 400)


class ASessionNetworkPeersLogLineTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    async def refuse(middleware: Any, *, peer: str = "172.30.0.5", path: str = "/sessions", kind: str = "http") -> None:
        await middleware({"type": kind, "client": (peer, 1234), "path": path, "headers": []}, AsyncMock(), AsyncMock())

    async def test_fifty_refused_requests_from_one_peer_write_one_line_and_the_next_says_how_many_it_held_back(
        self,
    ) -> None:
        # This layer answers ahead of the rate limiter, and a page in a session's browser sends what it likes.
        downstream = AsyncMock()
        middleware = SessionNetworkPeerMiddleware(downstream, refuses_peer=lambda host: True)

        with self.assertLogs(MIDDLEWARE_LOGGER, level="WARNING") as logs:
            for number in range(50):
                await self.refuse(middleware, kind="websocket" if number % 10 == 9 else "http")
            self.assertEqual(len(logs.records), 1, logs.output[:3])
            self.assertNotIn("more", logs.records[0].getMessage())
            with patch("app.utils.REFUSAL_LOG_WINDOW_SECONDS", 0):  # the window is over
                await self.refuse(middleware)

        self.assertEqual(len(logs.records), 2, logs.output)
        self.assertIn("49 more", logs.records[1].getMessage())
        downstream.assert_not_awaited()  # each of them was refused all the same

    async def test_each_peer_has_a_window_of_its_own(self) -> None:
        middleware = SessionNetworkPeerMiddleware(AsyncMock(), refuses_peer=lambda host: True)

        with self.assertLogs(MIDDLEWARE_LOGGER, level="WARNING") as logs:
            for _ in range(3):
                await self.refuse(middleware, peer="172.30.0.5")
            for _ in range(2):
                await self.refuse(middleware, peer="172.30.1.5")
            self.assertEqual(len(logs.records), 2, logs.output)
            with patch("app.utils.REFUSAL_LOG_WINDOW_SECONDS", 0):
                await self.refuse(middleware, peer="172.30.1.5")

        self.assertIn("172.30.1.5", logs.records[2].getMessage())
        self.assertIn("1 more", logs.records[2].getMessage())

    async def test_a_path_of_any_length_is_cut_in_the_line(self) -> None:
        middleware = SessionNetworkPeerMiddleware(AsyncMock(), refuses_peer=lambda host: True)

        with self.assertLogs(MIDDLEWARE_LOGGER, level="WARNING") as logs:
            await self.refuse(middleware, path="/x/" + "p" * 5000)

        message = logs.records[0].getMessage()
        self.assertIn("/x/pppp", message)
        self.assertLess(len(message), 400)

    async def test_a_path_holding_line_breaks_cannot_start_a_line_of_the_log(self) -> None:
        # A page in a session's browser chooses the path, and this refusal logs it.
        sent: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        middleware = SessionNetworkPeerMiddleware(AsyncMock(), refuses_peer=lambda host: True)
        scope = {"type": "http", "client": ("172.30.0.5", 1234), "path": "/x\nWARNING:app:forged\rline", "headers": []}

        with self.assertLogs(MIDDLEWARE_LOGGER, level="WARNING") as logs:
            await middleware(scope, AsyncMock(), send)

        self.assertEqual(sent[0]["status"], 403)
        message = logs.records[0].getMessage()
        self.assertNotIn("\n", message)
        self.assertNotIn("\r", message)
        self.assertIn("forged", message)


class OnlyATokenlessControllerRefusesTests(unittest.TestCase):
    def test_a_controller_with_a_token_leaves_the_token_to_decide(self) -> None:
        client = _client(token=TOKEN)
        headers = {"Authorization": f"Bearer {TOKEN}", "Sec-Fetch-Site": "cross-site", "Origin": FOREIGN_ORIGIN}
        response = client.post("/maintenance/cleanup", headers=headers)
        self.assertEqual((response.status_code, response.json()), (200, {"ok": True}))

    def test_without_the_token_it_is_the_tokens_401_not_this_layers_403(self) -> None:
        response = _client(token=TOKEN).post("/maintenance/cleanup", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(response.status_code, 401, response.text)


if __name__ == "__main__":
    unittest.main()
