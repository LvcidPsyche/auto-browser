from __future__ import annotations

import asyncio
import base64
import hashlib
import unittest
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.browser.page_provenance import PageProvenance
from app.cdp.passthrough import CDPParamsError
from app.mesh.models import PeerRecord
from app.routes.extensions import register_all_routers


class FakeInspector:
    def __init__(self) -> None:
        self._hooks: list[str] = []

    def entries(self, *, limit: int, method: str | None = None, url_contains: str | None = None):
        items = [
            {"method": "GET", "url": "https://example.com/page", "resource_type": "document"},
            {"method": "POST", "url": "https://example.com/api", "resource_type": "xhr"},
        ]
        if method:
            items = [item for item in items if item["method"] == method]
        if url_contains:
            items = [item for item in items if url_contains in item["url"]]
        return items[:limit]

    def summary(self):
        return {"total": 2, "failed": 0, "hooks": len(self._hooks)}

    def register_hook(self, pattern: str, fn) -> None:
        self._hooks.append(pattern)

    def list_hooks(self):
        return list(self._hooks)

    def remove_hook(self, pattern: str) -> bool:
        if pattern in self._hooks:
            self._hooks.remove(pattern)
            return True
        return False


class FakePeerRegistry:
    def __init__(self) -> None:
        self.items = {}

    def all(self):
        return list(self.items.values())

    def add(self, peer) -> None:
        self.items[peer.node_id] = peer


class FakeCdp:
    async def get_element_intelligence(self, selector: str):
        return {"selector": selector, "stable": True}

    async def raw_cdp_command(self, method: str, params: dict):
        if method == "Forbidden.command":
            raise ValueError("blocked")
        if method == "Bad.params":
            raise CDPParamsError("parameter 'x' is not taken")
        return {"method": method, "params": params}


