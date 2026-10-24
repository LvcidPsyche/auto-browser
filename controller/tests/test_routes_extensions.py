from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

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
        return {"method": method, "params": params}


class RoutesExtensionsTests(unittest.TestCase):
    def make_client(self) -> TestClient:
        app = FastAPI()
        app.state.settings = SimpleNamespace(
            operator_id_header="X-Operator-Id",
            operator_name_header="X-Operator-Name",
        )
        app.state.network_inspectors = {"session-1": FakeInspector()}
        app.state.cdp_sessions = {"session-1": FakeCdp()}
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

    def test_mesh_cdp_and_dashboard_routes(self) -> None:
        client = self.make_client()

        # Peers come from the peers file, not from the API: the registry is seeded directly, and
        # the HTTP views only read it (see tests/test_mesh_peer_registration.py).
        client.app.state.peer_registry.add(
            PeerRecord(node_id="peer-1", pubkey_b64="pub", endpoint="https://peer.example.com", display_name="Peer")
        )
        self.assertEqual(client.get("/mesh/peers").json()["peers"][0]["node_id"], "peer-1")
        self.assertEqual(client.post("/mesh/peers", json={"node_id": "peer-2", "pubkey_b64": "pub"}).status_code, 405)
        self.assertIn(client.delete("/mesh/peers/peer-1").status_code, (404, 405))
        self.assertEqual([p["node_id"] for p in client.get("/mesh/peers").json()["peers"]], ["peer-1"])
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
