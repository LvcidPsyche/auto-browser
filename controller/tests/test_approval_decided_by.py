"""An approval records which named operator decided it, and a deployment may refuse an operator's own approvals.

AUTH-36: a record said only how it was decided (``decided_via``: an operator or an agent), never by whom, so
the approvals and the audit trail could not tell whose approve let an action run, and a deferred rejection's give-back
was audited as the operator running the action. ``decided_by`` is the token-verified operator who decided
(``verified_operator()``): None when no named credential proved one, and for a record stored before the field.
``APPROVAL_REFUSE_SELF_APPROVAL_OPERATORS`` names operators who may not approve what they asked for.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.approvals import ApprovalStore, ServerStateRefusal, held_for_execution, refused_before_effect
from app.audit import get_current_operator
from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.models import BrowserActionDecision, OperatorIdentity
from app.runtime_policy import validate_runtime_policy
from app.session_ownership import acting_as
from app.utils import UTC

SESSION = "session-1"
ALICE = OperatorIdentity(id="alice", source="token")
BOB = OperatorIdentity(id="bob", source="token")
CAROL = OperatorIdentity(id="carol", source="token")
# Claimed by the X-Operator-Id header, proven by nothing.
HEADER_BOB = OperatorIdentity(id="bob", source="header")


def _action(number: int = 0) -> BrowserActionDecision:
    return BrowserActionDecision(action="click", reason="pay", selector=f"#pay-{number}", risk_category="payment")


class _Audit:
    """An audit store that keeps what it is given, and who was current when it was."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    async def append(self, **event) -> None:
        self.events.append({**event, "operator": get_current_operator()})

    def of(self, event_type: str) -> list[dict]:
        return [event for event in self.events if event["event_type"] == event_type]


class StoreBase(unittest.IsolatedAsyncioTestCase):
    refuse: frozenset[str] = frozenset()

    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "approvals"
        self.db_path = Path(tmp.name) / "state.db"
        self.audit = _Audit()
        self.store = ApprovalStore(
            self.root, db_path=str(self.db_path), audit=self.audit, refuse_self_approval=self.refuse
        )
        await self.store.startup()
        self._asked = 0

    async def _ask(self, operator: OperatorIdentity | None = ALICE) -> str:
        self._asked += 1
        with acting_as(operator):
            approval = await self.store.create_or_reuse_pending(
                session_id=SESSION, kind="payment", reason="r", action=_action(self._asked)
            )
        return approval.id

    async def _decided_by_in_each_store(self, approval_id: str) -> tuple[str | None, str | None]:
        return (
            (await self.store.file_store.get(approval_id)).decided_by,
            (await self.store.sqlite_store.get(approval_id)).decided_by,
        )


class DecidedByTests(StoreBase):
    async def test_the_deciding_operator_is_recorded_in_both_stores(self) -> None:
        approved, rejected = await self._ask(), await self._ask()

        with acting_as(BOB):
            await self.store.approve(approved)
        with acting_as(CAROL):
            await self.store.reject(rejected)

        self.assertEqual(await self._decided_by_in_each_store(approved), ("bob", "bob"))
        self.assertEqual(await self._decided_by_in_each_store(rejected), ("carol", "carol"))
        self.assertIsNone((await self.store.get(await self._ask())).decided_by, "a pending approval names no one")

    async def test_a_decision_no_named_credential_proves_records_none(self) -> None:
        for operator in (HEADER_BOB, None):
            with self.subTest(operator=operator):
                approval_id = await self._ask(None)
                with acting_as(operator):
                    record = await self.store.approve(approval_id)

                self.assertIsNone(record.decided_by)
                self.assertEqual(await self._decided_by_in_each_store(approval_id), (None, None))

    async def test_an_agents_decision_names_the_operator_it_acts_for(self) -> None:
        approval_id = await self._ask()

        with acting_as(ALICE):
            record = await self.store.approve(approval_id, decided_via="agent")

        self.assertEqual((record.decided_via, record.decided_by), ("agent", "alice"))

    async def test_a_record_stored_before_the_field_reads_back_with_none(self) -> None:
        approval_id = await self._ask()
        with acting_as(BOB):
            await self.store.approve(approval_id)
        path = self.root / f"{approval_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        del payload["decided_by"]
        path.write_text(json.dumps(payload), encoding="utf-8")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE approvals SET payload = ? WHERE id = ?", (json.dumps(payload), approval_id))

        self.assertEqual(await self._decided_by_in_each_store(approval_id), (None, None))
        self.assertEqual([record.decided_by for record in await self.store.list()], [None])


