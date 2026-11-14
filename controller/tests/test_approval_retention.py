"""Decided approvals are deleted once APPROVAL_RETENTION_DAYS have passed, and nothing that can still act is."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from app import browser_manager
from app.app_factory import build_controller_services
from app.approval_backends import DIGEST_KEY_FILE, FileApprovalStore
from app.approvals import CONTROLLER_SCOPE, ApprovalStore
from app.audit import get_current_operator
from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.maintenance import MaintenanceService
from app.models import BrowserActionDecision
from app.utils import UTC

SESSION = "abc123abc123"


def _ago(**age: float) -> str:
    return (datetime.now(UTC) - timedelta(**age)).isoformat().replace("+00:00", "Z")


def _tool_call(arguments: str) -> BrowserActionDecision:
    """A governed tool call's stand-in as the gateway asks an approval for it: the tool, and its arguments' digest."""
    digest = hashlib.sha256(arguments.encode()).hexdigest()
    return BrowserActionDecision(
        action="request_human_takeover",
        reason="r",
        risk_category="account_change",
        text=f"browser.set_cookies sha256:{digest}",
    )


def _settings(root: Path, **values: str) -> Settings:
    settings = Settings(_env_file=None, **values)
    for name in ("artifact", "upload", "auth", "approval", "audit"):
        setattr(settings, f"{name}_root", str(root / name))
    settings.session_store_root = str(root / "sessions")
    return settings


async def _age(store: ApprovalStore, approval_id: str, *, days: float) -> None:
    """Rewrite a decided record as one decided `days` ago, whose approval (if it is one) lapsed with its TTL."""
    record = await store.get(approval_id)
    record.decided_at = _ago(days=days)
    if record.approved_expires_at is not None:
        record.approved_expires_at = store._expiry_timestamp(record.decided_at)
    await store._persist(record)


class _Audit:
    """An audit store that keeps what it is given, and who was current when it was."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    async def append(self, **event) -> None:
        self.events.append({**event, "operator": get_current_operator()})


class RetentionBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "approvals"
        self.db_path = Path(self.tmp.name) / "state.db"
        self.audit = _Audit()
        self.store = await self._store()
        self._seeded = 0

    async def _store(self, *, retention_days: float = 30, sqlite: bool = True) -> ApprovalStore:
        store = ApprovalStore(
            self.root, db_path=str(self.db_path) if sqlite else None, retention_days=retention_days, audit=self.audit
        )
        await store.startup()
        return store

    async def _pending(self, *, session_id: str = SESSION, selector: str | None = None) -> str:
        self._seeded += 1
        action = BrowserActionDecision(
            action="click", reason="pay", selector=selector or f"#pay-{self._seeded}", risk_category="payment"
        )
        approval = await self.store.create_or_reuse_pending(
            session_id=session_id, kind="payment", reason="r", action=action
        )
        return approval.id

    async def _decided(
        self,
        status: str,
        *,
        days: float,
        session_id: str = SESSION,
        decided_via: str = "operator",
        selector: str | None = None,
        expires: str | None = "lapsed",
    ) -> str:
        """An approval decided `days` ago, in both stores: `approved` (its expiry lapsed, in the `future`, or
        None for one stored with no expiry), `rejected`, or `executed`."""
        approval_id = await self._pending(session_id=session_id, selector=selector)
        if status == "rejected":
            await self.store.reject(approval_id, decided_via=decided_via)  # type: ignore[arg-type]
        else:
            await self.store.approve(approval_id, decided_via=decided_via)  # type: ignore[arg-type]
            if status == "executed":
                self.store.release_execution(await self.store.claim_execution(approval_id), spent=True)
        record = await self.store.get(approval_id)
        record.decided_at = _ago(days=days)
        if record.status != "rejected":
            lapsed = self.store._expiry_timestamp(record.decided_at)
            record.approved_expires_at = {"lapsed": lapsed, "future": _ago(days=-1), None: None}[expires]
        await self.store._persist(record)
        return approval_id

    async def _ids(self) -> set[str]:
        return {record.id for record in await self.store.list()}

    def _in_sqlite(self) -> set[str]:
        with closing(sqlite3.connect(self.db_path)) as conn:
            return {row[0] for row in conn.execute("SELECT id FROM approvals")}

    def _on_disk(self) -> set[str]:
        return {path.stem for path in self.root.glob("*.json")}


