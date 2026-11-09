from __future__ import annotations

import asyncio
import hashlib
import logging
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from playwright.async_api import Error as PlaywrightError
from pydantic import ValidationError

from app.action_records import wait_for_pending_record
from app.tool_gateway import McpToolGateway
from app.tool_inputs import GetStorageInput, SetCookiesInput, SetStorageInput, SetViewportInput
from tests.test_approval_spend import SESSION, SpendBase

# Goes in every cookie value and every storage value; neither it nor its SHA-256 may reach a record.
CANARY = "canary-secret-value-90c1"
CANARY_SHA256 = hashlib.sha256(CANARY.encode("utf-8")).hexdigest()
WRITE_RECORDS_LOGGER = "app.tool_gateway.write_records"
# The tool, its arguments, what the record's digest says of them, and the receipt's action_class.
WRITES = (
    (
        "browser.set_viewport",
        {"width": 800, "height": 600},
        {"width": 800, "height": 600},
        "write",
    ),
    (
        "browser.set_cookies",
        {
            "cookies": [
                {"name": "sid", "value": CANARY, "domain": "example.com", "path": "/"},
                {"name": "lang", "value": CANARY, "url": "https://example.com/account?next=" + CANARY},
            ]
        },
        {"cookies": [{"name": "sid", "domain": "example.com"}, {"name": "lang", "url": "https://example.com"}]},
        "account_change",
    ),
    (
        "browser.set_local_storage",
        {"storage_type": "session", "key": "theme", "value": CANARY},
        {"storage_type": "session", "key": "theme"},
        "account_change",
    ),
)


def _build_gateway() -> tuple[McpToolGateway, SimpleNamespace, SimpleNamespace]:
    page = SimpleNamespace(evaluate=AsyncMock(return_value=None))
    session = SimpleNamespace(page=page)
    manager = SimpleNamespace(get_session=AsyncMock(return_value=session))
    gateway = McpToolGateway(
        manager=manager,
        orchestrator=SimpleNamespace(list_providers=lambda: []),
        job_queue=SimpleNamespace(),
        tool_profile="full",
    )
    return gateway, manager, page


class ToolGatewayStorageInputTests(unittest.TestCase):
    def test_get_storage_local(self) -> None:
        payload = GetStorageInput(session_id="s1", storage_type="local")
        self.assertEqual(payload.storage_type, "local")

    def test_get_storage_session(self) -> None:
        payload = GetStorageInput(session_id="s1", storage_type="session")
        self.assertEqual(payload.storage_type, "session")

    def test_get_storage_invalid(self) -> None:
        with self.assertRaises(ValidationError):
            GetStorageInput(session_id="s1", storage_type="constructor")

    def test_set_storage_invalid(self) -> None:
        with self.assertRaises(ValidationError):
            SetStorageInput(session_id="s1", key="k", value="v", storage_type="__proto__")

    def test_set_storage_valid(self) -> None:
        payload = SetStorageInput(session_id="s1", key="k", value="v", storage_type="session")
        self.assertEqual(payload.storage_type, "session")


class ToolGatewayStorageRuntimeGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_storage_runtime_guard_rejects_invalid_storage_type(self) -> None:
        gateway, manager, _ = _build_gateway()
        payload = GetStorageInput.model_construct(session_id="s1", key="k", storage_type="constructor")

        with self.assertRaisesRegex(ValueError, "Invalid storage_type"):
            await gateway._get_local_storage(payload)

        manager.get_session.assert_not_awaited()

    async def test_set_storage_runtime_guard_rejects_invalid_storage_type(self) -> None:
        gateway, manager, _ = _build_gateway()
        payload = SetStorageInput.model_construct(session_id="s1", key="k", value="v", storage_type="__proto__")

        with self.assertRaisesRegex(ValueError, "Invalid storage_type"):
            await gateway._set_local_storage(payload)

        manager.get_session.assert_not_awaited()


class GetStorageKeyTests(SpendBase):
    async def test_a_key_is_the_evaluate_argument_and_never_part_of_the_script(self) -> None:
        # A key spliced in as a Python repr is not a JavaScript string: past the BMP, repr can write \U0010ffff.
        for key in ("it's", 'say "hi"', "back\\slash", "new\nline", "\U0001f600", "\U0010ffff"):
            with self.subTest(key=ascii(key)):
                self.page.evaluated.clear()
                self.page.evaluated_args.clear()

                await self.gateway._get_local_storage(GetStorageInput(session_id=SESSION, key=key))

                self.assertEqual(self.page.evaluated_args, [key])
                self.assertNotIn(key, self.page.evaluated[0])