class DeferredRejectionTests(StoreBase):
    """A reject that waits on a claim carries its operator to the record and the audit, wherever it ends."""

    async def _approved_by_bob(self) -> str:
        approval_id = await self._ask()
        with acting_as(BOB):
            await self.store.approve(approval_id)
        return approval_id

    async def test_a_rejection_during_the_claim_names_its_operator_once_the_give_back_applies_it(self) -> None:
        approval_id = await self._approved_by_bob()
        refusal = PermissionError("refused before the operation")

        with acting_as(ALICE), self.assertRaises(PermissionError):
            async with held_for_execution(self.store, approval_id, may_give_back=True):
                with acting_as(CAROL):
                    await self.store.reject(approval_id, comment="no")  # deferred on the claim
                self.assertEqual((await self.store.get(approval_id)).decided_by, "bob")
                refused_before_effect(refusal)
                raise refusal

        record = await self.store.get(approval_id)
        self.assertEqual(
            (record.status, record.decision_comment, record.decided_via, record.decided_by),
            ("rejected", "no", "operator", "carol"),
        )
        self.assertEqual(await self._decided_by_in_each_store(approval_id), ("carol", "carol"))
        [given_back] = self.audit.of("approval_given_back")
        # Written by the execution's task, as its operator: the details name who rejected.
        self.assertEqual((given_back["operator"].id, given_back["details"]["decided_by"]), ("alice", "carol"))

    async def test_a_rejection_the_give_back_could_not_apply_is_audited_with_its_operator(self) -> None:
        approval_id = await self._approved_by_bob()
        refusal = PermissionError("refused before the operation")

        with acting_as(ALICE), self.assertRaises(OSError):
            async with held_for_execution(self.store, approval_id, may_give_back=True):
                with acting_as(CAROL):
                    await self.store.reject(approval_id, comment="no")
                refused_before_effect(refusal)
                # The give-back's write, once the block has ended.
                failing = patch.object(self.store.file_store, "upsert", AsyncMock(side_effect=OSError("disk full")))
                failing.start()
                self.addCleanup(failing.stop)
                raise refusal

        [not_applied] = [event for event in self.audit.of("approval_decision") if event["status"] == "not_applied"]
        self.assertEqual(
            (not_applied["details"]["decided_by"], not_applied["details"]["decided_via"]), ("carol", "operator")
        )

    async def test_withdrawing_the_waiting_rejection_leaves_the_approvers_name(self) -> None:
        approval_id = await self._approved_by_bob()

        async with held_for_execution(self.store, approval_id, may_give_back=True):
            with acting_as(CAROL):
                await self.store.reject(approval_id, comment="no")
            with acting_as(BOB):
                await self.store.approve(approval_id)  # withdraws it

        record = await self.store.get(approval_id)
        self.assertEqual((record.status, record.decided_by), ("executed", "bob"))
        self.assertEqual(self.audit.of("approval_given_back"), [])


