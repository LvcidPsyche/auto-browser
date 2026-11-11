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
import sys
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
from app.runtime_policy import (
    MIN_SHORT_TOKEN_RUN_NOT_PRINTED,
    MIN_TOKEN_RUN_NOT_PRINTED,
    validate_runtime_policy,
)
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


# Two letters whose case fold is longer than they are, by their names: sharp s folds to "ss", the ligature to "fi".
SHARP_S = "\N{LATIN SMALL LETTER SHARP S}"
FI = "\N{LATIN SMALL LIGATURE FI}"


def kept_back(entries: int) -> str:
    """How the warning of entries that are not printed begins. It counts entries: the tokens they hold it cannot
    count, since tokens that begin alike are all held by an entry that holds one. The eight is written out: it is
    what the CHANGELOG says, whatever MIN_TOKEN_RUN_NOT_PRINTED is changed to."""
    counted = "1 entry that is not printed here, because it holds"
    if entries != 1:
        counted = f"{entries} entries that are not printed here, because each holds"
    return f"lists {counted} a bearer token of this deployment or a part of one (8 characters in a row, or all but one"


class ListedOperatorTests(unittest.TestCase):
    """A start names each listed id that refuses nobody: the list is compared to the proven operator id exactly."""

    TOKENS = "alice:" + "a" * 40 + ",bob:" + "b" * 40

    def _warnings(self, listed: str, tokens: str = TOKENS, shared: str | None = None) -> list[str]:
        settings = Settings(
            _env_file=None,
            API_BIND_SCOPE="loopback",
            API_BEARER_TOKENS=tokens,
            API_BEARER_TOKEN=shared,
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

    def test_a_list_pasted_as_id_token_pairs_is_named_by_its_ids_alone(self) -> None:
        # .env.example calls the values "named credentials of API_BEARER_TOKENS": that list pasted whole holds every
        # token, and a start writes its warnings to the log.
        warnings = self._warnings(self.TOKENS)

        self.assertEqual(len(warnings), 2, warnings)
        self.assertIn("'alice:'", warnings[0])
        self.assertIn("'bob:'", warnings[1])
        for warning in warnings:
            self.assertIn("not id:token pairs", warning)
            self.assertIn("refuses nobody", warning)
            self.assertNotIn("a" * 40, warning)
            self.assertNotIn("b" * 40, warning)

    def test_nothing_after_an_entrys_colon_is_printed(self) -> None:
        # A token no credential here holds (another deployment's), one with a colon in it, and nothing before the colon.
        other, halves = "c" * 40, "d" * 20 + ":" + "e" * 20
        warnings = self._warnings(f"carol:{other}, alice:{halves}, :{other}, alice:{other}")

        # The two entries that begin "alice:" read the same once cut, and are named once.
        self.assertEqual(len(warnings), 3, warnings)
        self.assertEqual(sum("'alice:'" in warning for warning in warnings), 1)
        self.assertEqual(sum("'carol:'" in warning for warning in warnings), 1)
        self.assertEqual(sum("':'" in warning for warning in warnings), 1)
        for warning in warnings:
            for secret in (other, "d" * 20, "e" * 20):
                self.assertNotIn(secret, warning)

    def test_a_listed_bearer_token_is_not_printed(self) -> None:
        # A token in place of an id: a named credential's, the shared one, and one that holds a colon, which is not
        # to be cut at it as an id:token pair is.
        named, shared, with_colon = "a" * 40, "s" * 40, "f" * 20 + ":" + "g" * 20
        tokens = f"alice:{named},bob:{with_colon}"
        warnings = self._warnings(f"{named},{shared},{with_colon}", tokens=tokens, shared=shared)

        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn(kept_back(3), warnings[0])
        self.assertIn("refuses nobody", warnings[0])
        for secret in (named, shared, "f" * 20, "g" * 20):
            self.assertNotIn(secret, warnings[0])

    # Tokens with no run of characters in common, in mixed case, so that one in upper case is another string.
    A, B = "k3Jx9mQ2vT7pL5wR8nZ4cF6hB1dG0sYuAeIoXtNq", "P8rW2yH5jK9bV3mC7xD1fS4gL6zQ0nTaEiOuMpRk"
    WITH_COLON = "f4Gh7Jk2Lm9Np3Qr5St8:g5Vw1Xy6Zb4Cd0Ef2Hj7"
    WITH_COMMA = "s3Kd8Fj2Lq7Wp5Zx1Cv9,t7Bn4Mh6Gy0Ru3Te8Ia2"
    WITH_BOTH = "s3Kd8Fj2Lq:x5Zc1Vb9Nm,t7Bn4Mh6Gy0Ru3Te8Ia2"
    NAMED = f"alice:{A},bob:{B}"
    NAMED_WITH_COLON = f"alice:{A},bob:{WITH_COLON}"
    # Three tokens that begin alike, as tokens made to one pattern do.
    ALIKE = ",".join(
        f"ops{n}:prod-key-{part}" for n, part in enumerate(("1a2B3c4D5e6F7g8H", "9i0J1k2L3m4N5o6P", "7q8R9s0T"))
    )
    N = MIN_TOKEN_RUN_NOT_PRINTED
    # A row: what is listed, API_BEARER_TOKENS, API_BEARER_TOKEN, and a text each warning holds, in their order.
    # Where an entry is not exactly a token, each of these wrote a token, or a part of one, to the log.
    SHAPES = {
        "a token alone": (A, NAMED, None, [kept_back(1)]),
        "a token after Bearer": (f"Bearer {A}", NAMED, None, [kept_back(1)]),
        "two tokens and a space": (f"{A} {B}", NAMED, None, [kept_back(1)]),
        "two tokens and a semicolon": (f"{A};{B}", NAMED, None, [kept_back(1)]),
        "pairs written with =": (f"alice={A},bob={B}", NAMED, None, [kept_back(2)]),
        "the list in double quotes": (f'"{A},{B}"', NAMED, None, [kept_back(2)]),
        "a token in single quotes": (f"'{A}'", NAMED, None, [kept_back(1)]),
        "a token and one character more": (f"{A}x", NAMED, None, [kept_back(1)]),
        "a token cut short by one": (A[:-1], NAMED, None, [kept_back(1)]),
        "a token cut short, after a stray quote": ('"' + A[:-1], NAMED, None, [kept_back(1)]),
        "a token in upper case": (A.upper(), NAMED, None, [kept_back(1)]),
        # The list is split at commas and the shared token is not: its halves are two entries.
        "a shared token that holds a comma": (WITH_COMMA, NAMED, WITH_COMMA, [kept_back(2)]),
        "a shared token that holds a comma and a colon": (WITH_BOTH, NAMED, WITH_BOTH, [kept_back(2)]),
        "a token that holds a colon": (WITH_COLON, NAMED_WITH_COLON, None, [kept_back(1)]),
        "Bearer and a token that holds a colon": (
            f"Bearer {WITH_COLON}",
            NAMED_WITH_COLON,
            None,
            [kept_back(1)],
        ),
        "a token that holds a colon, and more after another": (
            f"{WITH_COLON}:x",
            NAMED_WITH_COLON,
            None,
            [kept_back(1)],
        ),
        "the first half of such a token before another colon": (
            f"{WITH_COLON[:20]}:zzz",
            NAMED_WITH_COLON,
            None,
            [kept_back(1)],
        ),
        "a pair the wrong way round": (f"{A}:alice", NAMED, None, [kept_back(1)]),
        "one token twice, alone and before a colon": (f"{A},{A}:x", NAMED, None, [kept_back(2)]),
        # The count is of entries. It was of the tokens an entry has eight characters in common with: 3 for each
        # of these, of which the first lists one token and the second none.
        "one of three tokens that begin alike": (
            ALIKE.partition(":")[2].partition(",")[0],
            ALIKE,
            None,
            [kept_back(1)],
        ),
        "an id that begins as three tokens do": ("prod-key-admins", ALIKE, None, [kept_back(1)]),
        # Both sides of MIN_TOKEN_RUN_NOT_PRINTED: with one more, or one fewer, every other shape here passed.
        "a token's first characters, as many as are not printed": (A[:N], NAMED, None, [kept_back(1)]),
        "a token's first characters, one fewer": (
            A[: N - 1],
            NAMED,
            None,
            [f"lists '{A[: N - 1]}', which no credential"],
        ),
        # What a start still names: an id, and the id of a pasted pair.
        "the list pasted whole": (NAMED, NAMED, None, ["begins 'alice:'", "begins 'bob:'"]),
        "an id nobody proves": ("carol", NAMED, None, ["lists 'carol', which no credential"]),
        "an id that differs by case": ("Alice", NAMED, None, ["It differs from 'alice' only by case"]),
        "a short id that stands inside a token": (A[5:10], NAMED, None, [f"lists '{A[5:10]}', which no credential"]),
        # A token of fewer than eight characters (a development setup's): an id that holds its letters is an id like
        # any other. Each was counted as a token and not named, and the slip of case in the third went unsaid.
        "an id that begins with a short token": (
            "developer,carol",
            "alice:dev",
            None,
            ["lists 'carol', which no credential", "lists 'developer', which no credential"],
        ),
        "an id that ends with a short token": ("max1", "alice:x1", None, ["lists 'max1', which no credential"]),
        "an id that is its own short token": ("Alice", "alice:alice", None, ["It differs from 'alice' only by case"]),
        # The token itself is still one, in whatever case: two entries.
        "a short token alone, in two cases": ("dev,DEV", "alice:dev", None, [kept_back(2)]),
        # So is such a token as a word of its own, with something round it, where it was named: on each side an end
        # of the entry or a character that is no letter and no digit. Its letters in a word are an id's.
        "a short token after Bearer": ("Bearer dev", "alice:dev", None, [kept_back(1)]),
        "a short token in single quotes": ("'dev'", "alice:dev", None, [kept_back(1)]),
        "a short token after an id and =": ("alice=dev", "alice:dev", None, [kept_back(1)]),
        "a short token before a hyphen, in upper case": ("DEV-team", "alice:dev", None, [kept_back(1)]),
        "a short token after an underscore": ("ops_x1", "alice:x1", None, [kept_back(1)]),
        "a short token's letters after a digit": ("4dev", "alice:dev", None, ["lists '4dev', which no credential"]),
        "a short token's letters before a digit": ("dev2", "alice:dev", None, ["lists 'dev2', which no credential"]),
        # A token of eight characters or fewer has no eight in a row short of itself, and cut short by one it was
        # named: six characters of seven, and seven of eight.
        "a token of seven cut short by one": ("s3cr3t", "alice:s3cr3t!", None, [kept_back(1)]),
        "a token of eight after Bearer, cut short by one": ("Bearer abcdefg", "alice:abcdefgh", None, [kept_back(1)]),
        "a token of eight without its first character": ("bcdefgh", "alice:abcdefgh", None, [kept_back(1)]),
        # Both sides of MIN_SHORT_TOKEN_RUN_NOT_PRINTED, written out: four in a row keep an entry back, three do not.
        "a token of five cut short by one": ("q7Zk", "alice:q7Zk2", None, [kept_back(1)]),
        "a token of four cut short by one": ("q7Z", "alice:q7Zk", None, ["lists 'q7Z', which no credential"]),
        # A token whose case fold is longer than it is: the run was counted from the fold, a character longer, so all
        # but one of a token of five to eight was named. It is counted from the token as it is configured.
        **{
            f"a token of {length} that begins with {name}, without it": (
                "1234567"[: length - 1],
                f"alice:{letter}{'1234567'[: length - 1]}",
                None,
                [kept_back(1)],
            )
            for name, letter in (("sharp s", SHARP_S), ("the ligature fi", FI))
            for length in range(5, 9)
        },
        # And whether a token is short is decided on its own length: of four with sharp s, all but one is an id's.
        "a token of four with sharp s, cut short by one": (
            f"{SHARP_S}12",
            f"alice:{SHARP_S}123",
            None,
            [f"lists '{SHARP_S}12', which no credential"],
        ),
        "such a token after Bearer, in upper case": ("Bearer SS123", f"alice:{SHARP_S}123", None, [kept_back(1)]),
    }

    def test_a_case_fold_makes_no_text_shorter(self) -> None:
        # What a token's run is counted from rests on this. Its length is taken from the token as it is configured
        # and its parts from the token's fold, which holds the fold of each run of k of the token's characters, of
        # k characters or more: so whatever holds such a run, in any case, holds one of those parts.
        shorter = [point for point in range(sys.maxunicode + 1) if not chr(point).casefold()]
        self.assertEqual(shorter, [])
        for token in (f"{SHARP_S}1234567", f"{FI}1234567", f"ab{SHARP_S}c{FI}d{SHARP_S}"):
            folded = token.casefold()
            self.assertGreater(len(folded), len(token))
            for length in range(1, len(token) + 1):
                for start in range(len(token) - length + 1):
                    part = token[start : start + length].casefold()
                    self.assertGreaterEqual(len(part), length, (token, start, length))
                    self.assertIn(part, folded, (token, start, length))

    def test_no_warning_holds_eight_characters_in_a_row_of_a_token(self) -> None:
        for shape, (listed, named, shared, expected) in self.SHAPES.items():
            with self.subTest(shape=shape):
                warnings = self._warnings(listed, tokens=named, shared=shared)

                self.assertEqual(len(warnings), len(expected), warnings)
                for warning, text in zip(warnings, expected, strict=True):
                    self.assertIn(text, warning)
                ids = {pair.partition(":")[0].casefold() for pair in named.split(",")}
                tokens = [pair.partition(":")[2] for pair in named.split(",")] + ([shared] if shared else [])
                said = "\n".join(warnings)
                for token in tokens:
                    if token.casefold() in ids:
                        # alice:alice, a development setup's: the token is the id, and an id is named.
                        continue
                    # Eight in a row, and for a token of eight or fewer all but one of its characters.
                    run = min(MIN_TOKEN_RUN_NOT_PRINTED, len(token) - 1)
                    if run < MIN_SHORT_TOKEN_RUN_NOT_PRINTED:
                        # Too short a run to tell from an id's letters: only the token itself is not named.
                        self.assertNotIn(f"'{token}'".upper(), said.upper(), (token, warnings))
                        continue
                    for start in range(len(token) - run + 1):
                        self.assertNotIn(token[start : start + run], said, (token, warnings))
                    self.assertNotIn(token.upper()[:run], said.upper(), (token, warnings))

    def test_an_id_its_own_token_begins_with_is_still_named(self) -> None:
        # An operator id of API_BEARER_TOKENS is no secret, though ten characters of a token are that id: a slip of
        # case in it is still hinted, and the list pasted whole is still named by its ids.
        tokens = f"alice-prod:alice-prod-{self.A}"

        [slip] = self._warnings("Alice-Prod", tokens=tokens)
        [pasted] = self._warnings(tokens, tokens=tokens)

        self.assertIn("lists 'Alice-Prod'", slip)
        self.assertIn("It differs from 'alice-prod' only by case", slip)
        self.assertIn("begins 'alice-prod:'", pasted)
        # Nor does that id hide another: what such a token has in common with its own operator's id is no secret,
        # and an id that holds eight characters of it was counted as a token and not named.
        said = [slip, pasted]
        for near in ("alice-prod2", "alice-production", "malice-prod"):
            with self.subTest(near=near):
                [warning] = self._warnings(near, tokens=tokens)

                self.assertIn(f"lists '{near}', which no credential", warning)
                said.append(warning)
        # Eight in a row that go on past the id are the token's own.
        [goes_on] = self._warnings(f"alice-prod-{self.A[:1]}", tokens=tokens)
        self.assertIn(kept_back(1), goes_on)
        for warning in [*said, goes_on]:
            for start in range(len(self.A) - 7):
                self.assertNotIn(self.A[start : start + 8], warning)


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