class GatewayWriteRecordTests(SpendBase):
    """The three state changes the gateway makes itself (MCP-23).

    browser.set_viewport, set_cookies and set_local_storage left no audit entry and no witness receipt in the fast
    profile. These run the real audit store and witness chain over a fake page and read the files they write.
    """

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.page.set_viewport_size = AsyncMock()
        self.context = SimpleNamespace(add_cookies=AsyncMock())
        self.session.context = self.context

    async def records(self, tool: str) -> tuple[list[Any], list[dict[str, Any]]]:
        events = [event for event in await self.manager.audit.list(session_id=SESSION) if event.action == tool]
        receipts = [item for item in await self.manager.list_witness_receipts(SESSION) if item["action"] == tool]
        return events, receipts

    def assert_no_value_recorded(self) -> None:
        roots = [Path(self.settings.audit_root), Path(self.settings.witness_root)]
        files = [path for root in roots for path in root.rglob("*") if path.is_file()]
        self.assertTrue(any(path.name == "events.jsonl" for path in files), files)
        self.assertTrue(any(path.suffix == ".jsonl" and path.name != "events.jsonl" for path in files), files)
        for path in files:
            data = path.read_bytes()
            self.assertNotIn(CANARY.encode("utf-8"), data, path)
            self.assertNotIn(CANARY_SHA256.encode("ascii"), data, path)

    async def assert_recorded_failed(self, tool: str, *, error_name: str, began: bool) -> None:
        events, receipts = await self.records(tool)
        self.assertEqual([event.status for event in events], ["failed"])
        self.assertEqual(events[0].details["error"], error_name)
        self.assertEqual(events[0].details["reached_page"], began)
        self.assertEqual([item["status"] for item in receipts], ["failed"])
        self.assertEqual(receipts[0]["metadata"]["error"], error_name)
        self.assertEqual(receipts[0]["metadata"]["reached_page"], began)

    async def test_each_write_leaves_one_audit_entry_one_receipt_and_no_value(self) -> None:
        for tool, arguments, digest, action_class in WRITES:
            with self.subTest(tool=tool):
                response = await self.call(tool, session_id=SESSION, **arguments)

                self.assertFalse(response.isError, response.content[0].text)
                events, receipts = await self.records(tool)
                self.assertEqual([(event.status, event.details["digest"]) for event in events], [("ok", digest)])
                self.assertEqual([(item["status"], item["target"]["digest"]) for item in receipts], [("ok", digest)])
                self.assertEqual(events[0].session_id, SESSION)
                self.assertEqual((receipts[0]["session_id"], receipts[0]["action_class"]), (SESSION, action_class))
                self.assert_no_value_recorded()

    async def test_a_cookie_write_refused_for_its_host_records_failed_and_keeps_its_error(self) -> None:
        payload = SetCookiesInput(
            session_id=SESSION, cookies=[{"name": "sid", "value": CANARY, "domain": "evil.example"}]
        )

        with self.assertRaisesRegex(PermissionError, "is not allowlisted"):
            await self.gateway._set_cookies(payload)

        self.context.add_cookies.assert_not_awaited()
        await self.assert_recorded_failed("browser.set_cookies", error_name="PermissionError", began=False)

    async def test_a_playwright_error_records_failed_and_the_same_error_goes_on(self) -> None:
        cases = (
            ("browser.set_viewport", SetViewportInput(session_id=SESSION, width=800, height=600), "_set_viewport"),
            (
                "browser.set_cookies",
                SetCookiesInput(
                    session_id=SESSION, cookies=[{"name": "sid", "value": CANARY, "domain": "example.com"}]
                ),
                "_set_cookies",
            ),
            (
                "browser.set_local_storage",
                SetStorageInput(session_id=SESSION, key="theme", value=CANARY),
                "_set_local_storage",
            ),
        )
        for tool, payload, handler in cases:
            with self.subTest(tool=tool):
                error = PlaywrightError(f"rejected: {CANARY}")
                self.page.set_viewport_size = AsyncMock(side_effect=error)
                self.context.add_cookies = AsyncMock(side_effect=error)
                self.page.evaluate_error = error

                with self.assertRaises(PlaywrightError) as raised:
                    await getattr(self.gateway, handler)(payload)

                self.assertIs(raised.exception, error)
                await self.assert_recorded_failed(tool, error_name="Error", began=True)
                self.assert_no_value_recorded()

    async def test_a_storage_write_whose_page_moved_records_failed(self) -> None:
        def moved_while_it_was_written(*_args: Any) -> None:
            self.manager.page_provenance.committed(self.page, "https://elsewhere.test/", url_refused=True)

        self.page.evaluate = AsyncMock(side_effect=moved_while_it_was_written)  # type: ignore[method-assign]
        payload = SetStorageInput(session_id=SESSION, key="theme", value=CANARY)

        with self.assertRaisesRegex(PermissionError, "moved while it was read"):
            await self.gateway._set_local_storage(payload)

        await self.assert_recorded_failed("browser.set_local_storage", error_name="PermissionError", began=True)

    async def test_a_storage_write_on_a_refused_page_records_failed_before_touching_it(self) -> None:
        self.page.url = "https://elsewhere.test/"
        self.page.evaluate = AsyncMock()  # type: ignore[method-assign]
        payload = SetStorageInput(session_id=SESSION, key="theme", value=CANARY)

        with self.assertRaises(PermissionError):
            await self.gateway._set_local_storage(payload)

        self.page.evaluate.assert_not_awaited()
        await self.assert_recorded_failed("browser.set_local_storage", error_name="RefusedPageError", began=False)

    async def test_a_call_cancelled_after_it_reached_the_page_records_an_unknown_outcome(self) -> None:
        reached, hang = asyncio.Event(), asyncio.Event()

        async def set_viewport_size(_size: dict[str, int]) -> None:
            reached.set()
            await hang.wait()

        self.page.set_viewport_size = set_viewport_size
        task = asyncio.ensure_future(
            self.gateway._set_viewport(SetViewportInput(session_id=SESSION, width=800, height=600))
        )
        await reached.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task
        await wait_for_pending_record(self.session)

        events, receipts = await self.records("browser.set_viewport")
        self.assertEqual([event.status for event in events], ["error"])
        self.assertIn("outcome unknown", events[0].details["error"])
        self.assertEqual([item["status"] for item in receipts], ["error"])
        self.assertTrue(receipts[0]["metadata"]["reached_page"])

    async def test_a_call_cancelled_before_it_reached_the_page_records_nothing(self) -> None:
        # As the action pipeline does for an action that had not begun: nothing ran, so there is nothing to record.
        await self.session.lock.acquire()
        self.addCleanup(self.session.lock.release)
        task = asyncio.ensure_future(
            self.gateway._set_local_storage(SetStorageInput(session_id=SESSION, key="theme", value=CANARY))
        )
        await asyncio.sleep(0.05)
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task
        await wait_for_pending_record(self.session)

        self.assertEqual(await self.records("browser.set_local_storage"), ([], []))

    async def test_a_record_that_cannot_be_written_never_replaces_the_calls_own_error(self) -> None:
        error = PlaywrightError("rejected")
        self.context.add_cookies = AsyncMock(side_effect=error)
        self.manager.audit.append = AsyncMock(side_effect=OSError("disk full"))  # type: ignore[method-assign]
        payload = SetCookiesInput(
            session_id=SESSION, cookies=[{"name": "sid", "value": CANARY, "domain": "example.com"}]
        )

        with self.assertRaises(PlaywrightError) as raised:
            await self.gateway._set_cookies(payload)

        self.assertIs(raised.exception, error)

    async def test_a_record_that_cannot_be_written_fails_a_write_that_ran_as_an_action_does(self) -> None:
        # The action pipeline raises then too: an unrecorded change is not reported as done.
        self.manager.audit.append = AsyncMock(side_effect=OSError("disk full"))  # type: ignore[method-assign]
        payload = SetCookiesInput(
            session_id=SESSION, cookies=[{"name": "sid", "value": CANARY, "domain": "example.com"}]
        )

        with self.assertRaisesRegex(OSError, "disk full"):
            await self.gateway._set_cookies(payload)

    async def test_a_governed_write_records_once(self) -> None:
        arguments = {
            "session_id": SESSION,
            "workflow_profile": "governed",
            "storage_type": "local",
            "key": "theme",
            "value": CANARY,
        }
        ask = await self.call("browser.set_local_storage", **arguments)
        approval_id = ask.structuredContent["approval_id"]
        await self.manager.approve(approval_id)

        done = await self.call("browser.set_local_storage", **arguments, approval_id=approval_id)

        self.assertFalse(done.isError, done.content[0].text)
        events, receipts = await self.records("browser.set_local_storage")
        self.assertEqual(([event.status for event in events], [item["status"] for item in receipts]), (["ok"], ["ok"]))

    def hold_the_audit_write(self) -> tuple[asyncio.Event, asyncio.Event]:
        """The first event is set once a record's audit write has begun, and that write goes on when the second is."""
        recording, release = asyncio.Event(), asyncio.Event()
        append = self.manager.audit.append

        async def held_append(**kwargs: Any) -> Any:
            recording.set()
            await release.wait()
            return await append(**kwargs)

        self.manager.audit.append = held_append  # type: ignore[method-assign]
        return recording, release

    def assert_logged_the_class_and_no_text(self, records: list[logging.LogRecord], error_class: str) -> None:
        # As a handler would print them: a traceback, and the __context__ chain it follows, count.
        formatted = "\n".join(logging.Formatter().format(record) for record in records)
        self.assertRegex(formatted, rf"\b{error_class}\b")
        self.assertNotIn(CANARY, formatted)
        self.assertNotIn("Traceback", formatted)

    async def test_a_cancellation_while_a_failure_is_recorded_logs_its_class_and_not_its_text(self) -> None:
        recording, release = self.hold_the_audit_write()
        self.context.add_cookies = AsyncMock(side_effect=PlaywrightError(f"rejected: {CANARY}"))
        payload = SetCookiesInput(
            session_id=SESSION, cookies=[{"name": "sid", "value": CANARY, "domain": "example.com"}]
        )

        with self.assertLogs(WRITE_RECORDS_LOGGER, level="WARNING") as logs:
            task = asyncio.ensure_future(self.gateway._set_cookies(payload))
            await recording.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        release.set()
        await wait_for_pending_record(self.session)

        self.assert_logged_the_class_and_no_text(logs.records, "Error")

    async def test_a_record_that_fails_logs_the_errors_class_and_its_own_repr_only(self) -> None:
        # The recording's own exception is raised while the original is handled, so its __context__ is the original.
        self.context.add_cookies = AsyncMock(side_effect=PlaywrightError(f"rejected: {CANARY}"))
        self.manager.audit.append = AsyncMock(side_effect=OSError("disk full"))  # type: ignore[method-assign]
        payload = SetCookiesInput(
            session_id=SESSION, cookies=[{"name": "sid", "value": CANARY, "domain": "example.com"}]
        )

        with self.assertLogs(WRITE_RECORDS_LOGGER, level="WARNING") as logs:
            with self.assertRaises(PlaywrightError):
                await self.gateway._set_cookies(payload)

        self.assert_logged_the_class_and_no_text(logs.records, "Error")
        self.assertIn(repr(OSError("disk full")), logs.output[0])

    async def test_a_second_cancellation_while_a_cancellation_is_recorded_is_absorbed(self) -> None:
        reached, hang = asyncio.Event(), asyncio.Event()

        async def set_viewport_size(_size: dict[str, int]) -> None:
            reached.set()
            await hang.wait()

        self.page.set_viewport_size = set_viewport_size
        recording, release = self.hold_the_audit_write()
        task = asyncio.ensure_future(
            self.gateway._set_viewport(SetViewportInput(session_id=SESSION, width=800, height=600))
        )
        with self.assertLogs(WRITE_RECORDS_LOGGER, level="WARNING") as logs:
            await reached.wait()
            task.cancel()
            await recording.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        release.set()
        await wait_for_pending_record(self.session)

        self.assertIn("cancelled again", logs.output[0])
        events, receipts = await self.records("browser.set_viewport")
        self.assertEqual([event.status for event in events], ["error"])
        self.assertEqual([item["status"] for item in receipts], ["error"])

    async def test_a_cancellation_while_a_success_is_recorded_lands_ok_and_ends_cancelled(self) -> None:
        recording, release = self.hold_the_audit_write()
        task = asyncio.ensure_future(
            self.gateway._set_viewport(SetViewportInput(session_id=SESSION, width=800, height=600))
        )

        await recording.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        release.set()
        await wait_for_pending_record(self.session)

        events, receipts = await self.records("browser.set_viewport")
        self.assertEqual(([event.status for event in events], [item["status"] for item in receipts]), (["ok"], ["ok"]))
