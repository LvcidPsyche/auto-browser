"""The controller's Host-header rules: DNS-rebinding defence and IPv6 parsing.

A tokenless controller is only safe on loopback if it also refuses requests
addressed to other names — otherwise a web page whose hostname re-resolves to
127.0.0.1 becomes a same-origin client of an API that asks for no credential.
Compose always set CONTROLLER_ALLOWED_HOSTS; running the controller directly
left it empty, and empty meant every Host was accepted.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

from app.auth_policy import is_loopback_host
from app.config import Settings
from app.middleware import hosts
from app.middleware.hosts import ControllerHostMiddleware, host_from_header, host_is_allowed
from app.middleware.http import install_controller_http_middleware

TOKEN = "t" * 40
# A page whose name its owner rebinds to 127.0.0.1: its requests are same-origin, so only the Host says what it is.
REBOUND = {"host": "attacker.example:8000", "origin": "http://attacker.example:8000", "sec-fetch-site": "same-origin"}
# A REST route, the MCP endpoint and the REST door to MCP tools: stand-ins, as only the middleware is under test.
ROUTES = (("GET", "/sessions"), ("POST", "/mcp"), ("POST", "/mcp/tools/call"))


def _app(**overrides) -> tuple[FastAPI, Settings]:
    application = FastAPI()

    @application.get("/sessions")
    def sessions() -> dict:
        return {"ok": True}

    @application.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @application.post("/mcp")
    def mcp() -> dict:
        return {"ok": True}

    @application.post("/mcp/tools/call")
    def mcp_tools_call() -> dict:
        return {"ok": True}

    @application.websocket("/ws")
    async def stand_in_websocket(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("accepted")
        await websocket.close()

    settings = Settings(_env_file=None, **overrides)
    install_controller_http_middleware(
        application,
        settings=settings,
        rate_limiter=None,
        metrics=SimpleNamespace(enabled=False),
    )
    return application, settings


def _client(**overrides) -> TestClient:
    return TestClient(_app(**overrides)[0])


def _tokenless_client(allowed_hosts: str) -> TestClient:
    return _client(
        API_BIND_SCOPE="loopback", CONTROLLER_ALLOWED_HOSTS=allowed_hosts, API_ALLOW_TOKENLESS_LOOPBACK="true"
    )


class HostParsingTests(unittest.TestCase):
    def test_host_part_is_extracted_from_every_header_shape(self) -> None:
        self.assertEqual(host_from_header("[::1]:8000"), "::1")
        self.assertEqual(host_from_header("[::1]"), "::1")
        self.assertEqual(host_from_header("127.0.0.1:8000"), "127.0.0.1")
        self.assertEqual(host_from_header("Controller.Example.COM"), "controller.example.com")
        self.assertEqual(host_from_header(""), "")

    def test_text_after_the_bracket_is_not_a_loopback_host(self) -> None:
        # "[::1]evil.com" parsed as "::1" and passed the loopback and allowed-host checks.
        for header in ("[::1]evil.com", "[::1].evil.com:80", "[::1]x]:80", "[::1", "[::1]:", "[::1]:80x"):
            with self.subTest(header=header):
                self.assertEqual(host_from_header(header), "")
                self.assertFalse(host_is_allowed(header, ["::1"]))
                self.assertFalse(is_loopback_host(header))
        for header in ("[::1]", "[::1]:8000"):
            with self.subTest(header=header):
                self.assertEqual(host_from_header(header), "::1")
                self.assertTrue(is_loopback_host(header))

    def test_ipv6_loopback_matches_the_entry_compose_ships(self) -> None:
        # Starlette split on the first colon, so "[::1]:8000" became "[" and
        # the ::1 entry in the shipped CONTROLLER_ALLOWED_HOSTS never matched.
        self.assertTrue(host_is_allowed("[::1]:8000", ["localhost", "127.0.0.1", "::1"]))
        self.assertTrue(host_is_allowed("[::1]:8000", ["[::1]"]))

    def test_wildcards_match_subdomains_only(self) -> None:
        self.assertTrue(host_is_allowed("a.example.com", ["*.example.com"]))
        self.assertFalse(host_is_allowed("example.com", ["*.example.com"]))
        self.assertFalse(host_is_allowed("evilexample.com", ["*.example.com"]))
        self.assertTrue(host_is_allowed("anything", ["*"]))

    def test_malformed_wildcards_are_rejected_at_startup(self) -> None:
        with self.assertRaises(ValueError):
            ControllerHostMiddleware(FastAPI(), allowed_hosts=["example.*"], requires_credential=lambda: True)
        with self.assertRaises(ValueError):
            ControllerHostMiddleware(FastAPI(), allowed_hosts=["*example.com"], requires_credential=lambda: True)

    def test_a_bracket_round_anything_but_an_ipv6_address_and_a_bare_star_dot_are_refused_at_startup(self) -> None:
        # The brackets were stripped whatever they held, so "[*]" and "[*.example.com]" were wildcards the check
        # passed, and "*." admitted every name written with its trailing dot ("attacker.example.:8000").
        # Through the app's own install, on a controller with a token, so no other rule refuses them: the middleware
        # is built at the app's first ASGI call, its lifespan's start.
        for pattern in ("[*]", "[*.example.com]", "*.", "[controller.example]", "[127.0.0.1]", "[]", "[::1", "::1]"):
            with self.subTest(pattern=pattern):
                application, _ = _app(API_BEARER_TOKEN=TOKEN, CONTROLLER_ALLOWED_HOSTS=f"localhost,{pattern}")
                with self.assertRaisesRegex(ValueError, "invalid host pattern"):
                    with TestClient(application):
                        pass
        for pattern in ("[::1]", "[FE80::1]", "::1", "*", "*.example.com", "localhost", "127.0.0.1"):
            with self.subTest(pattern=pattern):
                application, _ = _app(API_BEARER_TOKEN=TOKEN, CONTROLLER_ALLOWED_HOSTS=f"localhost,{pattern}")
                with TestClient(application) as client:
                    self.assertEqual(client.get("/healthz", headers={"host": "localhost"}).status_code, 200)

    def test_every_spelling_of_a_wildcard_is_one(self) -> None:
        for pattern in ("*", "*.example.com", "*.EXAMPLE.com", " *.example.com ", "[*]", "[*.example.com]", "*."):
            with self.subTest(pattern=pattern):
                self.assertTrue(hosts.is_wildcard_pattern(pattern))
        for pattern in ("localhost", "controller.example", "[::1]", "::1", "127.0.0.1"):
            with self.subTest(pattern=pattern):
                self.assertFalse(hosts.is_wildcard_pattern(pattern))


class TokenlessLoopbackHostGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _client(
            API_BIND_SCOPE="loopback", CONTROLLER_ALLOWED_HOSTS="", API_ALLOW_TOKENLESS_LOOPBACK="true"
        )

    def test_a_rebound_hostname_is_refused(self) -> None:
        response = self.client.get("/sessions", headers={"host": "attacker.example:8000"})

        self.assertEqual(response.status_code, 400)
        detail = response.json()["detail"]
        self.assertIn("CONTROLLER_ALLOWED_HOSTS", detail)
        # What works, and not a wildcard: without a token, the controller refuses one there.
        self.assertIn("exact host names", detail)
        self.assertIn("wildcard", detail)

    def test_with_an_empty_list_healthz_answers_any_host_and_no_other_path_does(self) -> None:
        self.assertEqual(self.client.get("/healthz", headers=REBOUND).status_code, 200)
        for method, path in ROUTES:
            with self.subTest(path=path):
                self.assertEqual(self.client.request(method, path, headers=REBOUND).status_code, 400)

    def test_loopback_names_are_served(self) -> None:
        for host in ("127.0.0.1:8000", "localhost:8000", "localhost", "[::1]:8000"):
            with self.subTest(host=host):
                self.assertEqual(self.client.get("/sessions", headers={"host": host}).status_code, 200)

    def test_healthz_stays_reachable_for_orchestrators(self) -> None:
        response = self.client.get("/healthz", headers={"host": "controller:8000"})

        self.assertEqual(response.status_code, 200)


class HostGuardScopeTests(unittest.TestCase):
    def test_a_bearer_credential_lifts_the_loopback_only_rule(self) -> None:
        client = _client(API_BIND_SCOPE="loopback", CONTROLLER_ALLOWED_HOSTS="", API_BEARER_TOKEN=TOKEN)

        refused = client.get("/sessions", headers={"host": "controller.internal"})
        allowed = client.get(
            "/sessions",
            headers={"host": "controller.internal", "authorization": f"Bearer {TOKEN}"},
        )

        self.assertEqual(refused.status_code, 401)
        self.assertEqual(allowed.status_code, 200)

    def test_without_the_escape_hatch_every_host_needs_the_token(self) -> None:
        # Loopback with no credential is no longer tokenless: the auth gate refuses
        # before the Host guard is asked, whatever name the request uses.
        client = _client(API_BIND_SCOPE="loopback", CONTROLLER_ALLOWED_HOSTS="", API_ALLOW_TOKENLESS_LOOPBACK="false")

        for host in ("attacker.example:8000", "localhost:8000", "127.0.0.1"):
            with self.subTest(host=host):
                self.assertEqual(client.get("/sessions", headers={"host": host}).status_code, 401)

    def test_configured_hosts_replace_the_loopback_default(self) -> None:
        client = _client(
            API_BIND_SCOPE="loopback",
            CONTROLLER_ALLOWED_HOSTS="controller.internal",
            API_ALLOW_TOKENLESS_LOOPBACK="true",
        )

        self.assertEqual(client.get("/sessions", headers={"host": "controller.internal"}).status_code, 200)
        self.assertEqual(client.get("/sessions", headers={"host": "127.0.0.1"}).status_code, 400)
        self.assertEqual(client.get("/sessions", headers={"host": "attacker.example"}).status_code, 400)


class TokenlessWildcardHostTests(unittest.TestCase):
    """On a controller that requires no credential, a wildcard in CONTROLLER_ALLOWED_HOSTS admits no Host.

    A page that rebinds a name under the wildcard to 127.0.0.1 was admitted, and drove the controller whole. The start
    refuses such a list (runtime_policy); this is the rule an app built without its lifespan still keeps.
    """

    def test_a_rebound_host_is_refused_under_a_star_and_under_a_suffix(self) -> None:
        for allowed in ("*", "localhost,127.0.0.1,::1,*.example"):
            client = _tokenless_client(allowed)
            for method, path in ROUTES:
                with self.subTest(allowed=allowed, path=path):
                    self.assertEqual(client.request(method, path, headers=REBOUND).status_code, 400)

    def test_exact_names_and_loopback_are_still_served_beside_a_wildcard(self) -> None:
        client = _tokenless_client("localhost,127.0.0.1,::1,controller.internal,*.example")

        for host in ("localhost:8000", "127.0.0.1:8000", "[::1]:8000", "controller.internal"):
            for method, path in ROUTES:
                with self.subTest(host=host, path=path):
                    self.assertEqual(client.request(method, path, headers={"host": host}).status_code, 200)

    def test_a_list_of_wildcards_only_admits_no_host_without_a_token(self) -> None:
        # The start refuses this list; an app built without its lifespan fails closed, /healthz included.
        client = _tokenless_client("*")

        for host in ("localhost:8000", "127.0.0.1"):
            for path in ("/sessions", "/healthz"):
                with self.subTest(host=host, path=path):
                    self.assertEqual(client.get(path, headers={"host": host}).status_code, 400)

    def test_a_token_lets_the_wildcard_admit_again_read_per_request(self) -> None:
        application, settings = _app(
            API_BIND_SCOPE="loopback", CONTROLLER_ALLOWED_HOSTS="*", API_ALLOW_TOKENLESS_LOOPBACK="true"
        )
        client = TestClient(application)
        self.assertEqual(client.get("/sessions", headers=REBOUND).status_code, 400)

        with patch.object(settings, "api_bearer_token", TOKEN):
            # Admitted by its Host, then asked for the token the controller now has.
            self.assertEqual(client.get("/sessions", headers=REBOUND).status_code, 401)
            authorized = {**REBOUND, "authorization": f"Bearer {TOKEN}"}
            for method, path in ROUTES:
                with self.subTest(path=path):
                    self.assertEqual(client.request(method, path, headers=authorized).status_code, 200)

        self.assertEqual(client.get("/sessions", headers=REBOUND).status_code, 400)

    def test_a_websocket_with_a_rebound_host_is_refused_under_a_wildcard(self) -> None:
        # The other auth and origin layers are HTTP middleware and pass a WebSocket scope through; this one does not.
        client = _tokenless_client("localhost,*")

        with self.assertRaises(WebSocketDenialResponse) as refused:
            with client.websocket_connect("/ws", headers={"host": "attacker.example:8000"}):
                pass
        self.assertEqual(refused.exception.status_code, 400)
        with client.websocket_connect("/ws", headers={"host": "localhost:8000"}) as connection:
            self.assertEqual(connection.receive_text(), "accepted")


if __name__ == "__main__":
    unittest.main()
