"""A session's upload directory belongs to the operator who owns that session.

UPLOAD_ROOT/<session_id>/ is where one session's files are staged, but the upload
path check also allowed the shared UPLOAD_ROOT, and `<other_session_id>/file`
(or the absolute path) is inside it. Under named credentials (API_BEARER_TOKENS)
operator Bob could therefore upload a file staged for Alice's session into a page
of their own. Files directly in UPLOAD_ROOT stay shared, and unowned deployments
(shared token, no token) see no change.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.approvals import ApprovalRequiredError
from app.browser_manager import BrowserManager
from app.models import BrowserActionDecision, OperatorIdentity
from app.session_ownership import acting_as, as_system
from tests.test_browser_manager_create_session import FakeBrowser, FakeContext, FakePage, _settings

ALICE = OperatorIdentity(id="alice", source="token")
BOB = OperatorIdentity(id="bob", source="token")
SELECTOR = 'input[type="file"]'


def build_manager(root: Path) -> BrowserManager:
    manager = BrowserManager(_settings(root))
    manager.settings.max_sessions = 5
    manager._settle = AsyncMock()  # type: ignore[method-assign]
    manager._maybe_provision_session_tunnel = AsyncMock()  # type: ignore[method-assign]
    browser = FakeBrowser(FakeContext(FakePage()))
    browser.new_context = AsyncMock(side_effect=lambda **_kwargs: FakeContext(FakePage()))
    manager.browser = browser  # type: ignore[assignment]
    manager._acquire_session_browser = AsyncMock(return_value=(browser, None))  # type: ignore[method-assign]
    return manager


class UploadIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = build_manager(self.root)
        self.upload_root = Path(self.manager.settings.upload_root)

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def _session(self, operator: OperatorIdentity | None, name: str) -> str:
        with acting_as(operator):
            return (await self.manager.create_session(name=name))["id"]

    def _stage(self, session_id: str, name: str = "secret.pdf") -> Path:
        path = self.upload_root / session_id / name
        path.write_text("PRIVATE", encoding="utf-8")
        return path

    async def _upload(self, operator: OperatorIdentity | None, session_id: str, file_path: str) -> None:
        """Run an upload; passing the path check ends at the approval gate."""
        with acting_as(operator):
            await self.manager.upload(session_id, file_path=file_path, approved=False, selector=SELECTOR)

    async def _assert_passes_path_check(self, operator: OperatorIdentity | None, session_id: str, file_path: str):
        with self.assertRaises(ApprovalRequiredError):
            await self._upload(operator, session_id, file_path)

    # -- the leak ---------------------------------------------------------

    async def test_another_operator_cannot_upload_from_a_live_sessions_directory(self) -> None:
        alice = await self._session(ALICE, "a")
        bob = await self._session(BOB, "b")
        self._stage(alice)

        with self.assertRaises(PermissionError):
            await self._upload(BOB, bob, f"{alice}/secret.pdf")

    async def test_another_operator_cannot_upload_by_absolute_path_into_it(self) -> None:
        alice = await self._session(ALICE, "a")
        bob = await self._session(BOB, "b")
        secret = self._stage(alice)

        with self.assertRaises(PermissionError):
            await self._upload(BOB, bob, str(secret))

    async def test_another_operator_cannot_climb_out_of_their_directory_into_it(self) -> None:
        alice = await self._session(ALICE, "a")
        bob = await self._session(BOB, "b")
        self._stage(alice)

        with self.assertRaises(PermissionError):
            await self._upload(BOB, bob, f"../{alice}/secret.pdf")

    async def test_the_refusal_holds_after_the_other_session_closed(self) -> None:
        alice = await self._session(ALICE, "a")
        bob = await self._session(BOB, "b")
        secret = self._stage(alice)
        with acting_as(ALICE):
            await self.manager.close_session(alice)

        for file_path in (f"{alice}/secret.pdf", str(secret)):
            with self.assertRaises(PermissionError):
                await self._upload(BOB, bob, file_path)

    async def test_a_missing_file_in_anothers_directory_looks_the_same_as_a_present_one(self) -> None:
        alice = await self._session(ALICE, "a")
        bob = await self._session(BOB, "b")
        self._stage(alice)

        with self.assertRaises(PermissionError):
            await self._upload(BOB, bob, f"{alice}/there-is-no-such-file.pdf")

    async def test_the_approval_path_checks_the_file_before_asking_anyone(self) -> None:
        alice = await self._session(ALICE, "a")
        bob = await self._session(BOB, "b")
        self._stage(alice)
        decision = BrowserActionDecision(
            action="upload", reason="stage it", selector=SELECTOR, file_path=f"{alice}/secret.pdf"
        )

        with acting_as(BOB), self.assertRaises(PermissionError):
            await self.manager.execute_decision(bob, decision)
        self.assertEqual(await self.manager.list_approvals(), [])

    # -- what must keep working -------------------------------------------

    async def test_a_session_can_still_upload_its_own_file(self) -> None:
        alice = await self._session(ALICE, "a")
        self._stage(alice)
        secret = self.upload_root / alice / "secret.pdf"

        await self._assert_passes_path_check(ALICE, alice, "secret.pdf")
        await self._assert_passes_path_check(ALICE, alice, f"{alice}/secret.pdf")
        await self._assert_passes_path_check(ALICE, alice, str(secret))

    async def test_a_file_in_the_shared_upload_root_is_open_to_every_operator(self) -> None:
        bob = await self._session(BOB, "b")
        (self.upload_root / "shared.pdf").write_text("PUBLIC", encoding="utf-8")

        await self._assert_passes_path_check(BOB, bob, "shared.pdf")
        await self._assert_passes_path_check(BOB, bob, str(self.upload_root / "shared.pdf"))

    async def test_an_operator_can_still_reach_files_of_their_other_session(self) -> None:
        first = await self._session(ALICE, "first")
        second = await self._session(ALICE, "second")
        self._stage(first)

        await self._assert_passes_path_check(ALICE, second, f"{first}/secret.pdf")
        with acting_as(ALICE):
            await self.manager.close_session(first)
        await self._assert_passes_path_check(ALICE, second, f"{first}/secret.pdf")

    async def test_a_plain_subfolder_of_the_shared_root_is_not_a_session_directory(self) -> None:
        bob = await self._session(BOB, "b")
        (self.upload_root / "invoices").mkdir()
        (self.upload_root / "invoices" / "march.pdf").write_text("x", encoding="utf-8")

        await self._assert_passes_path_check(BOB, bob, "invoices/march.pdf")

    async def test_without_named_credentials_nothing_changes(self) -> None:
        first = await self._session(None, "first")
        second = await self._session(None, "second")
        self._stage(first)

        await self._assert_passes_path_check(None, second, f"{first}/secret.pdf")
        await self._assert_passes_path_check(None, second, str(self.upload_root / first / "secret.pdf"))

    async def test_an_unowned_session_directory_is_open_to_a_named_operator(self) -> None:
        unowned = await self._session(None, "shared-token")
        bob = await self._session(BOB, "b")
        self._stage(unowned)

        await self._assert_passes_path_check(BOB, bob, f"{unowned}/secret.pdf")

    async def test_the_system_may_reach_any_session_directory(self) -> None:
        alice = await self._session(ALICE, "a")
        bob = await self._session(BOB, "b")
        self._stage(alice)

        with as_system(), self.assertRaises(ApprovalRequiredError):
            await self.manager.upload(bob, file_path=f"{alice}/secret.pdf", approved=False, selector=SELECTOR)


if __name__ == "__main__":
    unittest.main()