class RetentionTests(RetentionBase):
    async def test_a_pass_removes_decided_records_past_the_retention_from_both_stores(self) -> None:
        approved = await self._decided("approved", days=31)
        rejected = await self._decided("rejected", days=31)
        executed = await self._decided("executed", days=31)
        recent = [await self._decided(status, days=29) for status in ("approved", "rejected", "executed")]
        pending = await self._pending()
        old_pending = await self.store.get(pending)
        old_pending.created_at = old_pending.updated_at = _ago(days=400)
        await self.store._persist(old_pending)

        # A pending approval is not one whose decision time cannot be read: it has none yet.
        with self.assertNoLogs("app.approvals", level="WARNING"):
            removed = await self.store.prune_decided()

        self.assertEqual(removed, 3)
        kept = {*recent, pending}
        self.assertEqual(await self._ids(), kept)
        self.assertEqual(self._in_sqlite(), kept)
        self.assertEqual(self._on_disk(), kept)
        for approval_id in (approved, rejected, executed):
            with self.assertRaises(KeyError):
                await self.store.get(approval_id)

    async def test_a_store_on_files_alone_removes_them_too(self) -> None:
        self.store = await self._store(sqlite=False)
        old = await self._decided("executed", days=31)
        recent = await self._decided("executed", days=1)

        self.assertEqual(await self.store.prune_decided(), 1)

        self.assertEqual(self._on_disk(), {recent})
        self.assertNotIn(old, await self._ids())

    async def test_a_retention_of_zero_removes_nothing_and_audits_nothing(self) -> None:
        self.store = await self._store(retention_days=0)
        old = await self._decided("executed", days=4000)

        self.assertEqual(await self.store.prune_decided(), 0)

        self.assertEqual(await self._ids(), {old})
        self.assertEqual(self.audit.events, [])

    async def test_a_store_built_without_a_retention_keeps_everything(self) -> None:
        # What every caller that builds a bare store gets; the controller passes its setting.
        self.store = ApprovalStore(self.root, db_path=str(self.db_path), audit=self.audit)
        await self.store.startup()
        old = await self._decided("executed", days=4000)

        self.assertEqual(await self.store.prune_decided(), 0)

        self.assertEqual(await self._ids(), {old})

    async def test_a_pass_writes_one_audit_event_with_the_count_and_none_when_nothing_went(self) -> None:
        for _ in range(2):
            await self._decided("executed", days=31)
        self.audit.events.clear()

        await self.store.prune_decided()
        await self.store.prune_decided()

        self.assertEqual(len(self.audit.events), 1, self.audit.events)
        event = self.audit.events[0]
        self.assertEqual(
            (event["event_type"], event["status"], event["action"]), ("approvals_pruned", "ok", "retention")
        )
        self.assertEqual(event["details"], {"count": 2, "retention_days": 30})
        self.assertEqual((event["operator"].id, event["operator"].source), ("system", "system"))

    async def test_a_tool_calls_arguments_go_with_its_record(self) -> None:
        action = BrowserActionDecision(action="request_human_takeover", reason="browser.eval_js sha256:0", text="t")
        approval = await self.store.create_or_reuse_pending(
            session_id=SESSION, kind="write", reason="r", action=action, origin="tool_call", arguments='{"a": 1}'
        )
        await self.store.approve(approval.id)
        await _age(self.store, approval.id, days=31)
        self.assertTrue(await self.store.file_store.has_arguments(approval.id))

        self.assertEqual(await self.store.prune_decided(), 1)

        self.assertFalse(await self.store.file_store.has_arguments(approval.id))
        # Nothing of the approval: the store's own key is all the root holds.
        self.assertEqual([path.name for path in self.root.iterdir()], [DIGEST_KEY_FILE])

    async def test_an_approval_still_good_for_its_action_is_kept(self) -> None:
        # Approved long ago and not expired (an APPROVAL_TTL_MINUTES longer than the retention, or stored with no
        # expiry): it can still run its action, so it is no more history than a pending one.
        unexpired = await self._decided("approved", days=31, expires="future")
        no_expiry = await self._decided("approved", days=31, expires=None)
        expired = await self._decided("approved", days=31)

        self.assertEqual(await self.store.prune_decided(), 1)

        self.assertEqual(await self._ids(), {unexpired, no_expiry})
        self.assertNotIn(expired, self._in_sqlite())
        self.assertIsNotNone(await self.store.claim_execution(unexpired))

    async def test_a_record_an_execution_holds_is_kept_until_the_hold_ends(self) -> None:
        approval_id = await self._decided("approved", days=31, expires=None)
        hold = await self.store.claim_execution(approval_id)
        self.assertEqual((await self.store.get(approval_id)).status, "executed")

        self.assertEqual(await self.store.prune_decided(), 0)
        self.assertEqual(await self._ids(), {approval_id})

        self.store.release_execution(hold, spent=True)
        self.assertEqual(await self.store.prune_decided(), 1)
        self.assertEqual(await self._ids(), set())

    async def test_an_operators_rejection_is_kept_where_its_action_can_still_be_asked_for(self) -> None:
        # It refuses an agent's approval of the same action (AUTONOMOUS_APPROVALS) in controller scope and in its
        # session while that is open: deleted, the agent's next request for the action could approve itself.
        standing = await self._decided("rejected", days=31, session_id=CONTROLLER_SCOPE, selector="#delete")
        open_session = await self._decided("rejected", days=31, session_id="open00000001")
        closed_session = await self._decided("rejected", days=31, session_id="closed000001")
        by_the_agent = await self._decided("rejected", days=31, session_id=CONTROLLER_SCOPE, decided_via="agent")

        removed = await self.store.prune_decided(open_sessions={"open00000001"})

        self.assertEqual(removed, 2)
        self.assertEqual(await self._ids(), {standing, open_session})
        self.assertFalse({closed_session, by_the_agent} & self._in_sqlite())
        asked_again = await self._pending(session_id=CONTROLLER_SCOPE, selector="#delete")
        with self.assertRaisesRegex(PermissionError, "an operator rejected this action"):
            await self.store.approve(asked_again, decided_via="agent")

    async def _rejected_tool_call(self, arguments: str, *, session_id: str = CONTROLLER_SCOPE) -> str:
        """An operator's rejection of a tool call's approval, kept under the key the store holds now."""
        approval = await self.store.create_or_reuse_pending(
            session_id=session_id, kind="account_change", reason="r", action=_tool_call(arguments), origin="tool_call"
        )
        await self.store.reject(approval.id)
        return approval.id

    async def test_an_operators_rejection_kept_under_a_key_that_is_gone_goes_once_past_the_retention(self) -> None:
        # It matches no call any more, so it refuses nothing (_operator_rejected). Kept for good where its action
        # could still be asked for, it was counted in a warning at every start that nothing an operator did ended.
        standing = await self._rejected_tool_call("a")
        open_session = await self._rejected_tool_call("b", session_id="open00000001")
        recent = await self._rejected_tool_call("c")
        (self.root / DIGEST_KEY_FILE).unlink()
        with self.assertLogs("app.approvals", level="WARNING") as logged:
            self.store = await self._store()
        self.assertIn("3 operator rejection(s) of tool calls are kept under another key", "\n".join(logged.output))
        for approval_id, days in ((standing, 31), (open_session, 31), (recent, 29)):
            await _age(self.store, approval_id, days=days)

        removed = await self.store.prune_decided(open_sessions={"open00000001"})

        self.assertEqual(removed, 2)
        self.assertEqual(await self._ids(), {recent})
        self.assertEqual(self._in_sqlite(), {recent})
        self.assertEqual(self._on_disk(), {recent})
        # Inside the retention it is still stored, and still said at a start.
        with self.assertLogs("app.approvals", level="WARNING") as logged:
            self.store = await self._store()
        self.assertIn("1 operator rejection(s) of tool calls are kept under another key", "\n".join(logged.output))
        await _age(self.store, recent, days=31)
        self.assertEqual(await self.store.prune_decided(), 1)
        with self.assertNoLogs("app.approvals", level="WARNING"):
            await self._store()

    async def test_an_operators_rejection_of_a_tool_call_under_the_key_held_is_kept(self) -> None:
        # In controller scope, whatever its age: this one still refuses an agent's approval of the same call.
        other_key = await self._rejected_tool_call("a")
        (self.root / DIGEST_KEY_FILE).unlink()
        with self.assertLogs("app.approvals", level="WARNING"):
            self.store = await self._store()
        standing = await self._rejected_tool_call("a")
        for approval_id in (other_key, standing):
            await _age(self.store, approval_id, days=400)

        self.assertEqual(await self.store.prune_decided(), 1)

        self.assertEqual(await self._ids(), {standing})
        asked_again = await self.store.create_or_reuse_pending(
            session_id=CONTROLLER_SCOPE, kind="account_change", reason="r", action=_tool_call("a"), origin="tool_call"
        )
        with self.assertRaisesRegex(PermissionError, "an operator rejected this action"):
            await self.store.approve(asked_again.id, decided_via="agent")

    async def _a_start_that_cannot_use_the_key_file_keeps_what_its_key_refuses(self, fault: Exception) -> None:
        standing = await self._rejected_tool_call("a")
        open_session = await self._rejected_tool_call("b", session_id="open00000001")
        closed_session = await self._rejected_tool_call("c", session_id="closed000001")
        for approval_id in (standing, open_session, closed_session):
            await _age(self.store, approval_id, days=31)
        with patch.object(FileApprovalStore, "digest_key", side_effect=fault):
            with self.assertLogs("app.approvals", level="WARNING") as logged:
                self.store = await self._store()
            self.assertIn("tool call approvals are keyed by this process alone", "\n".join(logged.output))

            # At the start and at every sweep of that process. One whose session has closed goes, as it always did.
            self.assertEqual(await self.store.prune_decided(open_sessions={"open00000001"}), 1)
            self.assertEqual(await self.store.prune_decided(open_sessions={"open00000001"}), 0)

        self.assertEqual(await self._ids(), {standing, open_session})
        self.assertEqual(self._in_sqlite(), {standing, open_session})
        self.assertEqual(self._on_disk(), {standing, open_session})
        # The next start can read the file: the rejection is under the key it holds, and refuses as before.
        with self.assertNoLogs("app.approvals", level="WARNING"):
            self.store = await self._store()
        self.assertEqual(await self.store.prune_decided(open_sessions={"open00000001"}), 0)
        asked_again = await self.store.create_or_reuse_pending(
            session_id=CONTROLLER_SCOPE, kind="account_change", reason="r", action=_tool_call("a"), origin="tool_call"
        )
        with self.assertRaisesRegex(PermissionError, "an operator rejected this action"):
            await self.store.approve(asked_again.id, decided_via="agent")

    async def test_a_start_that_may_not_read_the_key_file_keeps_an_operators_rejection_made_under_it(self) -> None:
        # Such a start keys with a key of its own, so every stored tool call reads as kept under another key. The
        # file's key is not gone: one transient fault deleted standing rejections the next good start would match.
        await self._a_start_that_cannot_use_the_key_file_keeps_what_its_key_refuses(
            PermissionError(errno.EACCES, "Permission denied")
        )

    async def test_a_start_that_reads_no_key_in_the_file_keeps_an_operators_rejection_made_under_it(self) -> None:
        # A file caught half restored, or saved in another encoding for a while: the same, by the other error.
        await self._a_start_that_cannot_use_the_key_file_keeps_what_its_key_refuses(ValueError("holds no key"))

    async def test_a_pass_that_is_the_first_to_read_the_key_keeps_what_its_own_key_refuses(self) -> None:
        # A start reads the key before its retention asks. A store nothing has started reads it at first use, and
        # the pass can be that use: asked whether it keys alone before it had tried the file, it answered no, and
        # took the file's own key for one that is gone.
        standing = await self._rejected_tool_call("a")
        await _age(self.store, standing, days=31)
        unstarted = ApprovalStore(self.root, retention_days=30, audit=self.audit)

        with (
            patch.object(FileApprovalStore, "digest_key", side_effect=PermissionError(errno.EACCES, "denied")),
            self.assertLogs("app.approvals", level="WARNING") as logged,
        ):
            removed = await unstarted.prune_decided()

        self.assertIn("tool call approvals are keyed by this process alone", "\n".join(logged.output))
        self.assertEqual(removed, 0)
        self.assertEqual(self._on_disk(), {standing})

    async def test_a_rejection_made_by_a_start_that_keyed_alone_goes_once_the_files_key_is_held_again(self) -> None:
        # That start's own key ends with its process, so what it rejected matches no call at any later start.
        with patch.object(FileApprovalStore, "digest_key", side_effect=PermissionError(errno.EACCES, "denied")):
            with self.assertLogs("app.approvals", level="WARNING"):
                self.store = await self._store()
            made_alone = await self._rejected_tool_call("a")
            await _age(self.store, made_alone, days=31)
            # Under the key that process holds: kept, as any operator's rejection that still matches its call.
            self.assertEqual(await self.store.prune_decided(), 0)
        with self.assertLogs("app.approvals", level="WARNING") as logged:
            self.store = await self._store()
        self.assertIn("1 operator rejection(s) of tool calls are kept under another key", "\n".join(logged.output))

        self.assertEqual(await self.store.prune_decided(), 1)

        self.assertEqual(await self._ids(), set())
        with self.assertNoLogs("app.approvals", level="WARNING"):
            await self._store()

    async def _stored_texts(self, approval_id: str) -> set[str | None]:
        """The record's text as each store configured holds it, not as the store hands it out."""
        backends = [self.store.file_store, *([self.store.sqlite_store] if self.store.sqlite_store else [])]
        return {(await backend.get(approval_id)).action.text for backend in backends}

    async def _asked_again_and_refused_the_agents_approval(self, arguments: str) -> None:
        asked_again = await self.store.create_or_reuse_pending(
            session_id=CONTROLLER_SCOPE,
            kind="account_change",
            reason="r",
            action=_tool_call(arguments),
            origin="tool_call",
        )
        with self.assertRaisesRegex(PermissionError, "an operator rejected this action"):
            await self.store.approve(asked_again.id, decided_via="agent")

    async def test_a_start_that_keys_alone_leaves_a_rejection_stored_before_1_18_0_for_a_start_that_holds_the_key(
        self,
    ) -> None:
        # 1.17.0 stored a tool call's digest plain, and a start rewrites it keyed. A start that could not use the key
        # file rewrote it under its own key, which no later start holds: at the next good start the operator's
        # rejection no longer refused the agent's own approval of the call, and the retention removed it.
        for sqlite in (True, False):
            with self.subTest(sqlite=sqlite):
                self.root = Path(self.tmp.name) / f"approvals-{sqlite}"
                self.db_path = Path(self.tmp.name) / f"state-{sqlite}.db"
                self.store = await self._store(sqlite=sqlite)
                rejected = await self._rejected_tool_call("a")
                await _age(self.store, rejected, days=31)
                record = await self.store.get(rejected)
                record.action = _tool_call("a")  # as 1.17.0 stored it
                for backend in (self.store.file_store, self.store.sqlite_store):
                    if backend is not None:
                        await backend.upsert(record)
                plain = _tool_call("a").text
                self.assertEqual(await self._stored_texts(rejected), {plain})

                with patch.object(FileApprovalStore, "digest_key", side_effect=PermissionError(errno.EACCES, "denied")):
                    with self.assertLogs("app.approvals", level="INFO") as logged:
                        self.store = await self._store(sqlite=sqlite)
                    alone = "\n".join(logged.output)
                    self.assertIn("tool call approvals are keyed by this process alone", alone)
                    # Nothing is rewritten, and no line says anything was.
                    self.assertEqual(await self._stored_texts(rejected), {plain})
                    self.assertNotIn("now keep their tool call digest keyed", alone)
                    self.assertIn("1 approval record(s) stored before 1.18.0 still keep a tool call digest", alone)
                    # In that process it refuses as before (both sides are keyed in memory), and is kept.
                    await self._asked_again_and_refused_the_agents_approval("a")
                    self.assertEqual(await self.store.prune_decided(), 0)
                    self.assertEqual(await self._stored_texts(rejected), {plain})

                with self.assertLogs("app.approvals", level="INFO") as logged:
                    self.store = await self._store(sqlite=sqlite)
                good = "\n".join(logged.output)
                self.assertIn("1 approval record(s) stored before 1.18.0 now keep their tool call digest", good)
                self.assertNotIn("operator rejection(s) of tool calls are kept under another key", good)
                # Under the file's key now, in each store: what this start keeps for the same call.
                under_the_files_key = self.store._stored_action(_tool_call("a")).text
                self.assertNotEqual(under_the_files_key, plain)
                self.assertEqual(await self._stored_texts(rejected), {under_the_files_key})
                await self._asked_again_and_refused_the_agents_approval("a")
                self.assertEqual(await self.store.prune_decided(), 0)
                self.assertIn(rejected, await self._ids())

    async def test_a_decision_where_a_start_keys_alone_leaves_a_record_stored_before_1_18_0_for_the_key_file(
        self,
    ) -> None:
        # Such a start rewrites none of them, but hands each out keyed under its own key, and a decision or a claim
        # wrote it back so: an operator's rejection made there, or made again, no longer refused the agent's own
        # approval of the call at the next good start, and its retention removed it.
        calls = {"again": "a", "rejected": "b", "run": "c", "given back": "d"}
        for sqlite in (True, False):
            with self.subTest(sqlite=sqlite):
                self.root = Path(self.tmp.name) / f"approvals-{sqlite}"
                self.db_path = Path(self.tmp.name) / f"state-{sqlite}.db"
                self.store = await self._store(sqlite=sqlite)
                ids = {"again": await self._rejected_tool_call("a")}
                for name in ("rejected", "run", "given back"):
                    asked = await self.store.create_or_reuse_pending(
                        session_id=CONTROLLER_SCOPE,
                        kind="account_change",
                        reason="r",
                        action=_tool_call(calls[name]),
                        origin="tool_call",
                    )
                    ids[name] = asked.id
                for name, approval_id in ids.items():
                    record = await self.store.get(approval_id)
                    record.action = _tool_call(calls[name])  # as 1.17.0 stored it
                    for backend in (self.store.file_store, self.store.sqlite_store):
                        if backend is not None:
                            await backend.upsert(record)

                with patch.object(FileApprovalStore, "digest_key", side_effect=PermissionError(errno.EACCES, "denied")):
                    with self.assertLogs("app.approvals", level="WARNING"):
                        self.store = await self._store(sqlite=sqlite)
                    decided = await self.store.reject(ids["again"], "still no")
                    await self.store.reject(ids["rejected"], "no")
                    await self.store.approve(ids["run"], "yes")
                    self.store.release_execution(await self.store.claim_execution(ids["run"]), spent=True)
                    await self.store.approve(ids["given back"], "yes")
                    hold = await self.store.claim_execution(ids["given back"])
                    hold.gives_back_on_its_own = True  # refused before it changed anything
                    await self.store.give_back(hold)
                    self.store.release_execution(hold, spent=False)
                    for name, approval_id in ids.items():
                        self.assertEqual(await self._stored_texts(approval_id), {_tool_call(calls[name]).text}, name)
                    # Handed out keyed, as before: in that process both rejections refuse.
                    self.assertNotEqual(decided.action.text, _tool_call("a").text)
                    self.assertEqual((await self.store.get(ids["again"])).action.text, decided.action.text)
                    await self._asked_again_and_refused_the_agents_approval("a")
                    await self._asked_again_and_refused_the_agents_approval("b")

                with self.assertLogs("app.approvals", level="INFO") as logged:
                    self.store = await self._store(sqlite=sqlite)
                good = "\n".join(logged.output)
                self.assertIn("4 approval record(s) stored before 1.18.0 now keep their tool call digest", good)
                self.assertNotIn("operator rejection(s) of tool calls are kept under another key", good)
                for name, approval_id in ids.items():
                    under_the_files_key = self.store._stored_action(_tool_call(calls[name])).text
                    self.assertEqual(await self._stored_texts(approval_id), {under_the_files_key}, name)
                self.assertEqual((await self.store.get(ids["run"])).status, "executed")
                # An approval made there runs until it expires, as one made before 1.18.0 does.
                await self.store.require_approved(
                    approval_id=ids["given back"],
                    session_id=CONTROLLER_SCOPE,
                    kind="account_change",
                    action=_tool_call("d"),
                    origin="tool_call",
                )
                await self._asked_again_and_refused_the_agents_approval("a")
                await self._asked_again_and_refused_the_agents_approval("b")
                for name in ("again", "rejected"):
                    await _age(self.store, ids[name], days=31)
                self.assertEqual(await self.store.prune_decided(), 0)

    async def test_a_record_another_start_keys_between_a_decisions_read_and_its_write_keeps_the_files_key(
        self,
    ) -> None:
        # Two processes over one APPROVAL_ROOT: this one keys alone, and a start that can read the key file rewrites a
        # record stored before 1.18.0 under the file's key between this one's read of it and its write. The write
        # compared with what the store held then, keyed, and put this process's own keyed text over the rewrite: no
        # later start holds that key, so the operator's rejection made here no longer refused the agent's own
        # approval of the call after the next good start. A record's action never changes once it is made, so the
        # write takes the text the store holds, whatever it is.
        for sqlite in (True, False):
            with self.subTest(sqlite=sqlite):
                self.root = Path(self.tmp.name) / f"approvals-race-{sqlite}"
                self.db_path = Path(self.tmp.name) / f"state-race-{sqlite}.db"
                self.store = await self._store(sqlite=sqlite)
                rejected = await self._rejected_tool_call("a")
                record = await self.store.get(rejected)
                record.action = _tool_call("a")  # as 1.17.0 stored it
                for backend in (self.store.file_store, self.store.sqlite_store):
                    if backend is not None:
                        await backend.upsert(record)
                with patch.object(FileApprovalStore, "digest_key", side_effect=PermissionError(errno.EACCES, "denied")):
                    with self.assertLogs("app.approvals", level="WARNING"):
                        alone = await self._store(sqlite=sqlite)  # keys alone from here on: its key is its own
                writes_back = alone._as_the_stores_hold_it
                good_starts: list[ApprovalStore] = []

                async def a_good_start_between(approval):
                    good_starts.append(await self._store(sqlite=sqlite))  # rewrites the record under the file's key
                    return await writes_back(approval)

                with patch.object(alone, "_as_the_stores_hold_it", a_good_start_between):
                    await alone.reject(rejected, "still no")

                under_the_files_key = good_starts[0]._stored_action(_tool_call("a")).text
                self.store = alone
                self.assertEqual(await self._stored_texts(rejected), {under_the_files_key})
                self.assertEqual((await alone.get(rejected)).decision_comment, "still no")
                self.store = await self._store(sqlite=sqlite)
                await self._asked_again_and_refused_the_agents_approval("a")

    async def test_a_record_whose_decision_time_cannot_be_read_is_kept_and_logged_once(self) -> None:
        undated = await self._decided("rejected", days=31, session_id="closed000001")
        garbled = await self._decided("executed", days=31)
        for approval_id, value in ((undated, None), (garbled, "the day before yesterday")):
            record = await self.store.get(approval_id)
            record.decided_at = value
            await self.store._persist(record)

        with self.assertLogs("app.approvals", level="WARNING") as logged:
            self.assertEqual(await self.store.prune_decided(), 0)
            self.assertEqual(await self.store.prune_decided(), 0)

        self.assertEqual(await self._ids(), {undated, garbled})
        for approval_id in (undated, garbled):
            self.assertEqual(sum(approval_id in line for line in logged.output), 1, logged.output)
        self.assertEqual(self.audit.events, [])

    async def test_a_decision_time_the_platform_cannot_place_keeps_its_record_and_not_the_pass(self) -> None:
        # A time with no offset is read as local time, and Windows raises OSError for one before 1970. The error
        # left the pass, which then removed nothing at every start and sweep while that record existed.
        unplaceable = await self._decided("executed", days=31)
        record = await self.store.get(unplaceable)
        record.decided_at = "1960-01-01T00:00:00"
        await self.store._persist(record)
        old = await self._decided("executed", days=31)
        parse = ApprovalStore._parse_timestamp

        def parse_as_windows(value: str) -> datetime:
            if value == "1960-01-01T00:00:00":
                raise OSError(22, "Invalid argument")
            return parse(value)

        with (
            patch.object(ApprovalStore, "_parse_timestamp", staticmethod(parse_as_windows)),
            self.assertLogs("app.approvals", level="WARNING") as logged,
        ):
            self.assertEqual(await self.store.prune_decided(), 1)
            self.assertEqual(await self.store.prune_decided(), 0)

        self.assertEqual(await self._ids(), {unplaceable})
        self.assertNotIn(old, self._in_sqlite())
        self.assertEqual(sum(unplaceable in line for line in logged.output), 1, logged.output)
        self.assertEqual([event["details"]["count"] for event in self.audit.events], [1])

    async def test_nothing_is_removed_while_sqlite_is_configured_and_unavailable(self) -> None:
        # SQLite is caught up from the file records at its next start: one removed meanwhile could not show that
        # SQLite's copy of it is stale (pending there, rejected since).
        old = await self._decided("executed", days=31)
        down = ApprovalStore(self.root, db_path=str(self.db_path), retention_days=30, audit=self.audit)
        down.sqlite_store.startup = AsyncMock(side_effect=RuntimeError("database is locked"))
        with self.assertLogs("app.approvals", level="WARNING"):
            await down.startup()
        self.assertIs(down._primary, down.file_store)

        with self.assertLogs("app.approvals", level="WARNING") as logged:
            self.assertEqual(await down.prune_decided(), 0)
            self.assertEqual(await down.prune_decided(), 0)

        self.assertEqual(self._on_disk(), {old})
        self.assertEqual(len([line for line in logged.output if "retention" in line]), 1, logged.output)

    async def test_a_record_whose_file_cannot_be_removed_stays_in_both_stores(self) -> None:
        stuck = await self._decided("executed", days=31)
        gone = await self._decided("executed", days=31)
        unlink = Path.unlink

        def refusing_unlink(path: Path, missing_ok: bool = False) -> None:
            if path.name == f"{stuck}.json":
                raise PermissionError(13, "Permission denied", str(path))
            unlink(path, missing_ok=missing_ok)

        with patch.object(Path, "unlink", refusing_unlink):
            with self.assertLogs("app.approval_backends", level="WARNING") as logged:
                removed = await self.store.prune_decided()

        self.assertEqual(removed, 1)
        self.assertEqual(await self._ids(), {stuck})
        self.assertEqual(self._on_disk(), {stuck})
        self.assertNotIn(gone, self._in_sqlite())
        self.assertTrue(any(stuck in line for line in logged.output), logged.output)
        self.assertEqual(self.audit.events[-1]["details"]["count"], 1)

    async def test_arguments_that_cannot_be_removed_keep_their_record_for_the_next_pass(self) -> None:
        # The record went first and was counted: with it gone no pass could find its arguments again, and they
        # stayed until a restart's sweep.
        action = BrowserActionDecision(action="request_human_takeover", reason="browser.eval_js sha256:0", text="t")
        approval = await self.store.create_or_reuse_pending(
            session_id=SESSION, kind="write", reason="r", action=action, origin="tool_call", arguments='{"a": 1}'
        )
        await self.store.approve(approval.id)
        await _age(self.store, approval.id, days=31)
        gone = await self._decided("executed", days=31)
        unlink = Path.unlink

        def refusing_unlink(path: Path, missing_ok: bool = False) -> None:
            if path.name == f"{approval.id}.arguments":
                raise PermissionError(13, "The process cannot access the file", str(path))
            unlink(path, missing_ok=missing_ok)

        with patch.object(Path, "unlink", refusing_unlink):
            with self.assertLogs("app.approval_backends", level="WARNING") as logged:
                removed = await self.store.prune_decided()

        self.assertEqual(removed, 1)
        self.assertEqual(await self._ids(), {approval.id})
        self.assertEqual(self._on_disk(), {approval.id})
        self.assertEqual(self._in_sqlite(), {approval.id})
        self.assertTrue(await self.store.file_store.has_arguments(approval.id))
        self.assertNotIn(gone, self._on_disk())
        self.assertTrue(any(approval.id in line for line in logged.output), logged.output)
        self.assertEqual(self.audit.events[-1]["details"]["count"], 1)

        # The next pass, with no restart between, removes both.
        self.assertEqual(await self.store.prune_decided(), 1)
        self.assertEqual([path.name for path in self.root.iterdir()], [DIGEST_KEY_FILE])
        self.assertEqual(self._in_sqlite(), set())

    async def test_a_pass_that_fails_is_logged_and_raises_nothing(self) -> None:
        old = await self._decided("executed", days=31)
        remove_many = self.store.sqlite_store.remove_many
        self.store.sqlite_store.remove_many = AsyncMock(side_effect=sqlite3.OperationalError("database is locked"))

        with self.assertLogs("app.approvals", level="ERROR") as logged:
            removed = await self.store.prune_decided()

        self.assertEqual(removed, 0)
        self.assertIn("database is locked", "\n".join(logged.output))
        # Still SQLite's record, so still listed, and nothing is audited yet: the next pass takes it.
        self.assertEqual(await self._ids(), {old})
        self.assertEqual(self.audit.events, [])
        self.store.sqlite_store.remove_many = remove_many
        self.assertEqual(await self.store.prune_decided(), 1)
        self.assertEqual([event["details"]["count"] for event in self.audit.events], [1])

    async def test_a_cancelled_pass_lands_whole_and_is_audited_before_the_cancellation_is_raised(self) -> None:
        old = await self._decided("executed", days=31)
        remove_many, removing, release = self.store.file_store.remove_many, asyncio.Event(), asyncio.Event()

        async def slow_remove(approval_ids: list[str]) -> list[str]:
            removing.set()
            await release.wait()
            return await remove_many(approval_ids)

        self.store.file_store.remove_many = slow_remove  # type: ignore[method-assign]
        run = asyncio.create_task(self.store.prune_decided())
        await asyncio.wait_for(removing.wait(), timeout=5)
        run.cancel()
        await asyncio.sleep(0)
        self.assertTrue(self.store._lock.locked(), "the pass gave up the lock while its removal was under way")
        release.set()

        with self.assertRaises(asyncio.CancelledError):
            await run
        self.assertNotIn(old, self._in_sqlite())
        self.assertNotIn(old, self._on_disk())
        self.assertEqual([event["details"]["count"] for event in self.audit.events], [1])

    async def test_an_audit_that_fails_is_logged_and_the_count_still_returned(self) -> None:
        await self._decided("executed", days=31)
        self.audit.append = AsyncMock(side_effect=OSError("No space left on device"))  # type: ignore[method-assign]

        with self.assertLogs("app.approvals", level="ERROR") as logged:
            removed = await self.store.prune_decided()

        self.assertEqual(removed, 1)
        self.assertIn("No space left on device", "\n".join(logged.output))

    async def test_a_retention_longer_than_the_calendar_removes_nothing(self) -> None:
        self.store = await self._store(retention_days=1e9)
        old = await self._decided("executed", days=4000)

        with self.assertNoLogs("app.approvals", level="WARNING"):
            self.assertEqual(await self.store.prune_decided(), 0)

        self.assertEqual(await self._ids(), {old})


