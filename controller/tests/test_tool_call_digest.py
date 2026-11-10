"""A governed tool call's approval keeps its arguments' digest keyed, so a credential cannot be guessed against it.

The gateway's stand-in names the call by the sha256 of its whole arguments, credentials included (a cookie's value,
a password in a URL), and the approval is stored, listed by GET /approvals and the approval tools, and handed back
to the agent, while the preview beside it shows every field but the credential: with the rest known, a short secret
could be tried offline until a guess hashed to the stored text. The store now keeps the digest as an HMAC under a
key of its own, kept beside the records so the same call still matches its approval after a restart, and names
that key by an id, so an approval kept under a key that is gone is refused with why.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from app.approval_backends import DIGEST_KEY_FILE, FileApprovalStore
from app.approval_display import approval_summary, approval_tool
from app.approvals import ApprovalStore, ServerStateRefusal
from app.models import ApprovalRecord, BrowserActionDecision
from app.routes.extensions import _DASHBOARD_HTML
from app.tool_gateway.gateway import McpToolGateway
from tests.test_approval_provenance import SESSION, ApprovalBindingBase

SECRET = "s3cret"
GUESSES = ("letmein", "hunter2", SECRET, "password")
KEYED = r"^browser\.set_cookies hmac-sha256:[0-9a-f]{8}:[0-9a-f]{64}$"
ANOTHER_KEY = "kept under another key"
BACKENDS = ("file", "sqlite")


def _plain(arguments: str, tool: str = "browser.set_cookies") -> str:
    """A stand-in's text as the gateway writes it, and as versions before 1.18.0 stored it."""
    return f"{tool} sha256:{hashlib.sha256(arguments.encode('utf-8')).hexdigest()}"


def _stand_in(text: str) -> BrowserActionDecision:
    return BrowserActionDecision(action="request_human_takeover", reason="r", risk_category="account_change", text=text)


def _digest(stand_in_text: str) -> str:
    return stand_in_text.rsplit(":", 1)[1]


def _key_id(keyed_text: str) -> str:
    return keyed_text.split(":")[1]


def _cookies(value: str) -> list[dict[str, Any]]:
    return [{"name": "sid", "value": value, "url": "https://example.com"}]


def _files_holding(needle: str, *roots: Path) -> list[str]:
    return sorted(
        str(path)
        for root in roots
        for path in root.rglob("*")
        if path.is_file() and needle.encode("ascii") in path.read_bytes()
    )


