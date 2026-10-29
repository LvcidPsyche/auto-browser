"""Where an element_id upload marks its effect: ElementTarget.set_input_files' own code, without a browser.

The upload spends its approval at before_transfer, which set_input_files calls
once the marker is checked to be alone on the registered element for the last
time, and just before Playwright's transfer. Earlier, and a marker the page
moved would spend the approval on an upload that never began; later, and a
transfer that put the file in and then lost its reply would give the approval
back, so a retry would upload the file again. The world and the page are
fakes that answer each script ElementTarget sends; the code under test is the
real ElementTarget, reached through BrowserManager.upload.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

from playwright.async_api import Error as PlaywrightError

from app.action_errors import BrowserActionError
from app.approvals import ApprovalRequiredError
from app.browser.element_scripts import (
    UPLOAD_LANDED_SCRIPT,
    UPLOAD_MARK_SCRIPT,
    UPLOAD_MARKED_ALONE_SCRIPT,
    UPLOAD_UNMARK_SCRIPT,
)
from app.browser.element_target import ElementTarget
from tests.test_approval_spend import SESSION, SpendBase

ELEMENT = "op-" + "a" * 24
WORLD_TIMEOUT_MS = 1000


class FakeUploadWorld:
    """The isolated world as set_input_files talks to it: each script answered by which one it is."""

    timeout_ms = WORLD_TIMEOUT_MS

    def __init__(self, *, file_input: bool, alone: list[bool]) -> None:
        self.file_input = file_input
        self.alone = list(alone)  # each check that the marker is alone on the registered element, in order
        self.unmarked = 0

    async def call(self, page: Any, script: str, *args: Any, timeout_ms: int | None = None, effect: str) -> Any:
        if script == UPLOAD_UNMARK_SCRIPT:
            self.unmarked += 1
            return None
        if UPLOAD_MARKED_ALONE_SCRIPT in script:
            value: Any = self.alone.pop(0)
        elif UPLOAD_MARK_SCRIPT in script:
            value = self.file_input
        elif UPLOAD_LANDED_SCRIPT in script:
            value = None
        else:
            raise AssertionError(f"a script set_input_files does not send: {script[:80]!r}")
        return {"status": "ok", "tag": "document-1", "value": value}

    def lookup_error(self, element_id: str, status: str, tag: str) -> Exception:
        raise AssertionError(f"looked up {element_id}: {status}")


class FakeMarkedInput:
    """The handle Playwright resolves the marker to; its transfer puts the file in, and may lose the reply."""

    def __init__(self, page: Any, *, reply_lost: bool) -> None:
        self.page = page
        self.reply_lost = reply_lost

    async def set_input_files(self, path: str) -> None:
        self.page.uploads.append(path)
        if self.reply_lost:
            raise PlaywrightError("Timeout 30000ms exceeded.")

    async def dispose(self) -> None:
        return None


class FakeMarkerPage:
    def __init__(self, handle: FakeMarkedInput) -> None:
        self.handle = handle

    def locator(self, selector: str) -> Any:
        async def element_handle(timeout: int) -> FakeMarkedInput:
            return self.handle

        return Mock(element_handle=element_handle)


class ElementUploadBoundaryTests(SpendBase):
    async def run_upload(self, world: FakeUploadWorld, *, reply_lost: bool = False) -> str:
        with self.assertRaises(ApprovalRequiredError) as ctx:
            await self.manager.upload(SESSION, element_id=ELEMENT, file_path="demo.txt", approved=False)
        approval_id = ctx.exception.approval.id
        await self.manager.approve(approval_id)
        page = FakeMarkerPage(FakeMarkedInput(self.page, reply_lost=reply_lost))
        target = ElementTarget(world, page, ELEMENT)  # type: ignore[arg-type]
        self.manager.element_ids.element = Mock(return_value=target)  # type: ignore[method-assign]

        with self.assertRaises(BrowserActionError) as failed:
            await self.manager.upload(
                SESSION, element_id=ELEMENT, file_path="demo.txt", approved=False, approval_id=approval_id
            )
        self.failure = failed.exception
        return approval_id

    async def test_an_element_that_is_no_file_input_gives_the_approval_back(self) -> None:
        world = FakeUploadWorld(file_input=False, alone=[])

        approval_id = await self.run_upload(world)

        self.assertEqual(self.failure.code, "element_not_a_file_input")
        self.assertEqual((self.page.uploads, await self.status(approval_id)), ([], "approved"))

    async def test_a_marker_moved_after_it_was_resolved_gives_the_approval_back(self) -> None:
        # The last check, after the handle: the page moved the marker in between.
        world = FakeUploadWorld(file_input=True, alone=[True, False])

        approval_id = await self.run_upload(world)

        self.assertEqual(self.failure.code, "element_target_moved")
        self.assertEqual((self.page.uploads, await self.status(approval_id)), ([], "approved"))
        self.assertEqual((world.alone, world.unmarked), ([], 1))  # both checks ran, and the marker went

    async def test_a_transfer_that_lost_its_reply_keeps_the_approval_spent(self) -> None:
        world = FakeUploadWorld(file_input=True, alone=[True, True])

        approval_id = await self.run_upload(world, reply_lost=True)

        self.assertEqual((len(self.page.uploads), await self.status(approval_id)), (1, "executed"))
        self.assertEqual((world.alone, world.unmarked), ([], 1))  # both checks ran, and the marker went