class RoutesExtensionsTests(unittest.TestCase):
    def make_client(self) -> TestClient:
        app = FastAPI()
        app.state.settings = SimpleNamespace(
            operator_id_header="X-Operator-Id",
            operator_name_header="X-Operator-Name",
        )
        app.state.network_inspectors = {"session-1": FakeInspector()}
        # The CDP routes read the session's active page under its lock, and only while it is allowed.
        session = SimpleNamespace(page=SimpleNamespace(url="https://example.com/"), lock=asyncio.Lock())
        app.state.browser_manager = SimpleNamespace(
            sessions={"session-1": session},
            _assert_page_allowed=lambda _s: None,
            _page_refused=lambda _page: False,
            page_provenance=PageProvenance(),
        )
        cdp = FakeCdp()
        cdp.page = session.page
        app.state.cdp_sessions = {"session-1": cdp}
        app.state.peer_registry = FakePeerRegistry()
        app.state.mesh_identity = SimpleNamespace(node_id="node-1", pubkey_b64="pub")
        register_all_routers(app)
        return TestClient(app)

    def test_network_routes_filter_and_manage_hooks(self) -> None:
        client = self.make_client()

        response = client.get("/sessions/session-1/network/requests?method=POST&resource_type=xhr")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["requests"][0]["url"], "https://example.com/api")

        self.assertEqual(client.post("/sessions/session-1/network/hooks", json={}).status_code, 422)
        registered = client.post("/sessions/session-1/network/hooks", json={"url_pattern": "*.json"})
        self.assertEqual(registered.status_code, 200)
        self.assertEqual(client.get("/sessions/session-1/network/hooks").json()["hooks"], ["*.json"])
        removed = client.delete("/sessions/session-1/network/hooks/*.json")
        self.assertTrue(removed.json()["removed"])
        self.assertEqual(client.get("/sessions/missing/network/requests").status_code, 404)

    def test_a_raw_cdp_command_that_is_not_a_string_or_whose_params_are_not_an_object_is_refused(self) -> None:
        # NAV-31: a method that is no string reached the allowlist's `in` (a list raised TypeError: a 500), and params
        # that are no object went to the browser as they were.
        client = self.make_client()
        cases = {
            "a list for the method": {"method": ["DOM.getDocument"]},
            "a number for the method": {"method": 7},
            "a list for the params": {"method": "DOM.getDocument", "params": [1]},
            "a string for the params": {"method": "DOM.getDocument", "params": "depth=1"},
        }
        for name, body in cases.items():
            with self.subTest(name):
                self.assertEqual(client.post("/sessions/session-1/cdp/raw", json=body).status_code, 422)

        missing = client.post("/sessions/session-1/cdp/raw", json={"method": "DOM.getDocument", "params": None})
        self.assertEqual(missing.json(), {"method": "DOM.getDocument", "params": {}})

    def test_a_raw_cdp_parameter_the_command_does_not_take_answers_422_with_the_reason(self) -> None:
        client = self.make_client()

        response = client.post("/sessions/session-1/cdp/raw", json={"method": "Bad.params", "params": {"x": 1}})

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "parameter 'x' is not taken")

    def test_hooks_whose_pattern_contains_a_slash_can_be_removed(self) -> None:
        from urllib.parse import quote

        client = self.make_client()
        for pattern in ("*/api/*", "https://api.example.com/*"):
            with self.subTest(pattern=pattern):
                client.post("/sessions/session-1/network/hooks", json={"url_pattern": pattern})

                removed = client.delete("/sessions/session-1/network/hooks/" + quote(pattern, safe=""))

                self.assertEqual(removed.status_code, 200)
                self.assertTrue(removed.json()["removed"])
                self.assertNotIn(pattern, client.get("/sessions/session-1/network/hooks").json()["hooks"])

    def test_a_hook_pattern_that_is_no_string_is_refused_and_nothing_is_stored(self) -> None:
        # A number was stored as the pattern, and fnmatch raised on it for every finished request of the session: no
        # hook registered after it fired again, and DELETE, whose pattern is text, could not take it out. A list or
        # an object, which the inspector's dict cannot key, answered 500.
        client = self.make_client()
        for pattern in (5, True, 1.5, ["*.json"], {"glob": "*.json"}, None):
            with self.subTest(pattern=pattern):
                answer = client.post("/sessions/session-1/network/hooks", json={"url_pattern": pattern})

                self.assertEqual(answer.status_code, 422, answer.text)
        self.assertEqual(client.get("/sessions/session-1/network/hooks").json()["hooks"], [])

    def test_a_missing_or_an_empty_hook_pattern_is_answered_as_it_was(self) -> None:
        # A null is no pattern either. Typed as a string alone, it was refused as a number is, with a list for its
        # detail where a client had read the text.
        client = self.make_client()
        for body in ({}, {"url_pattern": ""}, {"other": "*.json"}, {"url_pattern": None}):
            with self.subTest(body=body):
                answer = client.post("/sessions/session-1/network/hooks", json=body)

                self.assertEqual((answer.status_code, answer.json()), (422, {"detail": "url_pattern required"}))
        self.assertEqual(client.get("/sessions/session-1/network/hooks").json()["hooks"], [])

    def test_a_hook_pattern_of_the_wrong_type_is_refused_before_its_session_is_looked_for(self) -> None:
        # The body is a model, and a model is judged before the route runs: with a session the controller does not
        # know, a pattern that is no string answers 422 where it answered 404. Any other body meets the session first.
        client = self.make_client()

        refused = client.post("/sessions/missing/network/hooks", json={"url_pattern": 5})
        bodies = ({"url_pattern": "*.json"}, {}, {"url_pattern": None})
        others = [client.post("/sessions/missing/network/hooks", json=body) for body in bodies]

        self.assertEqual(refused.status_code, 422, refused.text)
        self.assertEqual([answer.status_code for answer in others], [404, 404, 404])

    def test_the_real_inspector_keeps_no_pattern_the_route_refused(self) -> None:
        from app.network_inspector import NetworkInspector

        client = self.make_client()
        inspector = client.app.state.network_inspectors["session-1"] = NetworkInspector("session-1")

        refused = client.post("/sessions/session-1/network/hooks", json={"url_pattern": ["*.json"]})
        stored = client.post("/sessions/session-1/network/hooks", json={"url_pattern": "*.json"})

        self.assertEqual((refused.status_code, stored.status_code), (422, 200))
        self.assertEqual(inspector.list_hooks(), ["*.json"])

    def test_mesh_cdp_and_dashboard_routes(self) -> None:
        client = self.make_client()

        # Peers come from the peers file, not from the API: the registry is seeded directly, and
        # the HTTP views only read it (see tests/test_mesh_peer_registration.py).
        key = bytes(range(32))
        peer_id = hashlib.sha256(key).hexdigest()  # a node_id is its key's hash
        client.app.state.peer_registry.add(
            PeerRecord(
                node_id=peer_id,
                pubkey_b64=base64.b64encode(key).decode(),
                endpoint="https://peer.example.com",
                display_name="Peer",
            )
        )
        self.assertEqual(client.get("/mesh/peers").json()["peers"][0]["node_id"], peer_id)
        self.assertEqual(client.post("/mesh/peers", json={"node_id": "peer-2", "pubkey_b64": "pub"}).status_code, 405)
        self.assertIn(client.delete(f"/mesh/peers/{peer_id}").status_code, (404, 405))
        self.assertEqual([p["node_id"] for p in client.get("/mesh/peers").json()["peers"]], [peer_id])
        self.assertEqual(client.get("/mesh/identity").json()["node_id"], "node-1")

        self.assertEqual(client.get("/sessions/session-1/cdp/element?selector=button").json()["selector"], "button")
        self.assertEqual(
            client.post("/sessions/session-1/cdp/raw", json={"method": "Runtime.evaluate"}).status_code, 200
        )
        self.assertEqual(
            client.post("/sessions/session-1/cdp/raw", json={"method": "Forbidden.command"}).status_code, 403
        )

        dashboard = client.get("/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("X-Operator-Id", dashboard.text)


if __name__ == "__main__":
    unittest.main()