class ToolCallDigestTests(ApprovalBindingBase):
    """Through the gateway, over a real manager and store."""

    async def _asked(self, value: str = SECRET) -> str:
        ask = await self.call(
            "browser.set_cookies", session_id=SESSION, workflow_profile="governed", cookies=_cookies(value)
        )
        return ask.structuredContent["approval_id"]

    def _gateway_stand_in(self, tool: str, **arguments: Any) -> str:
        """What a guesser computes: the gateway's own stand-in text for the call."""
        spec = self.gateway._registry.get(tool)
        stand_in, _ = McpToolGateway._governed_call_decision(spec, spec.input_model.model_validate(arguments))
        return stand_in.text

    def _plain_stand_in(self, value: str) -> str:
        return self._gateway_stand_in("browser.set_cookies", session_id=SESSION, cookies=_cookies(value))

    async def _spend(self, value: str, approval_id: str):
        return await self.call(
            "browser.set_cookies",
            session_id=SESSION,
            workflow_profile="governed",
            cookies=_cookies(value),
            approval_id=approval_id,
        )

    async def _store_plain(self, approval_id: str) -> None:
        """Write the record back with the plain digest an earlier version stored."""
        store = self.manager.approvals
        record = await store.get(approval_id)
        record.action = record.action.model_copy(update={"text": self._plain_stand_in(SECRET)})
        await store.file_store.upsert(record)

    async def test_the_stored_digest_cannot_be_recomputed_from_the_arguments(self) -> None:
        self.manager.settings.approval_webhook_url = "https://hooks.example/approvals"
        with patch("app.browser.services.actions.dispatch_approval_event", AsyncMock()) as webhook:
            ask = await self.call(
                "browser.set_cookies", session_id=SESSION, workflow_profile="governed", cookies=_cookies(SECRET)
            )
        approval_id = ask.structuredContent["approval_id"]
        record = await self.manager.approvals.get(approval_id)
        stored = record.action.text

        self.assertEqual([value for value in GUESSES if self._plain_stand_in(value) == stored], [])
        self.assertRegex(stored, KEYED)
        # What is listed, answered to the agent and handed to the webhook is the record as stored.
        self.assertEqual([item["action"]["text"] for item in await self.manager.list_approvals()], [stored])
        self.assertEqual(ask.structuredContent["approval"]["action"]["text"], stored)
        self.assertEqual(webhook.call_args.args[0].action.text, stored)
        self.assertNotIn(_digest(self._plain_stand_in(SECRET)), json.dumps(ask.model_dump(mode="json")))
        # Read by the server alone, the tool is still named.
        self.assertEqual(approval_tool(record), "browser.set_cookies")
        self.assertEqual(approval_summary(record), "tool call browser.set_cookies")
        # Nor is the gateway's text held in memory as a sensitive action's is.
        self.assertEqual(self.manager.approvals._sensitive_texts, {})

    async def test_nothing_but_the_keyed_digest_tells_two_secret_values_apart(self) -> None:
        # A second digest, a size or a preview that followed the value would be something to guess against too.
        # Two values of one length: a storage value's length is shown, by design (previews._length_only).
        calls = {
            "browser.set_cookies": lambda value: {"cookies": _cookies(value)},
            "browser.set_local_storage": lambda value: {"key": "token", "value": value},
            "browser.cdp_attach": lambda value: {"cdp_url": f"http://operator:{value}@127.0.0.1:9222/?token={value}"},
        }
        for tool, arguments in calls.items():
            with self.subTest(tool=tool):
                scope = {} if tool == "browser.cdp_attach" else {"session_id": SESSION}
                records, kept = [], []
                for value in ("s3cret", "hunter"):
                    ask = await self.call(tool, workflow_profile="governed", **scope, **arguments(value))
                    record = await self.manager.approvals.get(ask.structuredContent["approval_id"])
                    kept.append(await self.manager.approvals.arguments(record.id))
                    records.append(record)
                first, second = (
                    record.model_dump(exclude={"id": True, "created_at": True, "updated_at": True, "action": {"text"}})
                    for record in records
                )

                self.assertNotEqual(records[0].action.text, records[1].action.text)
                self.assertEqual(first, second)
                self.assertEqual(kept[0], kept[1])
                self.assertIsNotNone(records[0].arguments_sha256)

    async def test_no_file_holds_the_plain_digest_of_a_call_asked_approved_and_run(self) -> None:
        value = os.urandom(8).hex()  # this test's own, so the shared witness root holds no other test's
        cdp_url = f"http://operator:{value}@127.0.0.1:9222"
        approval_id = await self._asked(value)
        await self.manager.approve(approval_id)
        spend = await self._spend(value, approval_id)
        # In controller scope the request is audited instead of witnessed.
        unscoped = await self.call("browser.cdp_attach", workflow_profile="governed", cdp_url=cdp_url)

        self.assertFalse(spend.isError, spend.content[0].text)
        self.assertEqual(unscoped.structuredContent.get("status"), "approval_required", unscoped.structuredContent)
        roots = (Path(self.tempdir.name), Path(self.manager.settings.witness_root))
        for plain in (self._plain_stand_in(value), self._gateway_stand_in("browser.cdp_attach", cdp_url=cdp_url)):
            with self.subTest(tool=plain.split(" ", 1)[0]):
                self.assertEqual(_files_holding(_digest(plain), *roots), [])
        self.assertNotEqual(_files_holding(approval_id, *roots), [])  # the scan reads where the records are

    async def test_the_same_call_spends_it_after_a_restart_and_another_cookie_does_not(self) -> None:
        approval_id = await self._asked()
        await self.manager.approve(approval_id)
        restarted = ApprovalStore(self.manager.settings.approval_root, audit=self.manager.audit)
        await restarted.startup()
        self.manager.approvals = restarted

        other = await self._spend("another", approval_id)
        spend = await self._spend(SECRET, approval_id)

        self.assertTrue(other.isError)
        self.assertIn("does not match the requested action", other.content[0].text)
        self.assertFalse(spend.isError, spend.content[0].text)
        self.assertEqual(self.context.cookies, _cookies(SECRET))

    async def test_an_approval_stored_plain_by_an_earlier_version_still_matches_its_call(self) -> None:
        # Chosen over refusing it: the same call is all it can cover. A start rewrites it (StoredDigestTests); this
        # is the record a start could not rewrite, or one an older process wrote since.
        approval_id = await self._asked()
        await self.manager.approve(approval_id)
        await self._store_plain(approval_id)
        self.assertEqual(approval_tool(await self.manager.approvals.get(approval_id)), "browser.set_cookies")

        other = await self._spend("another", approval_id)
        spend = await self._spend(SECRET, approval_id)

        self.assertTrue(other.isError)
        self.assertIn("does not match the requested action", other.content[0].text)
        self.assertFalse(spend.isError, spend.content[0].text)
        self.assertEqual(self.context.cookies, _cookies(SECRET))

    async def test_a_pending_approval_stored_plain_is_asked_for_again_not_twice(self) -> None:
        approval_id = await self._asked()
        await self._store_plain(approval_id)

        self.assertEqual(await self._asked(), approval_id)
        self.assertEqual(len(await self.manager.approvals.list()), 1)

    async def test_an_operators_rejection_stored_plain_still_refuses_the_agents_own_approval(self) -> None:
        self.manager.settings.autonomous_approvals = True
        rejected = await self._asked()
        await self.manager.reject(rejected, comment="no")
        await self._store_plain(rejected)
        again = await self._asked()

        approved = await self.call("browser.approve_approval", approval_id=again)

        self.assertNotEqual(again, rejected)
        self.assertTrue(approved.isError)
        self.assertIn("an operator rejected this action", approved.content[0].text)

    async def test_an_operators_rejection_refuses_the_agents_approval_of_a_pending_one_stored_plain(self) -> None:
        # The other way round: the rejection is kept keyed, and the pending approval is the record still plain.
        self.manager.settings.autonomous_approvals = True
        rejected = await self._asked()
        await self.manager.reject(rejected, comment="no")
        again = await self._asked()
        await self._store_plain(again)

        approved = await self.call("browser.approve_approval", approval_id=again)

        self.assertTrue(approved.isError)
        self.assertIn("an operator rejected this action", approved.content[0].text)

    async def test_an_approval_kept_under_a_key_that_is_gone_tells_the_agent_to_ask_again(self) -> None:
        approval_id = await self._asked()
        await self.manager.approve(approval_id)
        (Path(self.manager.settings.approval_root) / DIGEST_KEY_FILE).unlink()
        restarted = ApprovalStore(self.manager.settings.approval_root, audit=self.manager.audit)
        with self.assertLogs("app.approvals", level="WARNING"):
            await restarted.startup()
        self.manager.approvals = restarted

        spend = await self._spend(SECRET, approval_id)
        again = await self._asked()

        self.assertTrue(spend.isError)
        self.assertIn(ANOTHER_KEY, spend.content[0].text)
        self.assertIn("repeat the call without approval_id", spend.content[0].text)
        self.assertEqual(self.context.cookies, [])
        self.assertNotEqual(again, approval_id)

    async def test_an_operators_rejection_kept_under_a_key_that_is_gone_no_longer_refuses_the_agent(self) -> None:
        # What a lost key costs, as the docs and the start's warning say: the rejection matches no call any more.
        self.manager.settings.autonomous_approvals = True
        rejected = await self._asked()
        await self.manager.reject(rejected, comment="no")
        (Path(self.manager.settings.approval_root) / DIGEST_KEY_FILE).unlink()
        restarted = ApprovalStore(self.manager.settings.approval_root, audit=self.manager.audit)
        with self.assertLogs("app.approvals", level="WARNING") as logged:
            await restarted.startup()
        self.manager.approvals = restarted
        again = await self._asked()

        approved = await self.call("browser.approve_approval", approval_id=again)

        self.assertNotEqual(again, rejected)
        self.assertFalse(approved.isError, approved.content[0].text)
        self.assertEqual((await restarted.get(again)).status, "approved")
        # The start said so, though nothing pending or approved is kept under the lost key.
        [warning] = logged.output
        self.assertIn("1 operator rejection(s) of tool calls", warning)
        self.assertIn("no longer stop an agent's own approval of the same call", warning)

    def test_the_dashboard_reads_a_stored_stand_in_as_its_tool(self) -> None:
        js_pattern = re.search(r"&& /(.+?)/\.exec", _DASHBOARD_HTML).group(1)
        digest = hashlib.sha256(b"x").hexdigest()
        keyed = f"browser.set_cookies hmac-sha256:0a1b2c3d:{digest}"
        plain = f"browser.set_cookies sha256:{digest}"

        for text in (keyed, plain):
            with self.subTest(text=text):
                self.assertEqual(re.match(js_pattern, text).group(1), "browser.set_cookies")
        for text in (f"browser.set_cookies hmac-sha256:{digest}", f"browser.set_cookies md5:0a1b2c3d:{digest}"):
            with self.subTest(text=text):
                self.assertIsNone(re.match(js_pattern, text))


