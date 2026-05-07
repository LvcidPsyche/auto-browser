import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.action_errors import BrowserActionError
from app.browser_manager import BrowserManager, BrowserSession, PlaywrightError
from app.config import Settings
from app.utils import UTC


def _settings(root: Path) -> Settings:
    return Settings(
        ARTIFACT_ROOT=str(root / "artifacts"),
        UPLOAD_ROOT=str(root / "uploads"),
        AUTH_ROOT=str(root / "auth"),
        APPROVAL_ROOT=str(root / "approvals"),
        AUDIT_ROOT=str(root / "audit"),
        WITNESS_ROOT=str(root / "witness"),
        SESSION_STORE_ROOT=str(root / "sessions"),
        REMOTE_ACCESS_INFO_PATH=str(root / "tunnels/reverse-ssh.json"),
        BROWSER_WS_ENDPOINT_FILE=str(root / "missing-ws.txt"),
        WITNESS_ENABLED=False,
        NETWORK_INSPECTOR_ENABLED=False,
        ENABLE_TRACING=False,
        STEALTH_ENABLED=False,
    )


class FakeMouse:
    def __init__(self) -> None:
        self.move = AsyncMock()
        self.down = AsyncMock()
        self.up = AsyncMock()
        self.wheel = AsyncMock()


class FakeKeyboard:
    def __init__(self) -> None:
        self.press = AsyncMock()
        self.type = AsyncMock()


class FakeLocator:
    def __init__(self, selector: str, *, attributes: dict[str, str] | None = None) -> None:
        self.selector = selector
        self.attributes = attributes or {}
        self.scroll_into_view_if_needed = AsyncMock()
        self.click = AsyncMock()
        self.hover = AsyncMock()
        self.select_option = AsyncMock()
        self.set_input_files = AsyncMock()
        self.fill = AsyncMock()
        self.count = AsyncMock(return_value=1)
        self.is_visible = AsyncMock(return_value=True)
        self.bounding_box = AsyncMock(return_value={"x": 10, "y": 20, "width": 100, "height": 40})
        self.get_attribute = AsyncMock(side_effect=lambda name: self.attributes.get(name))

    @property
    def first(self) -> "FakeLocator":
        return self


class FakePage:
    def __init__(self, url: str = "https://example.com") -> None:
        self.url = url
        self.context: FakeContext | None = None
        self.mouse = FakeMouse()
        self.keyboard = FakeKeyboard()
        self.locators: dict[str, FakeLocator] = {}
        self.on = Mock()
        self.set_default_timeout = Mock()
        self.title = AsyncMock(return_value="Fixture Page")
        self.wait_for_load_state = AsyncMock()
        self.wait_for_timeout = AsyncMock()
        self.bring_to_front = AsyncMock()
        self.goto = AsyncMock(side_effect=self._goto)
        self.reload = AsyncMock()
        self.go_back = AsyncMock()
        self.go_forward = AsyncMock()
        self.close = AsyncMock(side_effect=self._close)

    async def _goto(self, url: str, wait_until: str = "domcontentloaded") -> None:
        self.url = url

    async def _close(self) -> None:
        if self.context is not None and self in self.context.pages:
            self.context.pages.remove(self)

    def locator(self, selector: str) -> FakeLocator:
        locator = self.locators.get(selector)
        if locator is None:
            locator = FakeLocator(selector)
            self.locators[selector] = locator
        return locator


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.pages = [page]
        page.context = self
        self.on = Mock()
        self.storage_state = AsyncMock()
        self.tracing = Mock(stop=AsyncMock())

    async def _new_page(self) -> FakePage:
        page = FakePage("about:blank")
        page.context = self
        self.pages.append(page)
        return page

    def new_page(self) -> AsyncMock:
        return AsyncMock(side_effect=self._new_page)()


class BrowserManagerActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = BrowserManager(_settings(self.root))
        self.manager.audit.append = AsyncMock()
        self.manager._append_jsonl = AsyncMock()  # type: ignore[method-assign]
        self.manager._persist_session = AsyncMock()  # type: ignore[method-assign]
        self.manager._record_witness_receipt = AsyncMock()  # type: ignore[method-assign]
        self.manager.artifacts.trace_payload = Mock(return_value={"trace_path": "trace.zip", "trace_available": True})
        self.session = self._add_session()

        async def run_operation(session: BrowserSession, action_name: str, target: dict, operation) -> dict:
            await operation()
            session.last_action = action_name
            return {"action": action_name, "target": target}

        self.manager._run_action = run_operation  # type: ignore[method-assign]

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    def _add_session(self) -> BrowserSession:
        page = FakePage()
        context = FakeContext(page)
        artifact_dir = self.root / "artifacts" / "session-1"
        auth_dir = self.root / "auth" / "session-1"
        upload_dir = self.root / "uploads" / "session-1"
        for directory in (artifact_dir, auth_dir, upload_dir):
            directory.mkdir(parents=True, exist_ok=True)
        session = BrowserSession(
            id="session-1",
            name="fixture",
            created_at=datetime.now(UTC),
            context=context,  # type: ignore[arg-type]
            page=page,  # type: ignore[arg-type]
            artifact_dir=artifact_dir,
            auth_dir=auth_dir,
            upload_dir=upload_dir,
            takeover_url="http://localhost:6080/vnc.html",
            trace_path=artifact_dir / "trace.zip",
        )
        self.manager.sessions[session.id] = session
        return session

    async def test_action_wrappers_execute_real_operations(self) -> None:
        password = FakeLocator("#password", attributes={"type": "password"})
        self.session.page.locators["#password"] = password

        with patch("app.browser_manager.asyncio.sleep", new=AsyncMock()):
            click_result = await self.manager.click("session-1", selector="#submit")
            coordinate_click = await self.manager.click("session-1", x=4, y=8)
            hover_result = await self.manager.hover("session-1", selector="#menu")
            coordinate_hover = await self.manager.hover("session-1", x=12, y=16)
            option_result = await self.manager.select_option("session-1", selector="#choice", value="one")
            text_result = await self.manager.type("session-1", selector="#name", text="hello")
            secret_result = await self.manager.type("session-1", selector="#password", text="secret")
            press_result = await self.manager.press("session-1", "Enter")
            scroll_result = await self.manager.scroll("session-1", 0, 250)
            wait_result = await self.manager.wait("session-1", -25)
            reload_result = await self.manager.reload("session-1")
            back_result = await self.manager.go_back("session-1")
            forward_result = await self.manager.go_forward("session-1")

        self.assertEqual(click_result["target"]["selector"], "#submit")
        self.assertEqual(coordinate_click["target"]["mode"], "coordinates")
        self.assertEqual(hover_result["target"]["selector"], "#menu")
        self.assertEqual(coordinate_hover["target"]["x"], 12)
        self.session.page.locators["#choice"].select_option.assert_awaited_once_with(value="one")
        self.assertEqual(option_result["target"]["value"], "one")
        self.assertEqual(text_result["target"]["text_preview"], "hello")
        self.assertTrue(secret_result["target"]["text_redacted"])
        self.assertEqual(press_result["target"]["key"], "Enter")
        self.assertEqual(scroll_result["target"]["delta_y"], 250)
        self.assertEqual(wait_result["action"], "wait")
        self.session.page.reload.assert_awaited_once_with(wait_until="domcontentloaded")
        self.session.page.go_back.assert_awaited_once_with(wait_until="domcontentloaded")
        self.session.page.go_forward.assert_awaited_once_with(wait_until="domcontentloaded")
        self.assertEqual(reload_result["action"], "reload")
        self.assertEqual(back_result["action"], "go_back")
        self.assertEqual(forward_result["action"], "go_forward")

    async def test_tabs_diagnostics_takeover_and_trace_use_session_state(self) -> None:
        self.session.console_messages = [{"type": "log", "text": "ready", "location": {}}]
        self.session.page_errors = ["boom"]
        self.session.request_failures = [{"url": "https://example.com/fail", "method": "GET"}]
        self.session.downloads = [{"filename": "report.csv", "status": "ok"}]

        console = await self.manager.get_console_messages("session-1")
        errors = await self.manager.get_page_errors("session-1")
        failures = await self.manager.get_request_failures("session-1")
        downloads = await self.manager.list_downloads("session-1")
        trace = await self.manager.stop_trace("session-1")
        initial_tabs = await self.manager.list_tabs("session-1")
        opened = await self.manager.open_tab("session-1", "https://example.com/second", activate=False)
        activated = await self.manager.activate_tab("session-1", 1)
        closed = await self.manager.close_tab("session-1", 0)
        takeover = await self.manager.request_human_takeover("session-1", "operator check")

        self.assertEqual(console["items"][0]["text"], "ready")
        self.assertEqual(errors["items"], ["boom"])
        self.assertEqual(failures["items"][0]["url"], "https://example.com/fail")
        self.assertEqual(downloads[0]["filename"], "report.csv")
        self.assertTrue(trace["trace_available"])
        self.assertEqual(initial_tabs[0]["index"], 0)
        self.assertEqual(opened["index"], 1)
        self.assertFalse(opened["activated"])
        self.assertEqual(activated["index"], 1)
        self.assertEqual(closed["closed_index"], 0)
        self.assertEqual(len(closed["tabs"]), 1)
        self.assertEqual(takeover["reason"], "operator check")
        self.manager._persist_session.assert_awaited()

    async def test_run_action_records_success_and_normalized_failures(self) -> None:
        before = {
            "url": "https://example.com",
            "title": "Before",
            "active_element": {"tag": "body"},
            "text_excerpt": "before",
            "dom_outline": {"counts": {"button": 1}},
            "accessibility_outline": {"focused": None},
            "screenshot_path": "before.png",
            "screenshot_url": "/before.png",
        }
        after = {
            **before,
            "title": "After",
            "text_excerpt": "after",
            "interactables": [{"selector_hint": "#submit"}],
            "screenshot_path": "after.png",
            "screenshot_url": "/after.png",
        }
        failed = {**before, "screenshot_path": "failed.png", "screenshot_url": "/failed.png"}
        allowed = SimpleNamespace(should_block=False, block_reason=None, require_approval=False)
        blocked = SimpleNamespace(should_block=True, block_reason="policy says no", require_approval=True)

        self.manager.witness_policy.evaluate_action = Mock(return_value=allowed)
        self.manager._light_snapshot = AsyncMock(side_effect=[before, before, failed, before, failed])
        self.manager._observation_payload = AsyncMock(return_value=after)  # type: ignore[method-assign]
        self.manager._ensure_witness_remote_ready = AsyncMock()  # type: ignore[method-assign]
        self.manager._check_bot_challenge = AsyncMock(return_value=None)  # type: ignore[method-assign]

        async def successful_operation() -> None:
            self.session.page.url = "https://example.com/after"

        success = await BrowserManager._run_action(
            self.manager,
            self.session,
            "click",
            {"selector": "#submit"},
            successful_operation,
        )
        self.assertEqual(success["action"], "click")
        self.assertTrue(success["verification"]["verified"])
        self.assertEqual(self.session.last_action, "click")

        self.manager.witness_policy.evaluate_action = Mock(return_value=blocked)
        with self.assertRaises(BrowserActionError) as blocked_error:
            await BrowserManager._run_action(
                self.manager,
                self.session,
                "click",
                {"selector": "#submit", "approval_id": "approval-1"},
                successful_operation,
            )
        self.assertEqual(blocked_error.exception.code, "browser_action_blocked")

        self.manager.witness_policy.evaluate_action = Mock(return_value=allowed)

        async def playwright_failure() -> None:
            raise PlaywrightError("detached")

        with self.assertRaises(BrowserActionError) as failed_error:
            await BrowserManager._run_action(
                self.manager,
                self.session,
                "click",
                {"selector": "#submit"},
                playwright_failure,
            )
        self.assertEqual(failed_error.exception.code, "browser_action_failed")
        self.assertGreaterEqual(self.manager.audit.append.await_count, 3)

    async def test_remote_access_metadata_and_isolated_takeover_resolution(self) -> None:
        info_path = Path(self.manager.settings.remote_access_info_path)
        info_path.parent.mkdir(parents=True, exist_ok=True)

        inactive = self.manager.get_remote_access_info()
        self.assertFalse(inactive["active"])
        self.assertEqual(inactive["source"], "static")

        info_path.write_text("{not json", encoding="utf-8")
        unreadable = self.manager.get_remote_access_info()
        self.assertEqual(unreadable["status"], "error")
        self.assertEqual(unreadable["error"], "remote_access_metadata_unreadable")

        info_path.write_text(
            json.dumps(
                {
                    "status": "active",
                    "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "stale_after_seconds": 3600,
                    "public_takeover_url": "https://takeover.example.com",
                    "public_api_url": "https://api.example.com",
                }
            ),
            encoding="utf-8",
        )
        active = self.manager.get_remote_access_info()
        self.assertTrue(active["active"])
        self.assertEqual(active["takeover_url"], "https://takeover.example.com")
        self.assertEqual(self.manager._current_takeover_url(), "https://takeover.example.com")
        self.assertIsNotNone(BrowserManager._parse_remote_access_timestamp("2026-01-01T00:00:00Z"))
        self.assertIsNone(BrowserManager._parse_remote_access_timestamp("not-a-date"))
        self.assertTrue(BrowserManager._takeover_url_is_local_only("http://127.0.0.1:6080"))
        self.assertFalse(BrowserManager._takeover_url_is_local_only("https://takeover.example.com"))

        self.session.isolation_mode = "docker_ephemeral"
        self.session.takeover_url = "http://127.0.0.1:6080/vnc.html"
        self.manager.tunnel_broker.describe = Mock(return_value=None)
        local_only = self.manager.get_remote_access_info("session-1")
        self.assertEqual(local_only["status"], "api_only")
        self.assertTrue(local_only["requires_direct_host_access"])

        self.manager.tunnel_broker.describe = Mock(
            return_value={"active": True, "public_takeover_url": "https://session-tunnel.example.com"}
        )
        tunneled = self.manager.get_remote_access_info("session-1")
        self.assertEqual(tunneled["source"], "isolated_session_tunnel")
        self.assertEqual(tunneled["takeover_url"], "https://session-tunnel.example.com")
        self.assertEqual(self.manager._current_takeover_url(self.session), "https://session-tunnel.example.com")


if __name__ == "__main__":
    unittest.main()