class RefuseSelfApprovalTests(StoreBase):
    refuse = frozenset({"alice"})

    async def test_a_listed_operator_may_not_approve_what_they_asked_for(self) -> None:
        approval_id = await self._ask()

        with acting_as(ALICE), self.assertRaisesRegex(ServerStateRefusal, "APPROVAL_REFUSE_SELF_APPROVAL_OPERATORS"):
            await self.store.approve(approval_id)

        self.assertEqual((await self.store.get(approval_id)).status, "pending")
        with acting_as(BOB):
            record = await self.store.approve(approval_id)
        self.assertEqual((record.status, record.decided_by), ("approved", "bob"))

    async def test_the_list_leaves_every_other_decision_alone(self) -> None:
        # Their own rejection, an agent acting for them, an operator not on the list, and a request nobody owns.
        own_rejection, by_their_agent = await self._ask(), await self._ask()
        bobs, unowned = await self._ask(BOB), await self._ask(None)

        with acting_as(ALICE):
            self.assertEqual((await self.store.reject(own_rejection)).status, "rejected")
            self.assertEqual((await self.store.approve(by_their_agent, decided_via="agent")).status, "approved")
            self.assertEqual((await self.store.approve(unowned)).decided_by, "alice")
        with acting_as(BOB):
            self.assertEqual((await self.store.approve(bobs)).decided_by, "bob")

    async def test_nor_may_they_withdraw_a_rejection_waiting_on_their_own_request(self) -> None:
        # The withdraw is an approve of what they asked for: the rejection stands and the action is refused.
        approval_id = await self._ask()
        with acting_as(BOB):
            await self.store.approve(approval_id)

        with self.assertRaises(PermissionError):
            async with held_for_execution(self.store, approval_id, may_give_back=True):
                with acting_as(ALICE):
                    await self.store.reject(approval_id, comment="wait")
                    with self.assertRaises(ServerStateRefusal):
                        await self.store.approve(approval_id)
                refusal = PermissionError("refused before the operation")
                refused_before_effect(refusal)
                raise refusal

        self.assertEqual((await self.store.get(approval_id)).decided_by, "alice")

    async def test_off_by_default(self) -> None:
        self.assertEqual(Settings(_env_file=None).approval_refuse_self_approval_operator_ids, frozenset())
        store = ApprovalStore(self.root)
        await store.startup()
        with acting_as(ALICE):
            approval = await store.create_or_reuse_pending(
                session_id=SESSION, kind="payment", reason="r", action=_action(99)
            )
            self.assertEqual((await store.approve(approval.id)).decided_by, "alice")


class ListedOperatorTests(unittest.TestCase):
    """A start names each listed id that refuses nobody: the list is compared to the proven operator id exactly."""

    TOKENS = "alice:" + "a" * 40 + ",bob:" + "b" * 40

    def _warnings(self, listed: str, tokens: str = TOKENS) -> list[str]:
        settings = Settings(
            _env_file=None,
            API_BIND_SCOPE="loopback",
            API_BEARER_TOKENS=tokens,
            APPROVAL_REFUSE_SELF_APPROVAL_OPERATORS=listed,
        )
        warnings = validate_runtime_policy(settings).warnings
        return [warning for warning in warnings if "APPROVAL_REFUSE_SELF_APPROVAL_OPERATORS" in warning]

    def test_a_listed_id_a_credential_proves_is_not_warned_about(self) -> None:
        self.assertEqual(self._warnings("alice, bob"), [])
        self.assertEqual(self._warnings(""), [])

    def test_a_listed_id_no_credential_proves_is_named(self) -> None:
        warnings = self._warnings("alice,carol,dave")

        self.assertEqual(len(warnings), 2, warnings)
        self.assertIn("'carol'", warnings[0])
        self.assertIn("'dave'", warnings[1])
        self.assertIn("refuses nobody", warnings[0])
        self.assertNotIn("only by case", warnings[0])

    def test_an_id_that_differs_from_a_credentials_only_by_case_says_so(self) -> None:
        [warning] = self._warnings("Alice,bob")

        self.assertIn("'Alice'", warning)
        self.assertIn("differs from 'alice' only by case", warning)

    def test_an_id_whose_token_an_earlier_entry_repeats_is_proved_by_no_credential(self) -> None:
        # A request with that token proves the first operator listed with it, so nobody is ever bob.
        [warning] = self._warnings("alice,bob", tokens="alice:" + "a" * 40 + ",bob:" + "a" * 40)

        self.assertIn("'bob'", warning)
        self.assertIn("proves only the first operator listed with it", warning)

    def test_with_no_named_credential_every_listed_id_is_named(self) -> None:
        self.assertEqual(len(self._warnings("alice,bob", tokens="")), 2)

    def test_no_warning_holds_a_token(self) -> None:
        warnings = self._warnings("Alice,carol")

        # Both are there to be read: with no warning at all, the loop below passed over nothing.
        self.assertEqual(len(warnings), 2, warnings)
        for warning in warnings:
            self.assertNotIn("a" * 40, warning)
            self.assertNotIn("b" * 40, warning)


