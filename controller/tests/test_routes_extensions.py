from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

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

    def remove(self, node_id: str) -> bool:
        return self.items.pop(node_id, None) is not None


class FakeCdp:
    async def get_element_intelligence(self, selector: str):
        return {"selector": selector, "stable": True}

    async def raw_cdp_command(self, method: str, params: dict):
        if method == "Forbidden.command":
            raise ValueError("blocked")
        return {"method": method, "params": params}


class FakeWorkflowRun(SimpleNamespace):
    def __init__(self) -> None:
        super().__init__(
            run_id="run-1",
            status=SimpleNamespace(value="completed"),
            step_statuses={"step-1": SimpleNamespace(value="completed")},
            context={"ok": True},
            error=None,
        )


class FakeWorkflowEngine:
    def __init__(self) -> None:
        self.run = AsyncMock(return_value=FakeWorkflowRun())
        self._runs = [{"run_id": "run-1", "workflow_id": "fixture", "status": "completed"}]

    def list_runs(self, workflow_id: str = ""):
        if workflow_id:
            return [run for run in self._runs if run["workflow_id"] == workflow_id]
        return list(self._runs)


class RoutesExtensionsTests(unittest.TestCase):
    def make_client(self) -> TestClient:
        app = FastAPI()
        app.state.settings = SimpleNamespace(
            operator_id_header="X-Operator-Id",
            operator_name_header="X-Operator-Name",
        )
        app.state.network_inspectors = {"session-1": FakeInspector()}
        app.state.cdp_sessions = {"session-1": FakeCdp()}
        app.state.workflow_engine = FakeWorkflowEngine()
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

    def test_mesh_cdp_workflow_and_dashboard_routes(self) -> None:
        client = self.make_client()

        peer = {
            "node_id": "peer-1",
            "pubkey_b64": "pub",
            "endpoint": "https://peer.example.com",
            "display_name": "Peer",
            "grants": [],
        }
        self.assertEqual(client.post("/mesh/peers", json=peer).json()["status"], "added")
        self.assertEqual(client.get("/mesh/peers").json()["peers"][0]["node_id"], "peer-1")
        self.assertEqual(client.delete("/mesh/peers/peer-1").json()["status"], "removed")
        self.assertEqual(client.delete("/mesh/peers/missing").status_code, 404)
        self.assertEqual(client.get("/mesh/identity").json()["node_id"], "node-1")

        self.assertEqual(client.get("/sessions/session-1/cdp/element?selector=button").json()["selector"], "button")
        self.assertEqual(client.post("/sessions/session-1/cdp/raw", json={"method": "Runtime.evaluate"}).status_code, 200)
        self.assertEqual(client.post("/sessions/session-1/cdp/raw", json={"method": "Forbidden.command"}).status_code, 403)

        run = client.post("/workflows/run", json={"workflow_id": "fixture", "steps": []}).json()
        self.assertEqual(run["status"], "completed")
        self.assertEqual(client.get("/workflows/runs?workflow_id=fixture").json()["runs"][0]["run_id"], "run-1")
        self.assertEqual(client.get("/workflows/runs/run-1").json()["run_id"], "run-1")
        self.assertEqual(client.get("/workflows/runs/missing").status_code, 404)

        dashboard = client.get("/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("X-Operator-Id", dashboard.text)


if __name__ == "__main__":
    unittest.main()