class FakePage:
    def __init__(self, url: str = "https://example.com"):
        self.url = url

    async def title(self) -> str:
        return "Example Domain"


class PrunedApprovalIdTests(unittest.IsolatedAsyncioTestCase):
    """What a caller gets for the id of an approval the retention removed: what an unknown id gets."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = _settings(Path(self.tmp.name))
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
        self.manager.sessions[self.session.id] = self.session

    async def test_a_removed_approvals_id_runs_nothing_and_reads_as_unknown(self) -> None:
        self.manager.click = AsyncMock(return_value={"action": "click"})  # type: ignore[method-assign]
        decision = BrowserActionDecision(action="click", reason="pay", element_id="op-pay", risk_category="payment")
        with self.assertRaises(Exception) as asked:
            await self.manager.execute_decision(self.session.id, decision)
        approval_id = asked.exception.approval.id
        await self.manager.approve(approval_id)
        await self.manager.execute_decision(self.session.id, decision, approval_id=approval_id)
        self.manager.click.assert_awaited_once()
        store = self.manager.approvals
        self.assertEqual(store.retention_days, 30)
        await _age(store, approval_id, days=31)

        self.assertEqual(await store.prune_decided(), 1)

        with self.assertRaises(KeyError):
            await store.require_approved(
                approval_id=approval_id, session_id=self.session.id, kind="payment", action=decision
            )
        with self.assertRaises(KeyError):
            await self.manager.execute_decision(self.session.id, decision, approval_id=approval_id)
        self.manager.click.assert_awaited_once()
        for call in (
            self.manager.get_approval,
            self.manager.approve,
            self.manager.reject,
            self.manager.execute_approval,
        ):
            with self.subTest(call=call.__name__), self.assertRaises(KeyError):
                await call(approval_id)
        events = await self.manager.audit.list(event_type="approvals_pruned")
        self.assertEqual([event.details["count"] for event in events], [1])


class _Stop(Exception):
    """Ends a manager's startup where it would start Playwright: the stores have started by then."""


