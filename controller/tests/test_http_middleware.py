"""A controller with no token refuses a write a web page sends from another origin (AUTH-24).

The API has no cookies, so CORS was all that stood between a page on another site and a tokenless controller, and CORS
stops a page reading an answer, not sending the request: a form or a no-cors fetch reached POST /maintenance/cleanup and
every action route, and on a request that needs no body nothing else could tell. A browser says where a write came
from, and a request that says nothing (curl, an SDK) is not a page's.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth_policy import BIND_SCOPE_LOOPBACK
from app.mcp_transport import McpHttpTransport
from app.middleware.hosts import origin_matches_host
from app.middleware.http import CROSS_SITE_WRITE_DETAIL, install_controller_http_middleware
from app.routes.mcp import create_mcp_router

BASE_URL = "http://127.0.0.1:8000"
SAME_ORIGIN = BASE_URL
FOREIGN_ORIGIN = "https://evil.example"
LISTED_ORIGIN = "https://console.example"
TOKEN = "t" * 40
REFUSED = {"detail": CROSS_SITE_WRITE_DETAIL}
WRITES = ("post", "put", "patch", "delete")
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
}


def _application(*, token: str | None = None) -> FastAPI:
    application = FastAPI()
    settings = SimpleNamespace(
        api_bearer_token=token,
        api_allow_tokenless_loopback=token is None,
        api_bind_scope=BIND_SCOPE_LOOPBACK,
        model_fields_set=frozenset({"api_bind_scope"}),
        request_rate_limit_exempt_path_list=[],
        operator_id_header="X-Operator-Id",
        operator_name_header="X-Operator-Name",
        require_operator_id=False,
    )

    @application.get("/maintenance/cleanup")
    @application.post("/maintenance/cleanup")
    @application.put("/maintenance/cleanup")
    @application.patch("/maintenance/cleanup")
    @application.delete("/maintenance/cleanup")
    @application.post("/mesh/receive")
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
