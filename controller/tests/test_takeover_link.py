"""The model never receives an isolated session's noVNC password (NAV-14).

Each docker_ephemeral session's noVNC has its own random password. The handoff results (create_session,
get_session, get_remote_access, request_human_takeover, over REST and MCP) used to carry it in the takeover URL's
fragment, and over MCP those results reach the calling model; with a session tunnel up that URL is the public one.
The full link now comes only from GET /sessions/{session_id}/takeover-link, which the dashboard and
scripts/takeover_link.sh read and no MCP tool or agent-facing package exposes.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import session_isolation
from app.browser_manager import BrowserManager
from app.main import handle_key_not_found
from app.models import McpToolCallRequest
from app.routes.extensions import _DASHBOARD_HTML
from app.routes.operations import create_operations_router
from app.routes.sessions import create_sessions_router
from app.session_isolation import DockerBrowserNodeProvisioner
from app.tool_gateway.gateway import McpToolGateway
from tests.test_auth_state_ownership import StateContext
from tests.test_browser_manager_create_session import FakeBrowser, FakePage
from tests.test_session_isolation import FakeDockerClient, FakeDockerContainer
from tests.test_session_vnc_password import _settings

SENTINEL = "Snt9Pw7Q"
REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "takeover_link.sh"


class _Isolated(unittest.IsolatedAsyncioTestCase):
    """A docker_ephemeral manager whose sessions are provisioned on a fake Docker, with the password a sentinel."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        settings = _settings(root, SESSION_ISOLATION_MODE="docker_ephemeral", ISOLATED_HOST_DATA_ROOT=str(root))
        settings.max_sessions = 5
        patcher = patch.object(session_isolation, "_new_vnc_password", return_value=SENTINEL)
        patcher.start()
        self.addCleanup(patcher.stop)
        controller = FakeDockerContainer(container_id="controller-id", name="controller", attrs={})
        provisioner = DockerBrowserNodeProvisioner(settings, client=FakeDockerClient(controller))
        provisioner._controller_container_id = controller.id
        manager = BrowserManager(settings)
        manager.runtime_provisioner = provisioner
        manager._settle = AsyncMock()  # type: ignore[method-assign]
        manager._maybe_provision_session_tunnel = AsyncMock()  # type: ignore[method-assign]

        async def acquire(session_id: str):
            browser = FakeBrowser(StateContext(FakePage()))
            browser.new_context = AsyncMock(side_effect=lambda **_kwargs: StateContext(FakePage(), marker="own"))
            return browser, await provisioner.provision(session_id)

        manager._acquire_session_browser = acquire  # type: ignore[method-assign]
        self.manager = manager
        self.audit = AsyncMock(wraps=manager.audit.append)
        manager.audit.append = self.audit  # type: ignore[method-assign]
        self.gateway = McpToolGateway(manager=manager, orchestrator=Mock(), job_queue=Mock(), tool_profile="full")
        app = FastAPI()
        app.add_exception_handler(KeyError, handle_key_not_found)  # the controller's own: an unknown session is 404
        app.include_router(create_sessions_router(manager=manager))
        app.include_router(create_operations_router(manager=manager, proxy_store=Mock(), cron_service=Mock()))
        self.client = TestClient(app)

    async def call(self, tool: str, **arguments) -> str:
        response = await self.gateway.call_tool(McpToolCallRequest(name=tool, arguments=arguments))
        return response.model_dump_json()


