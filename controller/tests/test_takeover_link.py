"""The model never receives an isolated session's noVNC password (NAV-14).

Each docker_ephemeral session's noVNC has its own random password. The handoff results (create_session,
get_session, get_remote_access, request_human_takeover, over REST and MCP) used to carry it in the takeover URL's
fragment, and over MCP those results reach the calling model; with a session tunnel up that URL is the public one.
The full link now comes only from GET /sessions/{session_id}/takeover-link, which the dashboard and
scripts/takeover_link.sh read and no MCP tool or agent-facing package exposes.
"""

from __future__ import annotations

import os
import shutil
import stat
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
TAKEOVER = "takeover-sentinel-0123456789abcdefghijklmnop"
REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "takeover_link.sh"
_PAGE_SUMMARY = {
    "title": "Fixture Page",
    "active_element": None,
    "text_excerpt": "",
    "dom_outline": {},
    "accessibility_outline": {"available": False, "nodes": []},
}


class _Isolated(unittest.IsolatedAsyncioTestCase):
    """A docker_ephemeral manager whose sessions are provisioned on a fake Docker, with the password a sentinel."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        settings = _settings(root, SESSION_ISOLATION_MODE="docker_ephemeral", ISOLATED_HOST_DATA_ROOT=str(root))
        settings.max_sessions = 5
        settings.takeover_token = TAKEOVER
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
        # FakePage has no screenshot, so the default preset's observe fails, and a sentinel check on its error
        # passes whatever the result would carry. The text preset reads only these two, so observe succeeds.
        manager.element_ids.evaluate = AsyncMock(return_value=[])  # type: ignore[method-assign]
        manager.observation.page_summary = AsyncMock(return_value=_PAGE_SUMMARY)  # type: ignore[method-assign]
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
        dumped = response.model_dump_json()
        self.assertFalse(response.isError, dumped)
        return dumped

    def link(self, session_id: str, token: str | None = TAKEOVER):
        headers = {} if token is None else {"X-Takeover-Token": token}
        return self.client.get(f"/sessions/{session_id}/takeover-link", headers=headers)


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
            ("browser.observe", {"session_id": session_id, "preset": "text"}),
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
            ("get", f"/sessions/{session_id}/observe?preset=text", None),
            ("post", f"/sessions/{session_id}/fork", None),
        ):
            with self.subTest(path=path):
                kwargs = {"json": body} if body is not None else {}
                response = getattr(self.client, method)(path, **kwargs)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn(SENTINEL, response.text)


class TakeoverLinkEndpointTests(_Isolated):
    async def test_the_endpoint_returns_the_full_link_uncached_and_audits_without_it(self) -> None:
        session_id = self.client.post("/sessions", json={"name": "iso"}).json()["id"]
        self.audit.reset_mock()

        response = self.link(session_id)

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
        unknown = self.link("no-such-session")
        self.assertEqual(unknown.status_code, 404, unknown.text)
        self.assertEqual(unknown.json().get("code"), "unknown_session", unknown.text)
        session_id = self.client.post("/sessions", json={"name": "iso"}).json()["id"]
        await self.manager.close_session(session_id)

        closed = self.link(session_id)
        self.assertEqual(closed.status_code, 404, closed.text)
        self.assertEqual(closed.json().get("code"), "session_closed", closed.text)

    async def test_a_shared_browser_node_session_gets_its_plain_url(self) -> None:
        session_id = self.client.post("/sessions", json={"name": "iso"}).json()["id"]
        self.manager.sessions[session_id].isolation_mode = "shared_browser_node"

        response = self.link(session_id)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("#password=", response.json()["takeover_url"])

    async def test_the_endpoint_is_no_mcp_tool(self) -> None:
        names = {tool["name"] for tool in self.gateway.list_tools()}
        self.assertFalse(any("takeover_link" in name or "takeover-link" in name for name in names), names)


class TakeoverTokenTests(_Isolated):
    """The endpoint also demands X-Takeover-Token, a secret the person holds and agents are not given (NAV-14b)."""

    def refusals(self) -> list:
        return [c for c in self.audit.await_args_list if c.kwargs.get("event_type") == "takeover_link_refused"]

    async def test_a_missing_or_wrong_token_is_403_and_audited_without_the_token_or_link(self) -> None:
        session_id = self.client.post("/sessions", json={"name": "iso"}).json()["id"]
        self.audit.reset_mock()
        for token in (None, "", "wrong-" + TAKEOVER, TAKEOVER[:-1]):
            with self.subTest(token=token):
                response = self.link(session_id, token=token)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertNotIn(SENTINEL, response.text)
                self.assertEqual(response.json()["detail"], self.link("other", token=None).json()["detail"])
        refusals = self.refusals()
        self.assertGreaterEqual(len(refusals), 4)
        for refusal in refusals:
            self.assertNotIn(TAKEOVER, repr(refusal))
            self.assertNotIn(TAKEOVER[:-1], repr(refusal))
            self.assertNotIn(SENTINEL, repr(refusal))
            self.assertNotIn("vnc.html", repr(refusal))
        viewed = [c for c in self.audit.await_args_list if c.kwargs.get("event_type") == "takeover_link_viewed"]
        self.assertEqual(viewed, [])

    async def test_the_right_token_is_200(self) -> None:
        session_id = self.client.post("/sessions", json={"name": "iso"}).json()["id"]
        response = self.link(session_id)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["takeover_url"].endswith(f"#password={SENTINEL}"))

    async def test_an_unknown_session_without_the_token_is_403_not_404(self) -> None:
        response = self.link("no-such-session", token=None)
        self.assertEqual(response.status_code, 403, response.text)

    async def test_no_loaded_token_is_503_with_a_fixed_detail(self) -> None:
        session_id = self.client.post("/sessions", json={"name": "iso"}).json()["id"]
        self.manager.settings.takeover_token = None
        first = self.link(session_id, token=None)
        second = self.link(session_id, token="anything")
        self.assertEqual(first.status_code, 503, first.text)
        self.assertEqual(second.status_code, 503, second.text)
        self.assertEqual(first.json()["detail"], second.json()["detail"])
        self.assertNotIn(SENTINEL, first.text)

    async def test_tokenless_loopback_still_needs_the_takeover_token(self) -> None:
        from types import SimpleNamespace

        from app.middleware.http import install_controller_http_middleware

        session_id = self.client.post("/sessions", json={"name": "iso"}).json()["id"]
        settings = self.manager.settings
        settings.api_bind_scope = "loopback"
        settings.api_allow_tokenless_loopback = True
        settings.api_bearer_token = None
        app = FastAPI()
        app.add_exception_handler(KeyError, handle_key_not_found)
        app.include_router(create_sessions_router(manager=self.manager))
        install_controller_http_middleware(
            app, settings=settings, rate_limiter=None, metrics=SimpleNamespace(enabled=False)
        )
        client = TestClient(app)

        self.assertEqual(client.get("/sessions").status_code, 200)  # tokenless loopback really is on
        refused = client.get(f"/sessions/{session_id}/takeover-link")
        self.assertEqual(refused.status_code, 403, refused.text)
        allowed = client.get(f"/sessions/{session_id}/takeover-link", headers={"X-Takeover-Token": TAKEOVER})
        self.assertEqual(allowed.status_code, 200, allowed.text)


class AgentPackagesTests(unittest.TestCase):
    @unittest.skipUnless((REPO / "client").is_dir(), "the agent-facing packages are not shipped in the image")
    def test_no_agent_facing_package_calls_the_endpoint(self) -> None:
        for package in ("client", "packaging/auto-browser-mcp", "integrations/langchain"):
            for path in (REPO / package).rglob("*.py"):
                self.assertNotIn("takeover-link", path.read_text(encoding="utf-8"), str(path))


API_SENTINEL = "api-sentinel-0123456789abcdefghijklmnopqrs"
_RECORDER = """#!/usr/bin/env bash
# Records this command's argv, one argument per line, then runs the real one (or answers for curl).
for arg in "$0" "$@"; do printf '%s\\n' "$arg"; done >> "${STUB_LOG}/argv"
if [[ "$(basename "$0")" == curl ]]; then
  cat > "${STUB_LOG}/curl-stdin"
  printf '{"session_id": "abc123", "takeover_url": "http://127.0.0.1:16080/vnc.html#password=%s"}' "${STUB_PASSWORD}"
  exit 0
