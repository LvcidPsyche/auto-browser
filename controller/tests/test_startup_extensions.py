from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.startup import extensions


class FakeManager:
    def __init__(self) -> None:
        self.sessions = {}
        self.register_extension_hooks = MagicMock()


class StartupExtensionsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.old_mesh_enabled = os.environ.pop("MESH_ENABLED", None)
        self.old_stealth_profile = os.environ.pop("STEALTH_PROFILE", None)
        self.old_workflows_root = os.environ.get("WORKFLOWS_ROOT")
        self.temp_dir = tempfile.TemporaryDirectory()
        os.environ["WORKFLOWS_ROOT"] = self.temp_dir.name

    def tearDown(self) -> None:
        if self.old_mesh_enabled is not None:
            os.environ["MESH_ENABLED"] = self.old_mesh_enabled
        if self.old_stealth_profile is not None:
            os.environ["STEALTH_PROFILE"] = self.old_stealth_profile
        if self.old_workflows_root is not None:
            os.environ["WORKFLOWS_ROOT"] = self.old_workflows_root
        else:
            os.environ.pop("WORKFLOWS_ROOT", None)
        self.temp_dir.cleanup()

    def test_register_extensions_initializes_disabled_state_and_hooks(self) -> None:
        app = SimpleNamespace(
            state=SimpleNamespace(
                browser_manager=FakeManager(),
                settings=SimpleNamespace(stealth_enabled=False),
                tool_gateway=None,
            )
        )

        with patch("app.startup.extensions._init_curator") as curator:
            extensions.register_extensions(app)

        self.assertIsNone(app.state.mesh_identity)
        self.assertIsNone(app.state.peer_registry)
        self.assertIsNone(app.state.delegation_manager)
        self.assertEqual(app.state.network_inspectors, {})
        self.assertEqual(app.state.cdp_sessions, {})
        self.assertIsNone(app.state.youtube_client)
        self.assertIsNone(app.state.veo3_client)
        self.assertIsNotNone(app.state.workflow_engine)
        curator.assert_called_once_with(app)
        app.state.browser_manager.register_extension_hooks.assert_called_once()

    async def test_mesh_gateway_adapts_mcp_response_and_marks_errors(self) -> None:
        gateway = AsyncMock()
        app = SimpleNamespace(state=SimpleNamespace(tool_gateway=gateway))
        gateway.call_tool.return_value = SimpleNamespace(
            isError=False,
            structuredContent={"ok": True},
            content=[],
        )

        call = extensions._build_mesh_tool_gateway(app)
        result = await call("browser.observe", {}, "session-1")

        self.assertEqual(result, {"ok": True})
        args = gateway.call_tool.await_args.args[0]
        self.assertEqual(args.arguments["session_id"], "session-1")

        gateway.call_tool.return_value = SimpleNamespace(
            isError=True,
            structuredContent={"code": "bad"},
            content=[],
        )
        errored = await call("browser.bad", {"session_id": "other"}, "session-1")
        self.assertTrue(errored["_mesh_error"])

        gateway.call_tool.return_value = SimpleNamespace(
            isError=True,
            structuredContent={"status": "approval_required"},
            content=[],
        )
        approval = await call("browser.click", {}, "session-1")
        self.assertNotIn("_mesh_error", approval)

    async def test_session_lifecycle_tracks_network_cdp_and_curator(self) -> None:
        manager = FakeManager()
        session = SimpleNamespace(network_inspector=object())
        manager.sessions["session-1"] = session
        curator = SimpleNamespace(ready=True, complete=AsyncMock(return_value="NO_SKILL"))
        app = SimpleNamespace(
            state=SimpleNamespace(
                browser_manager=manager,
                network_inspectors={},
                cdp_sessions={},
                settings=SimpleNamespace(stealth_enabled=False),
                curator_adapter=curator,
            )
        )

        with patch("app.cdp.passthrough.CDPPassthrough.from_page", new=AsyncMock(return_value="cdp")):
            await extensions.on_session_created(app, "session-1", SimpleNamespace())

        self.assertIs(app.state.network_inspectors["session-1"], session.network_inspector)
        self.assertEqual(app.state.cdp_sessions["session-1"], "cdp")

        def _close_task(coro):
            coro.close()

        with patch("asyncio.create_task", side_effect=_close_task) as create_task:
            await extensions.on_session_closed(app, "session-1")

        self.assertEqual(app.state.network_inspectors, {})
        self.assertEqual(app.state.cdp_sessions, {})
        create_task.assert_called_once()


if __name__ == "__main__":
    unittest.main()
