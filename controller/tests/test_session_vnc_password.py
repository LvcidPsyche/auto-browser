"""Each docker_ephemeral session's noVNC has its own random VNC password.

A session's published noVNC (6080) is reachable from other sessions'
containers: Docker routes to published ports across bridge networks. The
operator's one VNC_PASSWORD used to be handed to every isolated container,
where code in one session could read its own copy and open any other
session's desktop, or the shared browser-node's. Each container now gets a
fresh password, and only the human-only takeover link (GET
/sessions/{id}/takeover-link, which the dashboard and scripts/takeover_link.sh
read) carries it, in the takeover URL's fragment. No REST or MCP result an agent
gets, the stored session records, the tunnel metadata file, the audit trail and
logs ever do (NAV-14).
"""

from __future__ import annotations

import logging
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.result_shaping import compact_session, shape_mcp_result
from app.session_isolation import DockerBrowserNodeProvisioner, IsolatedBrowserRuntime, takeover_url_with_password
from app.session_tunnel import IsolatedSessionTunnel
from app.utils import UTC
from tests.test_session_isolation import FakeContext, FakeDockerClient, FakeDockerContainer, FakePage, FakeTunnelProcess

PASSWORD = "Pw4xQz9K"
TAKEOVER = "http://127.0.0.1:16080/vnc.html?autoconnect=true&resize=scale"
DATA_ROOT_SETTINGS = {
    "ARTIFACT_ROOT": "artifacts",
    "UPLOAD_ROOT": "uploads",
    "AUTH_ROOT": "auth",
    "APPROVAL_ROOT": "approvals",
    "AUDIT_ROOT": "audit",
    "WITNESS_ROOT": "witness",
    "SESSION_STORE_ROOT": "sessions",
    "JOB_STORE_ROOT": "jobs",
    "HARNESS_ROOT": "harness",
    "MEMORY_ROOT": "memory",
    "ISOLATED_TUNNEL_INFO_ROOT": "tunnels/sessions",
    "REMOTE_ACCESS_INFO_PATH": "tunnels/reverse-ssh.json",
    "MCP_SESSION_STORE_PATH": "mcp/sessions.json",
    "CRON_STORE_PATH": "crons/crons.json",
}


def _settings(root: Path, **extra) -> Settings:
    paths = {key: str(root / value) for key, value in DATA_ROOT_SETTINGS.items()}
    return Settings(_env_file=None, ISOLATED_TAKEOVER_HOST="127.0.0.1", **paths, **extra)


def _files_containing(root: Path, needle: str) -> list[str]:
    hits = []
    for path in root.rglob("*"):
        if path.is_file() and needle.encode() in path.read_bytes():
            hits.append(str(path.relative_to(root)))
    return hits


class PasswordGenerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.root = Path(self._tempdir.name)

    def provisioner(self, **extra) -> tuple[DockerBrowserNodeProvisioner, FakeDockerClient]:
        settings = _settings(
            self.root, SESSION_ISOLATION_MODE="docker_ephemeral", ISOLATED_HOST_DATA_ROOT=str(self.root), **extra
        )
        controller = FakeDockerContainer(container_id="controller-id", name="controller", attrs={})
        client = FakeDockerClient(controller)
        provisioner = DockerBrowserNodeProvisioner(settings, client=client)
        provisioner._controller_container_id = controller.id
        return provisioner, client

    async def test_each_session_gets_its_own_random_password_never_the_operators(self) -> None:
        provisioner, client = self.provisioner(VNC_PASSWORD="operator1")

        a = await provisioner.provision("a")
        b = await provisioner.provision("b")

        for runtime in (a, b):
            self.assertRegex(runtime.vnc_password, r"^[A-Za-z0-9]{8}$")
            environment = client.containers.browser_containers[runtime.container_name].run_kwargs["environment"]
            self.assertEqual(environment["VNC_PASSWORD"], runtime.vnc_password)
            self.assertNotIn("operator1", environment.values())
            # The provisioner's own takeover URL stays password-free: it is stored.
            self.assertNotIn(runtime.vnc_password, runtime.takeover_url)
            self.assertNotIn(runtime.vnc_password, repr(runtime))
        self.assertNotEqual(a.vnc_password, b.vnc_password)

    async def test_the_password_is_never_logged(self) -> None:
        provisioner, _ = self.provisioner(ISOLATED_BROWSER_WAIT_TIMEOUT_SECONDS="1")
        with self.assertLogs("app", level=logging.DEBUG) as logs:
            logging.getLogger("app").debug("capture starts")
            runtime = await provisioner.provision("logged")
            await provisioner.release(runtime)
            await provisioner.startup()

        self.assertNotIn(runtime.vnc_password, "\n".join(logs.output))

    def test_the_fragment_carries_the_password(self) -> None:
        self.assertEqual(takeover_url_with_password(TAKEOVER, PASSWORD), f"{TAKEOVER}#password={PASSWORD}")
        # An ISOLATED_TAKEOVER_PATH that already has a fragment keeps it.
        self.assertEqual(
            takeover_url_with_password("http://h:1/vnc.html#autoconnect=1", PASSWORD),
            f"http://h:1/vnc.html#autoconnect=1&password={PASSWORD}",
        )
        self.assertEqual(takeover_url_with_password(TAKEOVER, ""), TAKEOVER)
        self.assertEqual(takeover_url_with_password("", PASSWORD), "")