fi
exec "${REAL_PYTHON3}" "$@"
"""


def _opener() -> str:
    return _DASHBOARD_HTML.split("async function openTakeover(")[1].split("\n}\n")[0]


class DashboardAndScriptTests(unittest.TestCase):
    def test_the_dashboard_opens_the_takeover_link_endpoint(self) -> None:
        self.assertIn("/takeover-link", _opener())

    def test_the_dashboard_sends_the_takeover_token_it_asked_for_once_per_tab(self) -> None:
        opener = _opener()
        self.assertIn("X-Takeover-Token", opener)
        self.assertIn("takeoverToken(", opener)
        asker = _DASHBOARD_HTML.split("function takeoverToken(")[1].split("\n}\n")[0]
        self.assertIn("sessionStorage.getItem(TAKEOVER_TOKEN_KEY)", asker)
        self.assertIn("sessionStorage.setItem(TAKEOVER_TOKEN_KEY", asker)
        self.assertIn("TAKEOVER_TOKEN", asker)
        self.assertIn("data/takeover-token", asker)
        self.assertIn("const TAKEOVER_TOKEN_KEY = 'ab_takeover_token';", _DASHBOARD_HTML)
        # A 403 clears the stored token; the next click asks again (W2-18: never a prompt behind the popup).
        self.assertIn("err.status === 403", opener)
        self.assertIn("sessionStorage.removeItem(TAKEOVER_TOKEN_KEY)", opener)

    def test_the_login_wizard_opens_its_takeover_window_through_open_takeover(self) -> None:
        wizard = _DASHBOARD_HTML.split("const takeover = document.getElementById('wizard-takeover');")[1]
        wizard = wizard.split("\n}\n")[0]
        self.assertIn("Open takeover window to log in", wizard)
        self.assertIn("openTakeover(event, ", wizard)

    def test_the_script_is_here_when_scripts_is(self) -> None:
        if not (REPO / "scripts").is_dir():
            self.skipTest("scripts/ is not shipped in the image")
        self.assertTrue(SCRIPT.is_file(), SCRIPT)

    @unittest.skipUnless(SCRIPT.is_file() and shutil.which("bash"), "scripts/ or bash is not here")
    def test_the_script_parses(self) -> None:
        subprocess.run([shutil.which("bash"), "-n", str(SCRIPT)], check=True)

    def _tree(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "scripts").mkdir()
        for name in ("takeover_link.sh", "load_env.sh"):
            shutil.copy2(REPO / "scripts" / name, root / "scripts" / name)
        (root / ".env").write_text(
            f"API_PORT=18123\nAPI_BEARER_TOKEN={API_SENTINEL}\nTAKEOVER_TOKEN={TAKEOVER}\n", encoding="utf-8"
        )
        (root / "bin").mkdir()
        (root / "log").mkdir()
        for name in ("curl", "python3"):
            stub = root / "bin" / name
            stub.write_text(_RECORDER, encoding="utf-8")
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        return root

    def _run(self, root: Path, *args: str) -> subprocess.CompletedProcess:
        env = {
            "PATH": f"{root / 'bin'}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "HOME": str(root),
            "STUB_LOG": str(root / "log"),
            "STUB_PASSWORD": SENTINEL,
            "REAL_PYTHON3": shutil.which("python3") or "/usr/bin/python3",
        }
        return subprocess.run(
            [shutil.which("bash"), str(root / "scripts" / "takeover_link.sh"), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    @unittest.skipUnless(SCRIPT.is_file() and shutil.which("bash") and os.name != "nt", "scripts/ or bash is not here")
    def test_the_script_reads_env_sends_both_tokens_on_stdin_and_prints_the_link(self) -> None:
        root = self._tree()
        result = self._run(root, "abc123")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"http://127.0.0.1:16080/vnc.html#password={SENTINEL}\n")
        argv = (root / "log" / "argv").read_text(encoding="utf-8")
        self.assertIn("http://127.0.0.1:18123/sessions/abc123/takeover-link", argv.splitlines())
        # Both stubs record every argv they were started with: no token, no password, no response there.
        for secret in (API_SENTINEL, TAKEOVER, SENTINEL):
            self.assertNotIn(secret, argv)
            self.assertNotIn(secret, result.stderr)
        config = (root / "log" / "curl-stdin").read_text(encoding="utf-8").splitlines()
        self.assertIn(f'header = "Authorization: Bearer {API_SENTINEL}"', config)
        self.assertIn(f'header = "X-Takeover-Token: {TAKEOVER}"', config)

    @unittest.skipUnless(SCRIPT.is_file() and shutil.which("bash") and os.name != "nt", "scripts/ or bash is not here")
    def test_the_script_refuses_an_id_that_does_not_start_with_a_letter_or_digit(self) -> None:
        root = self._tree()
        for bad in (".", "..", "-x", "_a", "a/b", "a b"):
            with self.subTest(session_id=bad):
                result = self._run(root, bad)
                self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse((root / "log" / "argv").exists() and "curl" in (root / "log" / "argv").read_text())
        self.assertEqual(self._run(root, "a.b_c-1").returncode, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