class NoPasswordInAgentResultsTests(_Isolated):
    async def test_no_mcp_result_carries_the_password(self) -> None:
        created = await self.call("browser.create_session", name="iso")
        self.assertNotIn(SENTINEL, created)
        session_id = next(iter(self.manager.sessions))
        session = self.manager.sessions[session_id]
        self.assertEqual(session.runtime.vnc_password, SENTINEL)  # the session has one; no result shows it

        for name, arguments in (
            ("browser.get_session", {"session_id": session_id}),
            ("browser.get_remote_access", {"session_id": session_id}),
            ("browser.request_human_takeover", {"session_id": session_id, "reason": "log in"}),
            ("browser.fork_session", {"session_id": session_id}),
            ("browser.list_sessions", {}),
            ("browser.observe", {"session_id": session_id}),
        ):
            with self.subTest(tool=name):
                self.assertNotIn(SENTINEL, await self.call(name, **arguments))

    async def test_the_takeover_result_says_how_the_person_opens_the_full_link(self) -> None:
        await self.call("browser.create_session", name="iso")
        session_id = next(iter(self.manager.sessions))

        result = await self.manager.request_human_takeover(session_id, "log in")

        self.assertIn("scripts/takeover_link.sh", result["takeover_note"])
        self.assertIn("dashboard", result["remote_access"]["takeover_note"])
        self.assertNotIn("#password=", result["takeover_url"])

    async def test_no_rest_response_carries_the_password(self) -> None:
        created = self.client.post("/sessions", json={"name": "iso"})
        self.assertEqual(created.status_code, 200, created.text)
        self.assertNotIn(SENTINEL, created.text)
        session_id = created.json()["id"]

        for method, path, body in (
            ("get", f"/sessions/{session_id}", None),
            ("get", f"/remote-access?session_id={session_id}", None),
            ("post", f"/sessions/{session_id}/takeover", {"reason": "log in"}),
            ("get", "/sessions", None),
            ("get", f"/sessions/{session_id}/observe", None),
            ("post", f"/sessions/{session_id}/fork", None),
        ):
            with self.subTest(path=path):
                kwargs = {"json": body} if body is not None else {}
                response = getattr(self.client, method)(path, **kwargs)
                self.assertNotIn(SENTINEL, response.text)


class TakeoverLinkEndpointTests(_Isolated):
    async def test_the_endpoint_returns_the_full_link_uncached_and_audits_without_it(self) -> None:
        session_id = self.client.post("/sessions", json={"name": "iso"}).json()["id"]
        self.audit.reset_mock()

        response = self.client.get(f"/sessions/{session_id}/takeover-link")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        body = response.json()
        self.assertEqual(body["session_id"], session_id)
        self.assertTrue(body["takeover_url"].endswith(f"#password={SENTINEL}"), body)
        viewed = [c for c in self.audit.await_args_list if c.kwargs.get("event_type") == "takeover_link_viewed"]
        self.assertEqual(len(viewed), 1)
        self.assertNotIn(SENTINEL, repr(viewed[0]))
        self.assertNotIn("vnc.html", repr(viewed[0]))

    async def test_an_unknown_or_closed_session_is_404(self) -> None:
        self.assertEqual(self.client.get("/sessions/no-such-session/takeover-link").status_code, 404)
        session_id = self.client.post("/sessions", json={"name": "iso"}).json()["id"]
        await self.manager.close_session(session_id)

        self.assertEqual(self.client.get(f"/sessions/{session_id}/takeover-link").status_code, 404)

    async def test_a_shared_browser_node_session_gets_its_plain_url(self) -> None:
        session_id = self.client.post("/sessions", json={"name": "iso"}).json()["id"]
        self.manager.sessions[session_id].isolation_mode = "shared_browser_node"

        body = self.client.get(f"/sessions/{session_id}/takeover-link").json()

        self.assertNotIn("#password=", body["takeover_url"])

    async def test_the_endpoint_is_no_mcp_tool(self) -> None:
        names = {tool["name"] for tool in self.gateway.list_tools()}
        self.assertFalse(any("takeover_link" in name or "takeover-link" in name for name in names), names)


class AgentPackagesTests(unittest.TestCase):
    @unittest.skipUnless((REPO / "client").is_dir(), "the agent-facing packages are not shipped in the image")
    def test_no_agent_facing_package_calls_the_endpoint(self) -> None:
        for package in ("client", "packaging/auto-browser-mcp", "integrations/langchain"):
            for path in (REPO / package).rglob("*.py"):
                self.assertNotIn("takeover-link", path.read_text(encoding="utf-8"), str(path))


class DashboardAndScriptTests(unittest.TestCase):
    def test_the_dashboard_opens_the_takeover_link_endpoint(self) -> None:
        opener = _DASHBOARD_HTML.split("async function openTakeover(")[1].split("\n}\n")[0]
        self.assertIn("/takeover-link", opener)

    @unittest.skipUnless(SCRIPT.is_file() and shutil.which("bash"), "scripts/ or bash is not here")
    def test_the_script_parses(self) -> None:
        subprocess.run([shutil.which("bash"), "-n", str(SCRIPT)], check=True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
