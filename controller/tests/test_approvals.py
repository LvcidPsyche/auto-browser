from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock

from app.action_errors import BrowserActionError
from app.approvals import ApprovalRequiredError, ApprovalStore, held_for_execution, refused_before_effect
from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.models import BrowserActionDecision
from app.utils import UTC, utc_now

# How long the other call is given to approve and spend while the cancelled approve's write waits.
OTHER_CALL_SECONDS = 1.0
# A secret a stored record holds: no log line may carry it.
LOGGED_SENTINEL = "FAKE-SENTINEL-TOKEN"


class ProcessKilled(BaseException):
    """Stands in for the process dying where it is raised: no handler of the code under test runs after it."""


def stored_by_1_11_1(root: Path, db_path: Path, approval_id: str, *, url: str) -> None:
    """Rewrite a navigate approval's url in both stores, as 1.11.1 stored one its checks accepted."""
    path = root / f"{approval_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["action"]["url"] = url
    path.write_text(json.dumps(payload), encoding="utf-8")
    with closing(sqlite3.connect(db_path)) as conn, conn:
        row = conn.execute("SELECT payload FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        stored = json.loads(row[0])
        stored["action"]["url"] = url
        conn.execute("UPDATE approvals SET payload = ? WHERE id = ?", (json.dumps(stored), approval_id))


class FakePage:
    def __init__(self, url: str = "https://example.com"):
        self.url = url

    async def title(self) -> str:
        return "Example Domain"


class ApprovalQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.settings = Settings(_env_file=None)
        self.settings.artifact_root = str(root / "artifacts")
        self.settings.upload_root = str(root / "uploads")
        self.settings.auth_root = str(root / "auth")
        self.settings.approval_root = str(root / "approvals")
        self.settings.session_store_root = str(root / "sessions")
        self.settings.audit_root = str(root / "audit")
        self.manager = BrowserManager(self.settings)

        artifact_dir = Path(self.settings.artifact_root) / "session-1"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.session = BrowserSession(
            id="session-1",
            name="session-1",
            created_at=datetime.now(UTC),
            context=object(),  # type: ignore[arg-type]
            page=FakePage(),  # type: ignore[arg-type]
            artifact_dir=artifact_dir,
            auth_dir=Path(self.settings.auth_root) / "session-1",
            upload_dir=Path(self.settings.upload_root) / "session-1",
            takeover_url="http://127.0.0.1:6080/vnc.html",
            trace_path=artifact_dir / "trace.zip",
        )
        self.session.auth_dir.mkdir(parents=True, exist_ok=True)
        self.session.upload_dir.mkdir(parents=True, exist_ok=True)
        self.manager.sessions[self.session.id] = self.session

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_upload_requires_pending_approval_then_executes(self) -> None:
        upload_path = Path(self.settings.upload_root) / "demo.txt"
        upload_path.parent.mkdir(parents=True, exist_ok=True)
        upload_path.write_text("demo", encoding="utf-8")

        self.manager._run_action = AsyncMock(return_value={"action": "upload"})  # type: ignore[method-assign]

        with self.assertRaises(ApprovalRequiredError) as ctx:
            await self.manager.upload(
                self.session.id,
                selector='input[type="file"]',
                file_path="demo.txt",
                approved=False,
            )

        approval = ctx.exception.approval
        self.assertEqual(approval.kind, "upload")
        self.assertEqual(approval.status, "pending")

        await self.manager.approve(approval.id, comment="looks good")
        result = await self.manager.upload(
            self.session.id,
            selector='input[type="file"]',
            file_path="demo.txt",
            approved=False,
            approval_id=approval.id,
        )

        self.assertEqual(result["action"], "upload")
        stored = await self.manager.get_approval(approval.id)
        self.assertEqual(stored["status"], "executed")

    async def test_sensitive_decision_creates_queue_item_and_execute_approval_runs_action(self) -> None:
        self.manager.click = AsyncMock(return_value={"action": "click"})  # type: ignore[method-assign]

        decision = BrowserActionDecision(
            action="click",
            reason="This button submits a payment",
            element_id="op-pay",
            risk_category="payment",
        )

        with self.assertRaises(ApprovalRequiredError) as ctx:
            await self.manager.execute_decision(self.session.id, decision)

        approval = ctx.exception.approval
        self.assertEqual(approval.kind, "payment")
        await self.manager.approve(approval.id, comment="approved")

        result = await self.manager.execute_approval(approval.id)

        self.assertEqual(result["approval"]["status"], "executed")
        self.manager.click.assert_awaited_once_with(
            self.session.id,
            selector=None,
            element_id="op-pay",
            x=None,
            y=None,
        )

    async def test_one_approval_runs_its_action_once_under_concurrent_executes(self) -> None:
        # A client retrying an execute that timed out while the first was still
        # running used to pass the approval check twice and pay twice.
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_click(*_args, **_kwargs):
            started.set()
            await release.wait()
            return {"action": "click"}

        self.manager.click = AsyncMock(side_effect=slow_click)  # type: ignore[method-assign]
        decision = BrowserActionDecision(
            action="click",
            reason="This button submits a payment",
            element_id="op-pay",
            risk_category="payment",
        )
        with self.assertRaises(ApprovalRequiredError) as ctx:
            await self.manager.execute_decision(self.session.id, decision)
        approval_id = ctx.exception.approval.id
        await self.manager.approve(approval_id, comment="approved")

        first = asyncio.create_task(self.manager.execute_decision(self.session.id, decision, approval_id=approval_id))
        await started.wait()
        try:
            with self.assertRaises(PermissionError):
                await asyncio.wait_for(
                    self.manager.execute_decision(self.session.id, decision, approval_id=approval_id),
                    timeout=5,
                )
        finally:
            release.set()
        await first

        self.manager.click.assert_awaited_once()
        with self.assertRaises(PermissionError):
            await self.manager.execute_decision(self.session.id, decision, approval_id=approval_id)

    async def test_an_action_refused_before_it_starts_gives_its_approval_back_for_a_retry(self) -> None:
        # The pipeline refuses before the click's operation (the page is on a
        # host ALLOWED_HOSTS does not allow): nothing ran, so the approval is
        # given back. A click that raised once it had started stays spent.
        decision = BrowserActionDecision(
            action="click",
            reason="This button submits a payment",
            selector="#pay",
            risk_category="payment",
        )
        with self.assertRaises(ApprovalRequiredError) as ctx:
            await self.manager.execute_decision(self.session.id, decision)
        approval_id = ctx.exception.approval.id
        await self.manager.approve(approval_id, comment="approved")
        self.session.page.url = "https://elsewhere.test/"

        with self.assertRaises(BrowserActionError) as refused:
            await self.manager.execute_decision(self.session.id, decision, approval_id=approval_id)
        self.assertEqual(refused.exception.code, "browser_action_blocked")
        self.assertEqual((await self.manager.get_approval(approval_id))["status"], "approved")

        self.session.page.url = "https://example.com"
        self.manager.click = AsyncMock(return_value={"action": "click"})  # type: ignore[method-assign]
        await self.manager.execute_decision(self.session.id, decision, approval_id=approval_id)

        stored = await self.manager.get_approval(approval_id)
        self.assertEqual(stored["status"], "executed")
        self.manager.click.assert_awaited_once()

    async def test_governed_write_decision_requires_write_approval(self) -> None:
        decision = BrowserActionDecision(
            action="click",
            reason="Click the save button",
            element_id="op-save",
            risk_category="write",
        )

        with self.assertRaises(ApprovalRequiredError) as ctx:
            await self.manager.require_governed_approval(
                self.session.id,
                decision,
                approval_id=None,
            )

        approval = ctx.exception.approval
        self.assertEqual(approval.kind, "write")
        self.assertEqual(approval.action.action, "click")
        await self.manager.approve(approval.id, comment="operator approved")

        approved = await self.manager.require_governed_approval(
            self.session.id,
            decision,
            approval_id=approval.id,
        )

        self.assertEqual(approved.id, approval.id)


class ApprovalStoreSQLiteTests(unittest.IsolatedAsyncioTestCase):
    async def test_sqlite_store_persists_and_filters_records(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "approvals"
            db_path = Path(tempdir) / "db" / "operator.db"
            store = ApprovalStore(root, db_path=str(db_path))
            await store.startup()

            action = BrowserActionDecision(
                action="click",
                reason="Submit payment",
                element_id="op-pay",
                risk_category="payment",
            )
            approval = await store.create_or_reuse_pending(
                session_id="session-1",
                kind="payment",
                reason="Payment requires approval",
                action=action,
                observation={"url": "https://example.com"},
            )
            await store.approve(approval.id, comment="approved")

            loaded = await store.get(approval.id)
            self.assertEqual(loaded.status, "approved")

            approved = await store.list(status="approved")
            self.assertEqual(len(approved), 1)
            self.assertEqual(approved[0].id, approval.id)
            self.assertTrue(db_path.exists())

    async def test_an_approval_spent_while_sqlite_was_down_stays_spent_once_it_is_back(self) -> None:
        """B-LC-1: with STATE_DB_PATH set, a start whose SQLite failed ran on the file store alone, and an approval
        spent then still read approved in SQLite at the next start, so the same approval ran its action twice."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "approvals"
            db_path = Path(tempdir) / "state.db"
            action = BrowserActionDecision(action="click", reason="pay", selector="#pay", risk_category="payment")
            first = ApprovalStore(root, db_path=str(db_path))
            await first.startup()
            approval = await first.create_or_reuse_pending(
                session_id="abc123abc123", kind="payment", reason="r", action=action
            )
            await first.approve(approval.id)

            down = ApprovalStore(root, db_path=str(db_path))
            down.sqlite_store.startup = AsyncMock(side_effect=RuntimeError("database is locked"))
            await down.startup()
            hold = await down.claim_execution(approval.id)
            self.assertIsNotNone(hold)
            down.release_execution(hold, spent=True)

            back = ApprovalStore(root, db_path=str(db_path))
            await back.startup()

            self.assertIs(back._primary, back.sqlite_store)
            self.assertEqual((await back.get(approval.id)).status, "executed")
            with self.assertRaisesRegex(PermissionError, "was spent when its action started"):
                await back.claim_execution(approval.id)

    async def spent_in_both(self, root: Path, db_path: Path):
        """A store on SQLite, and an approval approved (that record) and then spent, in SQLite and the file."""
        store = ApprovalStore(root, db_path=str(db_path))
        await store.startup()
        action = BrowserActionDecision(action="click", reason="pay", selector="#pay", risk_category="payment")
        pending = await store.create_or_reuse_pending(
            session_id="abc123abc123", kind="payment", reason="r", action=action
        )
        approved = await store.approve(pending.id)
        store.release_execution(await store.claim_execution(pending.id), spent=True)
        return store, approved

    async def test_a_cancelled_approves_late_file_write_does_not_land_after_the_lock_is_released(self) -> None:
        """Item 24 (Codex C 1): an approve cancelled during its file write (a worker thread a cancellation does
        not stop) released the lock while the write went on. Another call approved and spent the approval, and
        the late write then put approved back in the file: the next start read it, and the approval ran twice."""
        with tempfile.TemporaryDirectory() as tempdir:
            root, db_path = Path(tempdir) / "approvals", Path(tempdir) / "state.db"
            store = ApprovalStore(root, db_path=str(db_path))
            await store.startup()
            action = BrowserActionDecision(action="click", reason="pay", selector="#pay", risk_category="payment")
            approval = await store.create_or_reuse_pending(
                session_id="abc123abc123", kind="payment", reason="r", action=action
            )
            real_upsert, gate, writing = store.file_store.upsert, asyncio.Event(), asyncio.Event()

            async def first_approve_waits(record) -> None:
                if record.status != "approved" or writing.is_set():
                    return await real_upsert(record)
                writing.set()
                snapshot = record.model_copy(deep=True)

                async def write() -> None:
                    await gate.wait()
                    await real_upsert(snapshot)

                # As asyncio.to_thread's worker: the write goes on whatever happens to its caller.
                await asyncio.shield(asyncio.ensure_future(write()))

            store.file_store.upsert = first_approve_waits
            cancelled_approve = asyncio.create_task(store.approve(approval.id))
            await writing.wait()
            cancelled_approve.cancel()

            async def approve_and_spend() -> None:
                await store.approve(approval.id)
                store.release_execution(await store.claim_execution(approval.id), spent=True)

            other = asyncio.create_task(approve_and_spend())
            # Its reads run in worker threads: time for it to finish, if the lock lets it.
            await asyncio.wait({other}, timeout=OTHER_CALL_SECONDS)
            gate.set()
            await other
            with self.assertRaises(asyncio.CancelledError):
                await cancelled_approve
            await asyncio.sleep(0.05)  # any write still under way lands

            self.assertEqual((await store.file_store.get(approval.id)).status, "executed")
            for sqlite_up in (True, False):
                with self.subTest(sqlite_up=sqlite_up):
                    again = ApprovalStore(root, db_path=str(db_path))
                    if not sqlite_up:
                        again.sqlite_store.startup = AsyncMock(side_effect=RuntimeError("database is locked"))
                    await again.startup()
                    with self.assertRaisesRegex(PermissionError, "was spent when its action started"):
                        await again.claim_execution(approval.id)

    async def test_a_file_record_older_than_sqlites_spent_one_does_not_replace_it(self) -> None:
        """Item 24: the catch-up copied any file record that differed over SQLite's, so a stale write (approved,
        from before the spend) revived an approval SQLite had spent."""
        with tempfile.TemporaryDirectory() as tempdir:
            root, db_path = Path(tempdir) / "approvals", Path(tempdir) / "state.db"
            _, approved = await self.spent_in_both(root, db_path)
            await ApprovalStore(root).file_store.upsert(approved)  # the stale write: approved, updated before

            back = ApprovalStore(root, db_path=str(db_path))
            await back.startup()

            self.assertEqual((await back.get(approved.id)).status, "executed")
            with self.assertRaisesRegex(PermissionError, "was spent when its action started"):
                await back.claim_execution(approved.id)

    async def stale_spend_and_a_record_sqlite_missed(self, root: Path, db_path: Path):
        """An approval spent in both stores whose file record is stale (approved), and another in the file store
        alone, created later, so a listing (newest first) reaches it first: (the stale record, the missed one)."""
        _, approved = await self.spent_in_both(root, db_path)
        await ApprovalStore(root).file_store.upsert(approved)  # the stale write
        file_only = ApprovalStore(root)
        await file_only.startup()
        action = BrowserActionDecision(action="click", reason="other", selector="#other", risk_category="payment")
        missed = await file_only.create_or_reuse_pending(
            session_id="abc123abc123", kind="payment", reason="r2", action=action
        )
        missed = missed.model_copy(update={"created_at": ApprovalStore._later_than(approved.created_at)})
        await file_only.file_store.upsert(missed)
        return approved, missed

    async def test_a_failed_repair_keeps_sqlite_primary_when_sqlite_takes_the_rest(self) -> None:
        """Item 53 (Codex F, Fable N1): the repair write was unguarded, so its failure aborted the catch-up and the
        start fell back to the file store, whose stale record let the spent approval be claimed again."""
        with tempfile.TemporaryDirectory() as tempdir:
            root, db_path = Path(tempdir) / "approvals", Path(tempdir) / "state.db"
            approved, _ = await self.stale_spend_and_a_record_sqlite_missed(root, db_path)

            back = ApprovalStore(root, db_path=str(db_path))
            back.file_store.upsert = AsyncMock(side_effect=OSError("file locked"))
            with self.assertLogs("app.approvals", "ERROR") as logged:
                await back.startup()

            self.assertIs(back._primary, back.sqlite_store)
            self.assertIn(approved.id, " | ".join(logged.output))
            with self.assertRaisesRegex(PermissionError, "was spent when its action started"):
                await back.claim_execution(approved.id)

    async def test_a_failed_repair_and_a_failed_sqlite_copy_refuse_to_start(self) -> None:
        """Item 53: neither store can be trusted then: the file holds the stale record, and SQLite failed."""
        with tempfile.TemporaryDirectory() as tempdir:
            root, db_path = Path(tempdir) / "approvals", Path(tempdir) / "state.db"
            approved, _ = await self.stale_spend_and_a_record_sqlite_missed(root, db_path)

            back = ApprovalStore(root, db_path=str(db_path))
            back.file_store.upsert = AsyncMock(side_effect=OSError("file locked"))
            back.sqlite_store.upsert = AsyncMock(side_effect=OSError("disk I/O error"))
            with self.assertRaisesRegex(RuntimeError, f"cannot start.*{approved.id}"):
                await back.startup()

    async def test_a_failed_sqlite_copy_falls_back_to_the_file_store_repaired_first(self) -> None:
        """Item 53: a SQLite upsert that failed before the loop reached a stale pair fell back to the file store
        with the stale record still in it."""
        with tempfile.TemporaryDirectory() as tempdir:
            root, db_path = Path(tempdir) / "approvals", Path(tempdir) / "state.db"
            approved, _ = await self.stale_spend_and_a_record_sqlite_missed(root, db_path)

            back = ApprovalStore(root, db_path=str(db_path))
            back.sqlite_store.upsert = AsyncMock(side_effect=OSError("disk I/O error"))
            await back.startup()

            self.assertIs(back._primary, back.file_store)
            with self.assertRaisesRegex(PermissionError, "was spent when its action started"):
                await back.claim_execution(approved.id)

    async def test_keeping_sqlites_spent_record_repairs_the_file_for_a_start_without_sqlite(self) -> None:
        """Item 37 (Codex E 1): the catch-up kept SQLite's spent record over the stale file one, but left the file
        approved, so a later start whose SQLite failed ran on the file store and could claim the approval again."""
        with tempfile.TemporaryDirectory() as tempdir:
            root, db_path = Path(tempdir) / "approvals", Path(tempdir) / "state.db"
            _, approved = await self.spent_in_both(root, db_path)
            await ApprovalStore(root).file_store.upsert(approved)  # the stale write

            back = ApprovalStore(root, db_path=str(db_path))
            await back.startup()
            self.assertEqual((await back.file_store.get(approved.id)).status, "executed")

            down = ApprovalStore(root, db_path=str(db_path))
            down.sqlite_store.startup = AsyncMock(side_effect=RuntimeError("database is locked"))
            await down.startup()
            self.assertIs(down._primary, down.file_store)
            with self.assertRaisesRegex(PermissionError, "was spent when its action started"):
                await down.claim_execution(approved.id)

    async def test_a_give_back_is_stamped_later_than_the_claim_in_the_claims_own_microsecond(self) -> None:
        """Item 38 (Codex E 2): utc_now() gave the claim and its give-back the same microsecond. Since item 56 the
        catch-up keeps SQLite's spend over any file record, so only the stamp is pinned here."""
        with tempfile.TemporaryDirectory() as tempdir:
            root, db_path = Path(tempdir) / "approvals", Path(tempdir) / "state.db"
            # One microsecond for every stamp, as two calls returned on this host: now, so it has not expired.
            with mock.patch("app.approvals.utc_now", return_value=utc_now()):
                store = ApprovalStore(root, db_path=str(db_path))
                await store.startup()
                action = BrowserActionDecision(action="click", reason="pay", selector="#pay", risk_category="payment")
                pending = await store.create_or_reuse_pending(
                    session_id="abc123abc123", kind="payment", reason="r", action=action
                )
                await store.approve(pending.id)
                hold = await store.claim_execution(pending.id)
                hold.gives_back_on_its_own = True
                store.sqlite_store.upsert = AsyncMock(side_effect=ProcessKilled())
                with self.assertRaises(ProcessKilled):
                    await store.give_back(hold)

            given_back = await store.file_store.get(pending.id)
            spent = await ApprovalStore(root, db_path=str(db_path)).sqlite_store.get(pending.id)
            self.assertGreater(
                ApprovalStore._parse_timestamp(given_back.updated_at), ApprovalStore._parse_timestamp(spent.updated_at)
            )

    async def test_a_file_record_older_than_sqlites_rejection_does_not_undo_it(self) -> None:
        """Item 44 (Fable E1-F3): the catch-up's exception covered only executed, so an older file record
        (approved, from before the operator's reject) undid SQLite's rejected one, and the claim was granted."""
        with tempfile.TemporaryDirectory() as tempdir:
            root, db_path = Path(tempdir) / "approvals", Path(tempdir) / "state.db"
            store = ApprovalStore(root, db_path=str(db_path))
            await store.startup()
            action = BrowserActionDecision(action="click", reason="pay", selector="#pay", risk_category="payment")
            pending = await store.create_or_reuse_pending(
                session_id="abc123abc123", kind="payment", reason="r", action=action
            )
            approved = await store.approve(pending.id)
            await store.reject(pending.id, comment="no")
            await store.file_store.upsert(approved)  # the stale write: approved, from before the reject

            back = ApprovalStore(root, db_path=str(db_path))
            await back.startup()

            self.assertEqual((await back.get(pending.id)).status, "rejected")
            self.assertEqual((await back.file_store.get(pending.id)).status, "rejected")
            with self.assertRaises(PermissionError):
                await back.claim_execution(pending.id)

    async def test_an_approve_in_the_rejects_own_microsecond_during_a_sqlite_down_run_is_caught_up(self) -> None:
        """Item 57 (Codex F 3): decisions were stamped with utc_now(), which can return the reject's microsecond,
        and the catch-up keeps SQLite's rejection over a file record not later than it: an operator's approve
        made while SQLite was unavailable was lost at the next start."""
        with tempfile.TemporaryDirectory() as tempdir:
            root, db_path = Path(tempdir) / "approvals", Path(tempdir) / "state.db"
            # One microsecond for every stamp, as two calls returned on this host: now, so it has not expired.
            with mock.patch("app.approvals.utc_now", return_value=utc_now()):
                store = ApprovalStore(root, db_path=str(db_path))
                await store.startup()
                action = BrowserActionDecision(action="click", reason="pay", selector="#pay", risk_category="payment")
                pending = await store.create_or_reuse_pending(
                    session_id="abc123abc123", kind="payment", reason="r", action=action
                )
                await store.reject(pending.id, comment="no")
                down = ApprovalStore(root, db_path=str(db_path))
                down.sqlite_store.startup = AsyncMock(side_effect=RuntimeError("database is locked"))
                await down.startup()
                await down.approve(pending.id, comment="yes after all")

            back = ApprovalStore(root, db_path=str(db_path))
            await back.startup()

            self.assertEqual((await back.get(pending.id)).status, "approved")

    async def test_control_an_approve_later_than_sqlites_rejection_is_caught_up(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root, db_path = Path(tempdir) / "approvals", Path(tempdir) / "state.db"
            store = ApprovalStore(root, db_path=str(db_path))
            await store.startup()
            action = BrowserActionDecision(action="click", reason="pay", selector="#pay", risk_category="payment")
            pending = await store.create_or_reuse_pending(
                session_id="abc123abc123", kind="payment", reason="r", action=action
            )
            rejected = await store.reject(pending.id, comment="no")
            later = ApprovalStore._later_than(rejected.updated_at)
            approved = rejected.model_copy(update={"status": "approved", "updated_at": later, "decided_at": later})
            approved.approved_expires_at = store._expiry_timestamp(later)
            await store.file_store.upsert(approved)  # the operator's approve while SQLite was not primary

            back = ApprovalStore(root, db_path=str(db_path))
            await back.startup()

            self.assertEqual((await back.get(pending.id)).status, "approved")
            self.assertIsNotNone(await back.claim_execution(pending.id))

    async def test_a_give_back_whose_sqlite_write_fails_leaves_both_stores_spent(self) -> None:
        """Item 45 (Fable E1-F4): the give-back's file write landed and its SQLite write failed. The caller was
        told it failed, with no note, and this process read executed, while the file read approved and later:
        the next start caught that up, and the old approval could run beside the new one the agent asked for."""
        with tempfile.TemporaryDirectory() as tempdir:
            root, db_path = Path(tempdir) / "approvals", Path(tempdir) / "state.db"
            store = ApprovalStore(root, db_path=str(db_path))
            await store.startup()
            action = BrowserActionDecision(action="click", reason="pay", selector="#pay", risk_category="payment")
            pending = await store.create_or_reuse_pending(
                session_id="abc123abc123", kind="payment", reason="r", action=action
            )
            await store.approve(pending.id)
            real_upsert = store.sqlite_store.upsert

            async def locked_for_the_give_back(record) -> None:
                if record.status == "approved" and (await store.sqlite_store.get(record.id)).status == "executed":
                    raise RuntimeError("database is locked")
                await real_upsert(record)

            with self.assertRaisesRegex(RuntimeError, "database is locked"):
                async with held_for_execution(store, pending.id, may_give_back=True):
                    store.sqlite_store.upsert = locked_for_the_give_back
                    refusal = PermissionError("Action blocked by policy (before the effect)")
                    refused_before_effect(refusal)
                    raise refusal
            store.sqlite_store.upsert = real_upsert

            self.assertEqual(
                ((await store.get(pending.id)).status, (await store.file_store.get(pending.id)).status),
                ("executed", "executed"),
            )
            back = ApprovalStore(root, db_path=str(db_path))
            await back.startup()
            with self.assertRaisesRegex(PermissionError, "was spent when its action started"):
                await back.claim_execution(pending.id)

    async def test_a_file_record_later_than_sqlites_spent_one_does_not_revive_it(self) -> None:
        """Item 56 (Codex F 2, Fable N4): the catch-up trusted a file record later than SQLite's executed one as a
        give-back. One whose SQLite half failed, and whose file put-back failed too, was told it failed, and the
        next start revived it beside the new approval the agent asked for. A spend is final at a start."""
        with tempfile.TemporaryDirectory() as tempdir:
            root, db_path = Path(tempdir) / "approvals", Path(tempdir) / "state.db"
            store, approved = await self.spent_in_both(root, db_path)
            spent = await store.sqlite_store.get(approved.id)
            later = (datetime.fromisoformat(spent.updated_at.replace("Z", "+00:00")) + timedelta(seconds=1)).isoformat()
            given_back = spent.model_copy(
                update={"status": "approved", "executed_at": None, "updated_at": later.replace("+00:00", "Z")}
            )
            await store.file_store.upsert(given_back)  # a give-back SQLite never took

            back = ApprovalStore(root, db_path=str(db_path))
            await back.startup()

            self.assertEqual((await back.get(spent.id)).status, "executed")
            self.assertEqual((await back.file_store.get(spent.id)).status, "executed")
            with self.assertRaisesRegex(PermissionError, "was spent when its action started"):
                await back.claim_execution(spent.id)

    async def test_a_stored_record_that_no_longer_validates_is_left_out_and_sqlite_stays_primary(self) -> None:
        """Item 29 (Opus C): a navigate approval 1.11.1 stored to http://127.1:3000/, which BrowserActionDecision
        now refuses, failed SQLite's listing, so the catch-up raised at every start and the file store stayed
        primary: the split item 12 closes, made permanent. It is left out, with a warning naming it, and
        answers as an unknown id; a valid record beside it is unaffected."""
        with tempfile.TemporaryDirectory() as tempdir:
            root, db_path = Path(tempdir) / "approvals", Path(tempdir) / "state.db"
            store = ApprovalStore(root, db_path=str(db_path))
            await store.startup()
            navigate = BrowserActionDecision(
                action="navigate", reason="go", url="http://127.0.0.1:3000/", risk_category="write"
            )
            stale = await store.create_or_reuse_pending(
                session_id="abc123abc123", kind="write", reason="r", action=navigate
            )
            click = BrowserActionDecision(action="click", reason="pay", selector="#pay", risk_category="payment")
            valid = await store.approve(
                (
                    await store.create_or_reuse_pending(
                        session_id="abc123abc123", kind="payment", reason="r", action=click
                    )
                ).id
            )
            stored_by_1_11_1(root, db_path, stale.id, url="http://127.1:3000/")

            back = ApprovalStore(root, db_path=str(db_path))
            with self.assertLogs("app.approval_backends", "WARNING") as logged:
                await back.startup()
                listed = await back.list()

            self.assertIs(back._primary, back.sqlite_store)
            self.assertEqual([record.id for record in listed], [valid.id])
            self.assertTrue(any(stale.id in line for line in logged.output), logged.output)
            for store_or_backend in (back, back.sqlite_store, back.file_store):
                with self.subTest(store=type(store_or_backend).__name__), self.assertRaises(KeyError):
                    await store_or_backend.get(stale.id)
            with self.assertRaises(KeyError):
                await back.claim_execution(stale.id)
            self.assertIsNotNone(await back.claim_execution(valid.id))

    async def test_the_warning_for_a_record_that_no_longer_validates_names_it_without_its_contents(self) -> None:
        """Item 39 (Codex E 3): the warning logged the whole ValidationError, whose input preview carried the
        stored navigate URL, a token in its query among it."""
        with tempfile.TemporaryDirectory() as tempdir:
            root, db_path = Path(tempdir) / "approvals", Path(tempdir) / "state.db"
            store = ApprovalStore(root, db_path=str(db_path))
            await store.startup()
            navigate = BrowserActionDecision(
                action="navigate", reason="go", url="http://127.0.0.1:3000/", risk_category="write"
            )
            stale = await store.create_or_reuse_pending(
                session_id="abc123abc123", kind="write", reason="r", action=navigate
            )
            stored_by_1_11_1(root, db_path, stale.id, url=f"http://127.1/?token={LOGGED_SENTINEL}")

            back = ApprovalStore(root, db_path=str(db_path))
            with self.assertLogs("app.approval_backends", "WARNING") as logged:
                await back.startup()
                with self.assertRaises(KeyError):
                    await back.file_store.get(stale.id)

            text = " | ".join(logged.output)
            self.assertIn(stale.id, text)
            self.assertIn("action", text)  # where it failed
            self.assertNotIn(LOGGED_SENTINEL, text)
            self.assertNotIn("127.1", text)

    async def test_the_file_listing_warns_of_a_record_that_no_longer_validates_without_its_contents(self) -> None:
        """Item 54 (Codex F, Fable N2): the file store's listing logged the whole ValidationError at DEBUG, whose
        input preview quoted the record (a navigate URL's query token), and gave no warning naming it."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "approvals"
            store = ApprovalStore(root)
            await store.startup()
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            stale_id = "aaaaaaaaaaaa"
            record = {
                "id": stale_id, "session_id": "abc123abc123", "kind": "write", "status": "pending",
                "created_at": now, "updated_at": now, "reason": "go",
                "action": {"action": "navigate", "reason": "go", "url": f"http://127.1/?token={LOGGED_SENTINEL}"},
            }  # fmt: skip
            (root / f"{stale_id}.json").write_text(json.dumps(record), encoding="utf-8")

            with self.assertLogs("app.approval_backends", "DEBUG") as logged:
                listed = await store.file_store.list()

            self.assertEqual(listed, [])
            self.assertNotIn(LOGGED_SENTINEL, " | ".join(logged.output))
            warnings = [line for line in logged.output if line.startswith("WARNING") and stale_id in line]
            self.assertTrue(warnings, logged.output)

    async def test_control_a_record_sqlite_already_has_is_left_as_it_is(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "approvals"
            db_path = Path(tempdir) / "state.db"
            action = BrowserActionDecision(action="click", reason="pay", selector="#pay", risk_category="payment")
            first = ApprovalStore(root, db_path=str(db_path))
            await first.startup()
            approval = await first.create_or_reuse_pending(
                session_id="abc123abc123", kind="payment", reason="r", action=action
            )
            await first.approve(approval.id)

            again = ApprovalStore(root, db_path=str(db_path))
            upserted: list[str] = []
            real_upsert = again.sqlite_store.upsert

            async def counted(record) -> None:
                upserted.append(record.id)
                await real_upsert(record)

            again.sqlite_store.upsert = counted
            await again.startup()

            # Item 33: a catch-up that rewrote every record passed this control as well.
            self.assertEqual(upserted, [])
            self.assertEqual((await again.get(approval.id)).status, "approved")
            self.assertIsNotNone(await again.claim_execution(approval.id))

    async def test_expired_approval_is_rejected_for_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "approvals"
            store = ApprovalStore(root)
            await store.startup()

            action = BrowserActionDecision(
                action="click",
                reason="Submit payment",
                element_id="op-pay",
                risk_category="payment",
            )
            approval = await store.create_or_reuse_pending(
                session_id="session-1",
                kind="payment",
                reason="Payment requires approval",
                action=action,
                observation={"url": "https://example.com"},
            )
            approval = await store.approve(approval.id, comment="approved")
            approval.approved_expires_at = (datetime.now(UTC) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
            await store.file_store.upsert(approval)

            with self.assertRaises(PermissionError):
                await store.require_approved(
                    approval_id=approval.id,
                    session_id="session-1",
                    kind="payment",
                    action=action,
                )

            with self.assertRaises(PermissionError):
                await store.mark_executed(approval.id)


class GovernedToolCallBindingTests(unittest.IsolatedAsyncioTestCase):
    """An approval for one governed tool call must not authorise a different one.

    The gateway's stand-in decision for tool calls carried only the tool name,
    and approval matching ignores the reason, so an approval granted for one
    browser.eval_js expression was accepted for any other.
    """

    async def test_an_approved_expression_does_not_authorise_another(self) -> None:
        from app.tool_gateway.gateway import McpToolGateway
        from app.tool_gateway.registry import ToolSpec
        from app.tool_inputs import EvalJsInput

        spec = ToolSpec(
            name="browser.eval_js",
            description="",
            input_model=EvalJsInput,
            handler=AsyncMock(),
            pack="scripting",
            governed_kind="write",
        )
        approved, _ = McpToolGateway._governed_call_decision(
            spec, EvalJsInput(session_id="session-1", expression="() => document.title")
        )
        other, _ = McpToolGateway._governed_call_decision(
            spec, EvalJsInput(session_id="session-1", expression="() => document.cookie")
        )
        same, _ = McpToolGateway._governed_call_decision(
            spec, EvalJsInput(session_id="session-1", expression="() => document.title", approval_id="x")
        )

        with tempfile.TemporaryDirectory() as tempdir:
            store = ApprovalStore(Path(tempdir))
            await store.startup()
            approval = await store.create_or_reuse_pending(
                session_id="session-1", kind="write", reason=approved.reason, action=approved
            )
            await store.approve(approval.id)

            with self.assertRaises(PermissionError):
                await store.require_approved(
                    approval_id=approval.id, session_id="session-1", kind="write", action=other
                )
            # The same call (the approval_id itself is not part of its identity) is accepted.
            await store.require_approved(approval_id=approval.id, session_id="session-1", kind="write", action=same)