class HandoffSurfaceTests(unittest.IsolatedAsyncioTestCase):
    """Only the takeover link carries the password; everything else, on disk or
    in the model's context, does not."""

    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.root = Path(self._tempdir.name)

    def manager_with_session(self, *, tunnel: bool = False, **extra) -> tuple[BrowserManager, BrowserSession]:
        if tunnel:
            ssh = self.root / "ssh"
            ssh.mkdir(parents=True, exist_ok=True)
            (ssh / "id_ed25519").write_text("dummy", encoding="utf-8")
            (ssh / "known_hosts").write_text("dummy", encoding="utf-8")
            extra.update(
                ISOLATED_TUNNEL_ENABLED="true",
                ISOLATED_TUNNEL_HOST="bastion.example.com",
                ISOLATED_TUNNEL_USER="tunnel",
                ISOLATED_TUNNEL_KEY_PATH=str(ssh / "id_ed25519"),
                ISOLATED_TUNNEL_KNOWN_HOSTS_PATH=str(ssh / "known_hosts"),
            )
        settings = _settings(self.root, SESSION_ISOLATION_MODE="docker_ephemeral", **extra)
        manager = BrowserManager(settings)
        runtime = IsolatedBrowserRuntime(
            session_id="s1",
            container_id="c1",
            container_name="browser-session-s1",
            network_name="browser-session-s1-net",
            browser_node_name="browser-session-s1",
            profile_dir=self.root / "browser-sessions" / "s1" / "profile",
            downloads_dir=self.root / "browser-sessions" / "s1" / "downloads",
            ws_endpoint_file=self.root / "browser-sessions" / "s1" / "profile" / "ws.txt",
            ws_endpoint="ws://browser-session-s1:9223/playwright",
            takeover_url=TAKEOVER,
            novnc_port=16080,
            tunnel_local_host="browser-session-s1",
            network_owned=True,
            vnc_password=PASSWORD,
        )
        artifact_dir = Path(settings.artifact_root) / "s1"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        session = BrowserSession(
            id="s1",
            name="s1",
            created_at=datetime.now(UTC),
            context=FakeContext(),  # type: ignore[arg-type]
            page=FakePage("https://example.com/"),  # type: ignore[arg-type]
            artifact_dir=artifact_dir,
            auth_dir=Path(settings.auth_root) / "s1",
            upload_dir=Path(settings.upload_root) / "s1",
            takeover_url=runtime.takeover_url,
            trace_path=artifact_dir / "trace.zip",
            browser_node_name=runtime.browser_node_name,
            isolation_mode="docker_ephemeral",
            runtime=runtime,
            shared_takeover_surface=False,
            shared_browser_process=False,
        )
        if tunnel:
            session.tunnel = IsolatedSessionTunnel(
                session_id="s1",
                remote_port=16181,
                local_host="browser-session-s1",
                local_port=6080,
                public_takeover_url="http://bastion.example.com:16181/vnc.html?autoconnect=true&resize=scale",
                info_path=Path(settings.isolated_tunnel_info_root) / "s1.json",
                log_path=Path(settings.isolated_tunnel_info_root) / "s1.log",
                ssh_host="bastion.example.com",
                ssh_port=22,
                ssh_user="tunnel",
                access_mode="private",
                remote_bind_address="127.0.0.1",
                info_interval_seconds=10.0,
                process=FakeTunnelProcess(),
                status="active",
            )
        manager.sessions[session.id] = session
        return manager, session

    async def test_only_the_takeover_link_carries_the_password(self) -> None:
        # NAV-14: the handoff results (session record, remote access, takeover result) reach the model over MCP, so
        # none of them carries it any more; only the human-only takeover link does.
        manager, session = self.manager_with_session()
        handoff = f"{TAKEOVER}#password={PASSWORD}"

        with self.assertLogs("app", level=logging.DEBUG) as logs:
            logging.getLogger("app").debug("capture starts")
            summary = await manager._session_summary(session)
            await manager._persist_session(session, status="active")
            takeover = await manager.request_human_takeover("s1", "log in")
            record = await manager.get_session_record("s1")
            remote = manager.get_remote_access_info("s1")
            link = manager.remote_access.takeover_link(session)

        self.assertEqual(link, {"session_id": "s1", "takeover_url": handoff})
        for name, value in {
            "summary": summary,
            "current_takeover_url": manager._current_takeover_url(session),
            "compact reference": compact_session(summary),
            "record": record,
            "get_session over MCP": shape_mcp_result("browser.get_session", record),
            "remote access": remote,
            "takeover result": takeover,
        }.items():
            self.assertNotIn(PASSWORD, repr(value), name)
        # On disk: the session store, the audit trail, actions.jsonl, witness receipts.
        self.assertEqual(_files_containing(self.root, PASSWORD), [])
        self.assertNotIn(PASSWORD, "\n".join(logs.output))

    async def test_the_tunnel_takeover_link_carries_it_but_the_tunnel_file_does_not(self) -> None:
        manager, session = self.manager_with_session(tunnel=True)
        tunnel_url = session.tunnel.public_takeover_url
        session.tunnel.info_path.parent.mkdir(parents=True, exist_ok=True)  # the broker's startup makes it

        manager.tunnel_broker._write_metadata(session.tunnel, status="active")
        link = manager.remote_access.takeover_link(session)
        view = manager.get_remote_access_info("s1")
        await manager._persist_session(session, status="active")

        self.assertEqual(link["takeover_url"], f"{tunnel_url}#password={PASSWORD}")
        self.assertEqual(view["takeover_url"], tunnel_url)
        self.assertNotIn(PASSWORD, repr(view))
        self.assertTrue(session.tunnel.info_path.is_file())
        self.assertEqual(_files_containing(self.root, PASSWORD), [])

    async def test_a_shared_session_gets_its_plain_url(self) -> None:
        manager, session = self.manager_with_session()
        session.isolation_mode = "shared_browser_node"

        self.assertNotIn(PASSWORD, manager.remote_access.takeover_link(session)["takeover_url"])