class StoredDigestTests(unittest.IsolatedAsyncioTestCase):
    """The store itself, on the file store alone and with SQLite primary beside it."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.base = Path(self.tempdir.name)

    def _paths(self, backend: str) -> tuple[Path, Path | None]:
        return self.base / backend / "approvals", (self.base / backend / "state.db" if backend == "sqlite" else None)

    async def _store(self, backend: str = "file", *, started: bool = True) -> ApprovalStore:
        root, db_path = self._paths(backend)
        store = ApprovalStore(root, db_path=str(db_path) if db_path else None)
        if started:
            await store.startup()
        return store

    def _raw(self, backend: str) -> str:
        """Everything either store holds of the records, as it is on disk."""
        root, db_path = self._paths(backend)
        text = "".join(path.read_text(encoding="utf-8") for path in sorted(root.glob("*.json")))
        if db_path is not None:
            with closing(sqlite3.connect(db_path)) as conn:
                text += "".join(row[0] for row in conn.execute("SELECT payload FROM approvals"))
        return text

    @staticmethod
    async def _asked(store: ApprovalStore, plain: str, *, origin: str = "tool_call") -> ApprovalRecord:
        return await store.create_or_reuse_pending(
            session_id=SESSION, kind="account_change", reason="r", action=_stand_in(plain), origin=origin
        )

    @staticmethod
    async def _spendable(store: ApprovalStore, approval_id: str, plain: str) -> ApprovalRecord:
        return await store.require_approved(
            approval_id=approval_id,
            session_id=SESSION,
            kind="account_change",
            action=_stand_in(plain),
            origin="tool_call",
        )

    @staticmethod
    async def _as_stored_before(store: ApprovalStore, approval_id: str, plain: str, **changes: Any) -> ApprovalRecord:
        """Write the record back as 1.17.0 stored it, in every store configured: the stand-in's plain digest."""
        record = await store.get(approval_id)
        record = record.model_copy(update={"action": record.action.model_copy(update={"text": plain}), **changes})
        await store.file_store.upsert(record)
        if store.sqlite_store is not None:
            await store.sqlite_store.upsert(record)
        return record

    async def test_both_stores_keep_the_digest_keyed_under_the_key_beside_the_records(self) -> None:
        plain = _plain("a")
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                store = await self._store(backend)
                approval = await self._asked(store, plain)
                await store.approve(approval.id)
                root, _ = self._paths(backend)

                stored = approval.action.text
                self.assertRegex(stored, KEYED)
                self.assertEqual({record.action.text for record in await store.list()}, {stored})
                self.assertNotIn(_digest(plain), self._raw(backend))
                self.assertIn(stored, self._raw(backend))
                # With SQLite too the key is the file store's: APPROVAL_ROOT holds it, and the arguments.
                self.assertRegex((root / DIGEST_KEY_FILE).read_text(encoding="ascii"), r"^[0-9a-f]{64}$")

                restarted = await self._store(backend)

                self.assertEqual((await self._spendable(restarted, approval.id, plain)).id, approval.id)
                with self.assertRaisesRegex(PermissionError, "does not match the requested action"):
                    await self._spendable(restarted, approval.id, _plain("b"))
                # What is asked after the restart is kept under the same key.
                self.assertEqual(_key_id((await self._asked(restarted, _plain("c"))).action.text), _key_id(stored))

    async def test_the_digest_follows_the_key(self) -> None:
        plain = _plain("a")
        first = await self._store("file")
        second = await self._store("sqlite")  # another root, so another key
        third_root = self.base / "third"
        third_root.mkdir()
        shutil.copy(self._paths("file")[0] / DIGEST_KEY_FILE, third_root / DIGEST_KEY_FILE)
        third = ApprovalStore(third_root)
        await third.startup()

        ours, theirs = (store._stored_action(_stand_in(plain)).text for store in (first, second))

        self.assertNotEqual(_digest(ours), _digest(theirs))
        self.assertNotEqual(_key_id(ours), _key_id(theirs))
        # Under the same key the same call reads the same, whichever process keyed it; a key's id is no part of it.
        self.assertEqual(third._stored_action(_stand_in(plain)).text, ours)
        self.assertNotIn(_key_id(ours), (self._paths("file")[0] / DIGEST_KEY_FILE).read_text(encoding="ascii"))

    async def test_a_start_rewrites_what_an_earlier_version_stored_plain(self) -> None:
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                store = await self._store(backend)
                before = {}
                for name in ("pending", "approved", "rejected", "legacy"):
                    approval = await self._asked(store, _plain(name))
                    if name == "approved":
                        await store.approve(approval.id)
                    if name == "rejected":
                        await store.reject(approval.id, comment="no")
                    # One stored before approvals recorded their origin loads as an action approval.
                    changes = {"origin": "action"} if name == "legacy" else {}
                    before[name] = await self._as_stored_before(store, approval.id, _plain(name), **changes)
                await self._asked(store, _plain("kept"))  # already keyed: left as it is
                self.assertIn(_digest(_plain("approved")), self._raw(backend))

                restarted = await self._store(backend, started=False)
                with self.assertLogs("app.approvals", level="INFO") as logged:
                    await restarted.startup()

                self.assertIn("4 approval record(s)", "\n".join(logged.output))
                for name, old in before.items():
                    self.assertNotIn(_digest(_plain(name)), self._raw(backend), name)
                    new = await restarted.get(old.id)
                    self.assertRegex(new.action.text, KEYED, name)
                    # Nothing but the text: its status, its times and who decided it stay.
                    self.assertEqual(new.model_copy(update={"action": old.action}), old, name)
                    self.assertEqual(await restarted.file_store.get(old.id), new, name)
                    if restarted.sqlite_store is not None:
                        self.assertEqual(await restarted.sqlite_store.get(old.id), new, name)
                approved = before["approved"].id
                self.assertEqual((await self._spendable(restarted, approved, _plain("approved"))).id, approved)
                with self.assertRaisesRegex(PermissionError, "does not match the requested action"):
                    await self._spendable(restarted, approved, _plain("pending"))
                # The pending one is still the one its call asks for.
                self.assertEqual((await self._asked(restarted, _plain("pending"))).id, before["pending"].id)

                with self.assertNoLogs("app.approvals", level="INFO"):
                    await (await self._store(backend, started=False)).startup()

    async def test_a_record_that_cannot_be_rewritten_is_logged_still_matches_and_is_taken_at_the_next_start(
        self,
    ) -> None:
        store = await self._store()
        approval = await self._asked(store, _plain("a"))
        await store.approve(approval.id)
        await self._as_stored_before(store, approval.id, _plain("a"))
        restarted = await self._store(started=False)

        with (
            patch.object(FileApprovalStore, "_write_sync", side_effect=OSError(28, "No space left on device")),
            self.assertLogs("app.approvals", level="WARNING") as logged,
        ):
            await restarted.startup()

        self.assertIn("1 approval record(s)", "\n".join(logged.output))
        self.assertIn(approval.id, "\n".join(logged.output))
        self.assertEqual((await restarted.get(approval.id)).action.text, _plain("a"))
        self.assertEqual((await self._spendable(restarted, approval.id, _plain("a"))).id, approval.id)

        later = await self._store()

        self.assertRegex((await later.get(approval.id)).action.text, KEYED)

    async def test_a_listing_that_fails_does_not_stop_the_start(self) -> None:
        store = await self._store(started=False)

        with (
            patch.object(FileApprovalStore, "_list_sync", side_effect=OSError(5, "Input/output error")),
            self.assertLogs("app.approvals", level="ERROR") as logged,
        ):
            await store.startup()

        self.assertIn("tool call digests", "\n".join(logged.output))

    async def test_with_sqlite_down_the_file_is_rewritten_and_sqlite_catches_up_at_its_next_start(self) -> None:
        store = await self._store("sqlite")
        approval = await self._asked(store, _plain("a"))
        await self._as_stored_before(store, approval.id, _plain("a"))
        down = await self._store("sqlite", started=False)
        down.sqlite_store.startup = AsyncMock(side_effect=RuntimeError("database is locked"))

        with self.assertLogs("app.approvals", level="WARNING"):
            await down.startup()

        self.assertIs(down._primary, down.file_store)
        self.assertRegex((await down.file_store.get(approval.id)).action.text, KEYED)
        self.assertEqual((await store.sqlite_store.get(approval.id)).action.text, _plain("a"))

        back = await self._store("sqlite")

        self.assertIs(back._primary, back.sqlite_store)
        self.assertEqual(await back.sqlite_store.get(approval.id), await down.file_store.get(approval.id))
        self.assertNotIn(_digest(_plain("a")), self._raw("sqlite"))

    async def test_an_approval_kept_under_a_key_that_is_gone_is_refused_with_why(self) -> None:
        store = await self._store()
        root, _ = self._paths("file")
        pending = await self._asked(store, _plain("pending"))
        approved = await self._asked(store, _plain("approved"))
        await store.approve(approved.id)
        rejected = await self._asked(store, _plain("rejected"))
        await store.reject(rejected.id, comment="no")
        old_key = (root / DIGEST_KEY_FILE).read_text(encoding="ascii")
        (root / DIGEST_KEY_FILE).unlink()
        # The process that read the key goes on with it: a key removed or replaced counts from the next start.
        self.assertEqual((await self._spendable(store, approved.id, _plain("approved"))).id, approved.id)

        restarted = await self._store(started=False)
        with self.assertLogs("app.approvals", level="WARNING") as logged:
            await restarted.startup()

        warning = "\n".join(logged.output)
        self.assertIn("2 pending or approved", warning)
        self.assertIn("1 operator rejection(s) of tool calls", warning)
        self.assertIn(DIGEST_KEY_FILE, warning)
        self.assertNotEqual((root / DIGEST_KEY_FILE).read_text(encoding="ascii"), old_key)
        with self.assertRaisesRegex(PermissionError, ANOTHER_KEY) as spend:
            await self._spendable(restarted, approved.id, _plain("approved"))
        self.assertNotIn("does not match", str(spend.exception))
        with self.assertRaisesRegex(ServerStateRefusal, f"approval {pending.id}: .*{ANOTHER_KEY}.*asks again"):
            await restarted.approve(pending.id)
        self.assertEqual((await restarted.get(pending.id)).status, "pending")
        # Asked again, the call gets a new approval, which this key reads; the old one can still be rejected.
        again = await self._asked(restarted, _plain("pending"))
        self.assertNotEqual(again.id, pending.id)
        await restarted.approve(again.id)
        self.assertEqual((await self._spendable(restarted, again.id, _plain("pending"))).id, again.id)
        self.assertEqual((await restarted.reject(pending.id)).status, "rejected")

        with self.assertLogs("app.approvals", level="WARNING") as logged:
            await (await self._store(started=False)).startup()
        self.assertIn("1 pending or approved", "\n".join(logged.output))
        # The one rejected to end that warning is an operator's rejection kept under the lost key too.
        self.assertIn("2 operator rejection(s) of tool calls", "\n".join(logged.output))

    async def test_a_start_counts_the_operators_rejections_kept_under_a_key_that_is_gone(self) -> None:
        # Nothing said so: such a rejection no longer stops an agent's own approval of the call it refused.
        store = await self._store()
        root, _ = self._paths("file")
        by_an_operator = await self._asked(store, _plain("operator"))
        await store.reject(by_an_operator.id, comment="no")
        by_the_agent = await self._asked(store, _plain("agent"))
        await store.reject(by_the_agent.id, decided_via="agent")
        (root / DIGEST_KEY_FILE).unlink()

        restarted = await self._store(started=False)
        with self.assertLogs("app.approvals", level="WARNING") as logged:
            await restarted.startup()

        # One line, and only of the operator's: nothing pending or approved is kept under that key.
        [warning] = logged.output
        self.assertIn("1 operator rejection(s) of tool calls", warning)
        self.assertIn(DIGEST_KEY_FILE, warning)
        self.assertIn("AUTONOMOUS_APPROVALS", warning)
        self.assertNotIn("pending or approved", warning)
        # A rejection kept under the key this start holds is not one of them.
        under_this_key = await self._asked(restarted, _plain("operator"))
        await restarted.reject(under_this_key.id, comment="no")
        with self.assertLogs("app.approvals", level="WARNING") as logged:
            await (await self._store(started=False)).startup()
        self.assertIn("1 operator rejection(s) of tool calls", "\n".join(logged.output))

    async def test_a_start_with_only_pending_or_approved_ones_under_a_key_that_is_gone_names_no_rejection(self) -> None:
        store = await self._store()
        root, _ = self._paths("file")
        await self._asked(store, _plain("pending"))
        (root / DIGEST_KEY_FILE).unlink()

        with self.assertLogs("app.approvals", level="WARNING") as logged:
            await (await self._store(started=False)).startup()

        [warning] = logged.output
        self.assertIn("1 pending or approved", warning)
        self.assertNotIn("rejection", warning)

    async def test_an_unusable_key_file_is_logged_and_each_process_keys_with_its_own(self) -> None:
        root, _ = self._paths("file")
        root.mkdir(parents=True)
        (root / DIGEST_KEY_FILE).write_text("not a key", encoding="ascii")
        plain = _plain("a")

        with patch("app.approval_backends._KEY_BEING_WRITTEN_WAIT_SECONDS", 0.05):
            with self.assertLogs("app.approvals", level="WARNING") as logged:
                store = await self._store()
            warning = "\n".join(logged.output)
            approval = await self._asked(store, plain)
            await store.approve(approval.id)
            # Within the process an approval works as ever.
            self.assertEqual((await self._spendable(store, approval.id, plain)).id, approval.id)
            with self.assertLogs("app.approvals", level="WARNING"):
                restarted = await self._store()

        self.assertIn(DIGEST_KEY_FILE, warning)
        self.assertIn("restart", warning)
        self.assertNotIn("not a key", warning)
        self.assertRegex(approval.action.text, KEYED)
        self.assertEqual((root / DIGEST_KEY_FILE).read_text(encoding="ascii"), "not a key")
        # After a restart nothing holds that process's key: the approval is refused as one under a key that is gone.
        with self.assertRaisesRegex(PermissionError, ANOTHER_KEY):
            await self._spendable(restarted, approval.id, plain)

    async def test_a_store_whose_root_is_missing_keys_with_its_own_and_says_so(self) -> None:
        store = ApprovalStore(self.base / "absent")  # never started: nothing made the root

        with self.assertLogs("app.approvals", level="WARNING") as logged:
            keyed = store._stored_action(_stand_in(_plain("a")))

        self.assertIn(DIGEST_KEY_FILE, "\n".join(logged.output))
        self.assertRegex(keyed.text, KEYED)
        self.assertEqual(store._stored_action(_stand_in(_plain("a"))), keyed)
        self.assertFalse((self.base / "absent").exists())

    async def test_only_a_takeover_whose_text_is_a_stand_in_is_keyed(self) -> None:
        store = await self._store()
        digest = hashlib.sha256(b"a").hexdigest()
        click = BrowserActionDecision(action="click", reason="r", selector="#buy", text=f"browser.x sha256:{digest}")
        for action in (
            click,
            _stand_in(f"see browser.set_cookies sha256:{digest}"),
            _stand_in(f"browser.set_cookies sha256:{digest} and more"),
            _stand_in(f"browser.set_cookies sha256:{digest[:-1]}"),
        ):
            with self.subTest(action=action.action, text=action.text):
                self.assertIs(store._stored_action(action), action)
        # Nor is an action whose text an agent wrote like a digest under another key refused an approve for it.
        typed = BrowserActionDecision(
            action="type", reason="r", selector="#note", text=f"browser.x hmac-sha256:0a1b2c3d:{digest}"
        )
        pending = await store.create_or_reuse_pending(session_id=SESSION, kind="write", reason="r", action=typed)

        self.assertEqual((await store.approve(pending.id)).status, "approved")


class DigestKeyFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.path = self.root / DIGEST_KEY_FILE
        self.store = FileApprovalStore(self.root)
        self.opened: list[tuple[str, int, int]] = []
        real_open = os.open

        def spy(path: Any, flags: int, mode: int = 0o777, **kwargs: Any) -> int:
            self.opened.append((Path(path).name, flags, mode))
            return real_open(path, flags, mode, **kwargs)

        self.open_spy = spy

    def test_the_first_read_writes_a_private_key_and_later_reads_only_read_it(self) -> None:
        with (
            patch("app.approval_backends.os.open", self.open_spy),
            patch("app.approval_backends.os.fsync", wraps=os.fsync) as synced,
        ):
            key = self.store.digest_key()
            again = FileApprovalStore(self.root).digest_key()

        self.assertEqual(len(key), 32)
        self.assertEqual(synced.call_count, 1)  # on disk before any record is kept under it
        self.assertEqual(self.path.read_text(encoding="ascii"), key.hex())
        self.assertEqual(again, key)
        # Asked for 0600, and never replaced: created exclusively, once. A root that holds a key is only read.
        (name, flags, mode), *later = self.opened
        self.assertEqual((name, mode, later), (DIGEST_KEY_FILE, 0o600, []))
        self.assertTrue(flags & os.O_EXCL and flags & os.O_CREAT)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_a_key_written_by_hand_with_a_newline_is_read(self) -> None:
        self.path.write_text("ab" * 32 + "\n", encoding="ascii")

        self.assertEqual(self.store.digest_key(), bytes.fromhex("ab" * 32))

    def test_a_start_that_loses_the_race_to_write_the_key_reads_the_winners(self) -> None:
        # Two starts on one root: both find no key, and O_EXCL lets one of them write it.
        theirs = "cd" * 32

        def open_after_theirs(path: Any, flags: int, mode: int = 0o777, **kwargs: Any) -> int:
            self.path.write_text(theirs, encoding="ascii")
            return self.open_spy(path, flags, mode, **kwargs)

        with patch("app.approval_backends.os.open", open_after_theirs):
            key = self.store.digest_key()

        self.assertEqual(key, bytes.fromhex(theirs))
        self.assertEqual(self.path.read_text(encoding="ascii"), theirs)

    def test_a_key_another_start_is_still_writing_is_waited_for(self) -> None:
        # The file exists from the moment the other start creates it, empty and then in part.
        theirs = "ab" * 32
        stages = iter(["", theirs[:20], theirs])
        real_read_bytes = Path.read_bytes

        def read_bytes(file: Path) -> bytes:
            self.path.write_text(next(stages), encoding="ascii")
            return real_read_bytes(file)

        with (
            patch.object(Path, "read_bytes", read_bytes),
            patch("app.approval_backends._KEY_BEING_WRITTEN_POLL_SECONDS", 0.001),
        ):
            key = self.store.digest_key()

        self.assertEqual(key, bytes.fromhex(theirs))
        self.assertEqual(list(stages), [])

    def test_a_key_file_that_holds_no_key_is_refused_without_its_contents_and_left_as_it_is(self) -> None:
        for held in (b"", b"not a key", b"ab" * 16, ("ab" * 32).encode("utf-16"), b"\xff\xfe" + b"ab" * 31):
            with self.subTest(held=held):
                self.path.write_bytes(held)
                with patch("app.approval_backends._KEY_BEING_WRITTEN_WAIT_SECONDS", 0.05):
                    with self.assertRaises(ValueError) as refused:
                        self.store.digest_key()
                self.assertEqual(self.path.read_bytes(), held)
                # A key in another encoding is still a key: nothing a log line prints of the error may quote it.
                self.assertEqual(str(refused.exception), f"{DIGEST_KEY_FILE} does not hold a 32-byte key in hex")
                self.assertIsNone(refused.exception.__context__)

    def test_a_key_that_cannot_be_written_leaves_no_empty_file_behind(self) -> None:
        # An empty file would read as no key at every later start; gone, the next start writes one.
        real_fdopen = os.fdopen

        class Full:
            def __init__(self, fd: int, *args: Any, **kwargs: Any) -> None:
                self.handle = real_fdopen(fd, *args, **kwargs)

            def __enter__(self) -> Full:
                return self

            def __exit__(self, *exc_info: Any) -> None:
                self.handle.close()

            def write(self, data: bytes) -> int:
                raise OSError(28, "No space left on device")

        with patch("app.approval_backends.os.fdopen", Full):
            with self.assertRaises(OSError):
                self.store.digest_key()

        self.assertFalse(self.path.exists())
        self.assertEqual(len(self.store.digest_key()), 32)

    def test_a_root_that_is_missing_raises_and_is_not_made(self) -> None:
        with self.assertRaises(OSError):
            FileApprovalStore(self.root / "absent").digest_key()

        self.assertFalse((self.root / "absent").exists())


if __name__ == "__main__":
    unittest.main()