class _NoPlaywright:
    async def start(self) -> None:
        raise _Stop


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = _settings(self.root, STATE_DB_PATH=str(self.root / "state.db"), WITNESS_ENABLED="false")

    async def _seed(self) -> tuple[str, str, str]:
        """An approval executed 31 days ago, one executed a day ago and a pending one, as an earlier run left them."""
        earlier = BrowserManager(self.settings).approvals
        await earlier.startup()
        ids = []
        for index, days in enumerate((31, 1, None)):
            action = BrowserActionDecision(
                action="click", reason="pay", selector=f"#pay-{index}", risk_category="payment"
            )
            approval = await earlier.create_or_reuse_pending(
                session_id=SESSION, kind="payment", reason="r", action=action
            )
            ids.append(approval.id)
            if days is not None:
                await earlier.approve(approval.id)
                await earlier.mark_executed(approval.id)
                await _age(earlier, approval.id, days=days)
        return ids[0], ids[1], ids[2]

    async def _start(self, manager: BrowserManager) -> None:
        with (
            patch("app.process_hardening.confine_playwright_driver_environment"),
            patch.object(browser_manager, "async_playwright", return_value=_NoPlaywright()),
            self.assertRaises(_Stop),
        ):
            await manager.startup()

    async def test_a_start_removes_what_is_past_the_retention_and_audits_it_in_both_audit_stores(self) -> None:
        old, recent, pending = await self._seed()
        manager = BrowserManager(self.settings)

        await self._start(manager)

        self.assertEqual({record.id for record in await manager.approvals.list()}, {recent, pending})
        self.assertFalse((Path(self.settings.approval_root) / f"{old}.json").exists())
        # The audit store was started first: its SQLite half, the one listings read, has the event too.
        self.assertIs(manager.audit._primary, manager.audit.sqlite_store)
        events = await manager.audit.list(event_type="approvals_pruned")
        self.assertEqual([(event.details["count"], event.operator.source) for event in events], [(1, "system")])

    async def test_a_start_with_the_retention_off_removes_nothing(self) -> None:
        old, recent, pending = await self._seed()
        self.settings.approval_retention_days = 0
        manager = BrowserManager(self.settings)

        await self._start(manager)

        self.assertEqual({record.id for record in await manager.approvals.list()}, {old, recent, pending})

    async def test_a_pass_that_fails_at_a_start_is_logged_and_the_start_goes_on(self) -> None:
        old, _, _ = await self._seed()
        manager = BrowserManager(self.settings)
        manager.approvals.file_store.remove_many = AsyncMock(side_effect=OSError("Read-only file system"))

        with self.assertLogs("app.approvals", level="ERROR") as logged:
            await self._start(manager)  # reached Playwright: the failure did not end the startup

        self.assertIn("Read-only file system", "\n".join(logged.output))
        self.assertIn(old, {record.id for record in await manager.approvals.list()})