class RouteWiringTests(unittest.TestCase):
    """No REST or MCP handler adds the password any more (NAV-14)."""

    def test_no_handler_adds_the_password(self) -> None:
        app_dir = Path(__file__).resolve().parents[1] / "app"
        for relative in ("routes/sessions.py", "routes/operations.py", "tool_gateway/gateway.py"):
            text = (app_dir / relative).read_text(encoding="utf-8")
            self.assertNotIn("with_takeover_password", text, relative)
            self.assertNotIn("takeover_url_with_password", text, relative)

    def test_no_mcp_tool_reaches_the_takeover_link(self) -> None:
        app_dir = Path(__file__).resolve().parents[1] / "app"
        tool_files = [
            app_dir / "tool_gateway" / "gateway.py",
            *sorted((app_dir / "tool_gateway" / "packs").glob("*.py")),
        ]
        self.assertGreater(len(tool_files), 1)
        for path in tool_files:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("takeover_link(", text, str(path))
            self.assertNotIn("takeover-link", text, str(path))
            self.assertNotIn("takeover_token", text, str(path))

    def test_only_the_human_only_paths_reach_the_takeover_link_anywhere_in_the_app(self) -> None:
        # W2-25: the guard above covered tool_gateway only. Across controller/app, takeover_link() is called only by
        # the route, defined only in the remote-access service, and the endpoint's path (a string with no spaces:
        # prose, docstrings and log messages may name it) is spelled only by the route's constant.
        import ast

        app_dir = Path(__file__).resolve().parents[1] / "app"
        calls, definitions, paths = set(), set(), set()
        for path in app_dir.rglob("*.py"):
            relative = path.relative_to(app_dir).as_posix()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call):
                    func = node.func
                    if (func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")) == "takeover_link":
                        calls.add(relative)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "takeover_link":
                    definitions.add(relative)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if "takeover-link" in node.value and not any(char.isspace() for char in node.value):
                        paths.add(relative)
        self.assertEqual(calls, {"routes/sessions.py"})
        self.assertEqual(definitions, {"browser/services/remote_access.py"})
        self.assertEqual(paths, {"routes/_utils.py"})
        dashboard = (app_dir / "routes" / "extensions" / "dashboard_html.py").read_text(encoding="utf-8")
        self.assertIn("/takeover-link`", dashboard)

    def test_the_agent_facing_note_names_no_token(self) -> None:
        from app.browser.services.remote_access import TAKEOVER_NOTE

        for needle in ("TAKEOVER_TOKEN", "takeover-token", "takeover token", "X-Takeover-Token", "scripts/", ".sh"):
            self.assertNotIn(needle, TAKEOVER_NOTE)


if __name__ == "__main__":
    unittest.main()
