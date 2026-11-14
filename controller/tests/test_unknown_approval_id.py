"""A tool call that presents an approval id no record answers to is told so.

It was answered with the id alone, the text of the store's KeyError: nothing said the approval was unknown, or what
to do next. The retention deletes decided approvals (APPROVAL_RETENTION_DAYS), so an agent that kept an old id met
this in ordinary use.

The answer says what the controller knows, and no more: it holds no approval of that id that the call can use. That
is as true of one that was deleted, of a record that no longer validates and of another operator's approval as of an
id that never was, and all of them read alike. It names the argument the id came in, since a call can present one in
two (approval_id, governed_approval_id) and the advice is to leave that argument out.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock

from app.models import OperatorIdentity
from app.session_ownership import acting_as
from tests.test_approval_spend import SESSION, SpendBase

UNKNOWN = "0123456789ab"
OTHER_UNKNOWN = "ba9876543210"
BROKEN_LINE = "0123\n456789ab"
COOKIE = {"name": "sid", "value": "v", "url": "https://example.com/"}
HELD = "this controller holds no approval of that id that this call can use."
MAYBE = "It may never have existed, or may have been deleted, as a decided approval can be once"
REPEAT = "Repeat the call without approval_id to ask for a new approval."
REPEAT_GOVERNED = "Repeat the call without governed_approval_id to ask for a new approval."
ASK_AGAIN = "To get a new approval, repeat the call that needed one without this approval id."
DECISION_TOOLS = ("browser.execute_approval", "browser.reject_approval", "browser.approve_approval")
GOVERNED_COOKIES = {"session_id": SESSION, "workflow_profile": "governed", "cookies": [COOKIE]}
ALICE = OperatorIdentity(id="alice", source="token")
BOB = OperatorIdentity(id="bob", source="token")
ANOTHER_SESSION = "session-2"
CLICK = {"action": "click", "reason": "submit payment", "selector": "#pay", "risk_category": "payment"}


def unknown(approval_id: str = UNKNOWN, presented_as: str = "approval_id") -> str:
    """How the answer begins: the id as the store was asked for it, the argument it came in, what is known of it."""
    return f"Unknown approval {approval_id!r} (presented as {presented_as}): {HELD}"


class UnknownApprovalIdTests(SpendBase):
    async def _written(self) -> tuple[int, int, int]:
        approvals = len(await self.manager.approvals.list())
        audit = len(await self.manager.audit.list(limit=1000))
        receipts = len(await self.manager.list_witness_receipts(SESSION))
        return approvals, audit, receipts

    async def _assert_told(self, name: str, what_next: str, **arguments) -> str:
        before = await self._written()

        answer = await self.call(name, approval_id=UNKNOWN, **arguments)

        text = answer.content[0].text
        self.assertTrue(answer.isError)
        self.assertTrue(text.startswith(f"{unknown()} {MAYBE} APPROVAL_RETENTION_DAYS (30) have passed"), text)
        self.assertTrue(text.endswith(what_next), text)
        self.assertEqual(answer.structuredContent, {"error": text})
        self.assertEqual(await self._written(), before, "a call with an unknown approval id wrote something")
        return text

    async def test_a_governed_call_that_presents_an_unknown_id(self) -> None:
        await self._assert_told("browser.set_cookies", REPEAT, **GOVERNED_COOKIES)

    async def test_executing_an_unknown_id(self) -> None:
        await self._assert_told("browser.execute_approval", ASK_AGAIN)

    async def test_rejecting_an_unknown_id(self) -> None:
        await self._assert_told("browser.reject_approval", ASK_AGAIN)

    async def test_approving_an_unknown_id_where_agents_may_approve(self) -> None:
        self.settings.autonomous_approvals = True

        await self._assert_told("browser.approve_approval", ASK_AGAIN)

    async def test_an_id_presented_as_governed_approval_id_is_told_to_leave_that_argument_out(self) -> None:
        # The advice named approval_id whichever argument held the id: followed, it left the unknown id in the call.
        answer = await self.call("browser.set_cookies", **GOVERNED_COOKIES, governed_approval_id=UNKNOWN)

        text = answer.content[0].text
        self.assertTrue(answer.isError)
        self.assertTrue(text.startswith(unknown(presented_as="governed_approval_id")), text)
        self.assertTrue(text.endswith(REPEAT_GOVERNED), text)

    async def test_of_two_ids_the_one_the_call_went_by_is_the_one_named(self) -> None:
        answer = await self.call(
            "browser.set_cookies", **GOVERNED_COOKIES, approval_id=OTHER_UNKNOWN, governed_approval_id=UNKNOWN
        )

        text = answer.content[0].text
        self.assertTrue(text.startswith(unknown(presented_as="governed_approval_id")), text)
        self.assertTrue(text.endswith(REPEAT_GOVERNED), text)
        self.assertNotIn(OTHER_UNKNOWN, text)

    async def test_an_id_presented_in_both_arguments_names_both(self) -> None:
        answer = await self.call(
            "browser.set_cookies", **GOVERNED_COOKIES, approval_id=UNKNOWN, governed_approval_id=UNKNOWN
        )

        text = answer.content[0].text
        self.assertTrue(text.startswith(unknown(presented_as="approval_id and governed_approval_id")), text)
        self.assertTrue(
            text.endswith("Repeat the call without approval_id and governed_approval_id to ask for a new approval."),
            text,
        )

    async def test_an_id_of_60000_characters_is_named_cut(self) -> None:
        # governed_approval_id is read before any model bounds it, and the answer named the id by its repr, whole: it
        # came back in the text and again in the structured error.
        long_id = "0" * 60_000

        answer = await self.call("browser.set_cookies", **GOVERNED_COOKIES, governed_approval_id=long_id)

        text = answer.content[0].text
        self.assertTrue(answer.isError)
        named = f"Unknown approval {long_id[:200]!r}... (presented as governed_approval_id): {HELD}"
        self.assertTrue(text.startswith(named), text[:400])
        self.assertLess(len(text), 1000)
        self.assertEqual(answer.structuredContent, {"error": text})

    async def test_white_space_round_the_id_of_a_decision_is_still_told(self) -> None:
        # These three tools strip their strings, so the store is asked for the stripped id and its KeyError names
        # that one: compared with the id as it was sent, a pasted id with a line break after it got the bare id back.
        self.settings.autonomous_approvals = True
        for name in DECISION_TOOLS:
            for sent in (f"{UNKNOWN}\n", f" {UNKNOWN}", f"{UNKNOWN} ", f"\t{UNKNOWN}\r\n"):
                with self.subTest(name, sent=sent):
                    answer = await self.call(name, approval_id=sent)

                    text = answer.content[0].text
                    self.assertTrue(answer.isError)
                    self.assertTrue(text.startswith(unknown()), text)
                    self.assertTrue(text.endswith(ASK_AGAIN), text)

    async def test_a_line_break_in_the_id_begins_no_line_of_the_answer(self) -> None:
        # UNKNOWN's repr is itself, so an answer that wrote the id as it came would pass every other test here.
        self.settings.autonomous_approvals = True
        calls = [(name, {"approval_id": BROKEN_LINE}, "approval_id") for name in DECISION_TOOLS]
        calls += [
            ("browser.set_cookies", {**GOVERNED_COOKIES, "approval_id": BROKEN_LINE}, "approval_id"),
            ("browser.set_cookies", {**GOVERNED_COOKIES, "approval_id": f"{UNKNOWN}\n"}, "approval_id"),
            ("browser.set_cookies", {**GOVERNED_COOKIES, "governed_approval_id": BROKEN_LINE}, "governed_approval_id"),
        ]
        for name, arguments, presented_as in calls:
            with self.subTest(name, arguments=arguments):
                answer = await self.call(name, **arguments)

                text = answer.content[0].text
                self.assertTrue(answer.isError)
                self.assertNotIn("\n", text)
                self.assertTrue(text.startswith(unknown(arguments[presented_as], presented_as)), text)

    async def test_with_the_retention_off_no_reason_is_guessed(self) -> None:
        # "It never existed" was said here: of an approval the retention deleted before it was turned off, too.
        self.settings.approval_retention_days = 0

        answer = await self.call("browser.execute_approval", approval_id=UNKNOWN)

        self.assertEqual(answer.content[0].text, f"{unknown()} {ASK_AGAIN}")

    def _a_second_session(self, owner: str) -> None:
        second = dataclasses.replace(self.session, id=ANOTHER_SESSION, name=ANOTHER_SESSION, owner=owner)
        self.manager.sessions[ANOTHER_SESSION] = second

    async def test_an_approval_of_another_operator_reads_as_an_unknown_one(self) -> None:
        # Two operators, a session each. Alice asks for two approvals in hers and has one of them approved; bob then
        # presents each id wherever a tool takes one. He is told, word for word, what an id no record answers to is
        # told. A governed call in his own session answered "approval <id> does not belong to session session-2":
        # that the id is an approval's, which the listing and the three decisions keep from him.
        self.settings.autonomous_approvals = True
        self.session.owner = "alice"
        self._a_second_session("bob")
        with acting_as(ALICE):
            asked = await self.call("browser.execute_action", session_id=SESSION, action={**CLICK, "selector": "#p2"})
            pending = asked.structuredContent["approval_id"]
            _decision, approved = await self.approved_click()
        in_his_own = {"session_id": ANOTHER_SESSION, "workflow_profile": "governed", "cookies": [COOKIE]}
        calls = [(name, {}, "approval_id") for name in DECISION_TOOLS]
        calls += [
            ("browser.set_cookies", in_his_own, "approval_id"),
            ("browser.set_cookies", in_his_own, "governed_approval_id"),
            ("browser.execute_action", {"session_id": ANOTHER_SESSION, "action": CLICK}, "approval_id"),
        ]

        with acting_as(BOB):
            self.assertEqual((await self.call("browser.list_approvals")).structuredContent, [])
            for name, arguments, presented_as in calls:
                for state, approval_id in (("pending", pending), ("approved", approved)):
                    with self.subTest(name, presented_as=presented_as, alices=state):
                        theirs = await self.call(name, **arguments, **{presented_as: approval_id})
                        unknown_one = await self.call(name, **arguments, **{presented_as: UNKNOWN})

                        told, told_of_no_record = theirs.content[0].text, unknown_one.content[0].text
                        self.assertTrue(theirs.isError)
                        self.assertEqual(told.replace(approval_id, UNKNOWN), told_of_no_record)
                        self.assertTrue(told_of_no_record.startswith(unknown(presented_as=presented_as)))

        with acting_as(ALICE):
            self.assertEqual((await self.status(pending), await self.status(approved)), ("pending", "approved"))

    async def test_the_callers_own_approval_presented_in_another_of_their_sessions_is_told_whose_it_is(self) -> None:
        # Only a caller who may not use the approval is told nothing of it.
        self.session.owner = "alice"
        self._a_second_session("alice")
        with acting_as(ALICE):
            _decision, approval_id = await self.approved_click()

            answer = await self.call(
                "browser.execute_action", session_id=ANOTHER_SESSION, action=CLICK, approval_id=approval_id
            )

        self.assertTrue(answer.isError)
        self.assertEqual(answer.content[0].text, f"approval {approval_id} does not belong to session {ANOTHER_SESSION}")

    async def test_another_operators_approval_in_a_session_nobody_owns_is_told_whose_it_is(self) -> None:
        # Who may use an approval is the manager's rule, the session's owner, which the store asks (may_use): of a
        # session nobody owns, anybody, and bob lists alice's approval there. The store's own rule, the operator the
        # record names, would tell him an approval he can list is unknown. In the two-operator test above the two
        # rules agree, as the session's owner is the record's.
        self.session.owner = None
        self._a_second_session("bob")
        with acting_as(ALICE):
            _decision, approval_id = await self.approved_click()

        with acting_as(BOB):
            listed = (await self.call("browser.list_approvals")).structuredContent
            answer = await self.call(
                "browser.execute_action", session_id=ANOTHER_SESSION, action=CLICK, approval_id=approval_id
            )

        self.assertEqual((await self.manager.approvals.get(approval_id)).owner, "alice")
        self.assertIn(approval_id, [approval["id"] for approval in listed])
        self.assertTrue(answer.isError)
        self.assertEqual(answer.content[0].text, f"approval {approval_id} does not belong to session {ANOTHER_SESSION}")

    async def test_another_missing_thing_keeps_its_own_words(self) -> None:
        # Only a KeyError that names the presented id is the approval's.
        self.manager.execute_approval = AsyncMock(  # type: ignore[method-assign]
            side_effect=KeyError("Memory profile not found")
        )

        answer = await self.call("browser.execute_approval", approval_id=UNKNOWN)

        self.assertEqual(answer.content[0].text, "Memory profile not found")