class FakePage:
    url = "https://example.com"

    async def title(self) -> str:
        return "Example Domain"


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    """Through the manager: the setting reaches the store, and the views and decision events name the decider."""

    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.settings = Settings(_env_file=None, APPROVAL_REFUSE_SELF_APPROVAL_OPERATORS=" alice ,, dave ")
        # The witness root too: a test reads the session's whole chain, which the suite's shared root keeps.
        for name in ("artifact", "upload", "auth", "approval", "audit", "witness"):
            setattr(self.settings, f"{name}_root", str(root / name))
        self.settings.session_store_root = str(root / "sessions")
        self.manager = BrowserManager(self.settings)
        artifact_dir = root / "artifact" / SESSION
        artifact_dir.mkdir(parents=True)
        # Created under the shared token: nobody owns it, so every operator may decide its approvals.
        self.manager.sessions[SESSION] = BrowserSession(
            id=SESSION,
            name=SESSION,
            created_at=datetime.now(UTC),
            context=object(),  # type: ignore[arg-type]
            page=FakePage(),  # type: ignore[arg-type]
            artifact_dir=artifact_dir,
            auth_dir=root / "auth" / SESSION,
            upload_dir=root / "upload" / SESSION,
            takeover_url="http://127.0.0.1:6080/vnc.html",
            trace_path=artifact_dir / "trace.zip",
        )

    async def _ask(self, number: int = 0) -> str:
        with acting_as(ALICE), self.assertRaises(Exception) as asked:
            await self.manager.execute_decision(SESSION, _action(number))
        return asked.exception.approval.id

    async def _decision_events(self) -> list[tuple[str, str | None]]:
        events = await self.manager.audit.list(event_type="approval_decision")
        return [(event.status, event.details.get("decided_by")) for event in reversed(events)]

    async def _decision_receipts(self) -> list[tuple[str, str, str, str | None, str | None]]:
        """Each decision's witness receipt, oldest first: its action and status, the operator whose task wrote
        it, and how and by whom its metadata says the decision was made."""
        return [
            (
                item["action"],
                item["status"],
                item["operator"]["id"],
                item["metadata"]["decided_via"],
                item["metadata"]["decided_by"],
            )
            for item in reversed(await self.manager.list_witness_receipts(SESSION))
            if item["event_type"] == "approval" and item["action"] in {"approve", "reject"}
        ]

    def test_the_setting_is_read_as_a_list_of_operator_ids(self) -> None:
        self.assertEqual(self.manager.approvals.refuse_self_approval, frozenset({"alice", "dave"}))

    async def test_the_views_and_the_decision_events_name_who_decided(self) -> None:
        approval_id = await self._ask()
        with acting_as(ALICE), self.assertRaises(ServerStateRefusal):
            await self.manager.approve(approval_id)

        with acting_as(BOB):
            approved = await self.manager.approve(approval_id, comment="ok")
            shown = await self.manager.get_approval(approval_id)
            listed = await self.manager.list_approvals()

        self.assertEqual((approved["decided_by"], shown["decided_by"]), ("bob", "bob"))
        self.assertEqual([item["decided_by"] for item in listed], ["bob"])
        asked_again = await self._ask()
        with acting_as(CAROL):
            self.assertEqual((await self.manager.reject(asked_again))["decided_by"], "carol")
        self.assertEqual(await self._decision_events(), [("approved", "bob"), ("rejected", "carol")])

    async def test_a_deferred_reject_and_its_withdrawal_are_audited_with_their_operators(self) -> None:
        approval_id = await self._ask()
        with acting_as(BOB):
            await self.manager.approve(approval_id)

        async with held_for_execution(self.manager.approvals, approval_id, may_give_back=True):
            with acting_as(CAROL):
                await self.manager.reject(approval_id, comment="no")
            with acting_as(BOB):
                await self.manager.approve(approval_id, comment="go on")

        self.assertEqual(
            await self._decision_events(), [("approved", "bob"), ("deferred", "carol"), ("withdrawn", "bob")]
        )

    async def test_a_decision_receipt_names_who_decided(self) -> None:
        approved, rejected, by_an_agent = await self._ask(1), await self._ask(2), await self._ask(3)

        with acting_as(BOB):
            await self.manager.approve(approved)
        with acting_as(CAROL):
            await self.manager.reject(rejected)
            await self.manager.approve(by_an_agent, decided_via="agent")

        self.assertEqual(
            await self._decision_receipts(),
            [
                ("approve", "approved", "bob", "operator", "bob"),
                ("reject", "rejected", "carol", "operator", "carol"),
                ("approve", "approved", "carol", "agent", "carol"),
            ],
        )

    async def test_the_receipt_of_a_rejection_a_give_back_applies_names_who_rejected(self) -> None:
        approval_id = await self._ask()
        with acting_as(BOB):
            await self.manager.approve(approval_id)
        refusal = PermissionError("refused before the operation")

        # Written by the task of the operator whose action was refused: only the metadata names who rejected.
        with acting_as(ALICE), self.assertRaises(PermissionError):
            async with held_for_execution(self.manager.approvals, approval_id, may_give_back=True):
                with acting_as(CAROL):
                    await self.manager.reject(approval_id, comment="no")  # deferred on the claim
                refused_before_effect(refusal)
                raise refusal

        self.assertEqual(
            await self._decision_receipts(),
            [("approve", "approved", "bob", "operator", "bob"), ("reject", "rejected", "alice", "operator", "carol")],
        )

    async def test_the_receipt_of_a_withdrawn_rejection_names_who_withdrew_it(self) -> None:
        approval_id = await self._ask()
        with acting_as(BOB):
            await self.manager.approve(approval_id)
        refusal = PermissionError("refused before the operation")

        with acting_as(ALICE), self.assertRaises(PermissionError):
            async with held_for_execution(self.manager.approvals, approval_id, may_give_back=True):
                with acting_as(CAROL):
                    await self.manager.reject(approval_id, comment="no")
                refused_before_effect(refusal)
                with acting_as(CAROL):
                    # After the refusal: the approval goes back to approved, and this approve gets a receipt.
                    await self.manager.approve(approval_id, comment="go on")
                raise refusal

        # The record keeps the approver the withdraw restores; the receipt is of the withdraw, and names its operator.
        self.assertEqual((await self.manager.approvals.get(approval_id)).decided_by, "bob")
        self.assertEqual(
            await self._decision_receipts(),
            [("approve", "approved", "bob", "operator", "bob"), ("approve", "approved", "carol", "operator", "carol")],
        )

    async def test_with_no_named_credential_the_views_show_none(self) -> None:
        with self.assertRaises(Exception) as asked:
            await self.manager.execute_decision(SESSION, _action())
        approval_id = asked.exception.approval.id

        approved = await self.manager.approve(approval_id)

        self.assertIsNone(approved["decided_by"])
        self.assertIsNone((await self.manager.get_approval(approval_id))["decided_by"])
        self.assertEqual(await self._decision_events(), [("approved", None)])


if __name__ == "__main__":
    unittest.main()