class MaintenanceSweepTests(RetentionBase):
    def _service(self, sessions: list[object]) -> MaintenanceService:
        settings = _settings(Path(self.tmp.name), CLEANUP_ON_STARTUP="false", CLEANUP_INTERVAL_SECONDS="0")
        return MaintenanceService(settings, session_provider=lambda: sessions, approval_store=self.store)

    async def test_a_sweep_runs_the_pass_and_names_the_open_sessions_to_it(self) -> None:
        old = await self._decided("executed", days=31)
        open_session = await self._decided("rejected", days=31, session_id="open00000001")
        closed_session = await self._decided("rejected", days=31, session_id="closed000001")
        # A session object with no id (a test double) names no session.
        service = self._service([SimpleNamespace(id="open00000001"), object()])

        await service.run_cleanup()

        self.assertEqual(await self._ids(), {open_session})
        self.assertFalse({old, closed_session} & self._on_disk())
        self.assertEqual([event["details"]["count"] for event in self.audit.events], [2])

    async def test_a_sweep_whose_pass_fails_still_reports(self) -> None:
        await self._decided("executed", days=31)
        self.store.file_store.remove_many = AsyncMock(side_effect=OSError("Read-only file system"))
        service = self._service([])

        with self.assertLogs("app.approvals", level="ERROR"):
            report = await service.run_cleanup()

        self.assertEqual(len(report["roots"]), 3)

    async def test_a_service_given_no_store_sweeps_as_before(self) -> None:
        settings = _settings(Path(self.tmp.name), CLEANUP_ON_STARTUP="false", CLEANUP_INTERVAL_SECONDS="0")
        service = MaintenanceService(settings, session_provider=lambda: [])

        self.assertEqual(len((await service.run_cleanup())["roots"]), 3)


class RetentionSettingTests(unittest.TestCase):
    def test_the_default_is_thirty_days_and_zero_turns_it_off(self) -> None:
        self.assertEqual(Settings(_env_file=None).approval_retention_days, 30)
        self.assertEqual(Settings(_env_file=None, APPROVAL_RETENTION_DAYS="0").approval_retention_days, 0)
        self.assertEqual(Settings(_env_file=None, APPROVAL_RETENTION_DAYS="7.5").approval_retention_days, 7.5)

    def test_a_negative_or_non_finite_retention_stops_startup_and_names_the_setting(self) -> None:
        for bad in ("-1", "-0.5", "nan", "inf"):
            with self.subTest(bad=bad), self.assertRaises(ValidationError) as caught:
                Settings(_env_file=None, APPROVAL_RETENTION_DAYS=bad)
            self.assertIn("approval_retention_days", str(caught.exception).lower())

    def test_the_controller_wires_the_setting_to_the_store_and_the_store_to_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            settings = _settings(Path(tempdir), APPROVAL_RETENTION_DAYS="7")
            services = build_controller_services(settings, version="test")

            self.assertEqual(services.manager.approvals.retention_days, 7)
            self.assertIs(services.maintenance.approval_store, services.manager.approvals)
