"""The whole arguments of a governed tool call's approval: kept beside the record, read by whoever may read it.

The reason previews 600 characters of them, so an operator approved the rest
of a long eval_js or memory profile unseen; and keeping them on the record
would put megabytes into every approvals listing. They are written beside the
record before it is shown, served by GET /approvals/{id}/arguments under the
record's own access rule, and removed once the call spends the approval (or, for
a call whose process died, at the next start); the record keeps their size and
sha256. A rejected approval keeps them, as an operator may approve it after all,
and an approve of one whose arguments are gone is refused.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import events, webhooks
from app.approvals import CONTROLLER_SCOPE, ApprovalStore, held_for_execution, refused_before_effect
from app.models import BrowserActionDecision, OperatorIdentity
from app.routes.operations import create_operations_router
from app.session_ownership import acting_as
from app.tool_gateway.gateway import McpToolGateway
from app.tool_inputs import EvalJsInput
from app.utils import PRIVATE_FILE_MODE, UTC, atomic_write_text
from tests.test_approval_spend import SESSION, SpendBase

ALICE = OperatorIdentity(id="alice", source="token")
# How long asking for a 4 MB call's approval may take on a loaded machine. Its preview was escaped
# whole before it was cut to 600 characters: about 25 s with the event loop blocked, ask and spend alike.
ASK_SECONDS_BOUND = 5
BOB = OperatorIdentity(id="bob", source="token")


class ApprovalArgumentsTests(SpendBase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.app = FastAPI()
        self.app.include_router(create_operations_router(manager=self.manager, proxy_store=None, cron_service=None))

        @self.app.exception_handler(KeyError)
        async def not_found(_: Request, exc: KeyError) -> JSONResponse:  # as app.main answers an unknown id
            return JSONResponse(status_code=404, content={"detail": f"Not found: {exc.args[0]}"})

    async def get(self, operator: OperatorIdentity | None, path: str) -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
            with acting_as(operator):
                return await client.get(path)

    async def ask(self, operator: OperatorIdentity | None, tool: str, **arguments: Any) -> str:
        with acting_as(operator):
            asked = await self.call(tool, workflow_profile="governed", **arguments)
        self.assertEqual(asked.structuredContent.get("status"), "approval_required", asked.content[0].text)
        return asked.structuredContent["approval_id"]

    def arguments_file(self, approval_id: str) -> Path:
        return Path(self.settings.approval_root) / f"{approval_id}.arguments"

    async def test_only_who_may_read_the_record_reads_its_arguments(self) -> None:
        approval_id = await self.ask(ALICE, "browser.delete_memory_profile", profile_name="shared-login")

        mine = await self.get(ALICE, f"/approvals/{approval_id}/arguments")
        theirs = await self.get(BOB, f"/approvals/{approval_id}/arguments")

        self.assertEqual(mine.status_code, 200, mine.text)
        self.assertEqual(json.loads(mine.json()["arguments"]), {"profile_name": "shared-login"})
        self.assertEqual((await self.get(BOB, f"/approvals/{approval_id}")).status_code, 404)
        self.assertEqual(theirs.status_code, 404)
        self.assertNotIn("shared-login", theirs.text)
        self.assertEqual((await self.manager.approvals.get(approval_id)).session_id, CONTROLLER_SCOPE)

    async def test_megabytes_of_arguments_stay_out_of_the_record_and_its_listing(self) -> None:
        notes = ["n" * 1000] * 4000  # 4 MB, as a governed memory profile may carry
        queue = events.subscribe(SESSION)
        self.addCleanup(events.unsubscribe, SESSION, queue)

        approval_id = await self.ask(
            None, "browser.save_memory_profile", session_id=SESSION, profile_name="big", goal_summary="g", notes=notes
        )

        approval = await self.manager.approvals.get(approval_id)
        text = (await self.get(None, f"/approvals/{approval_id}/arguments")).json()["arguments"]
        self.assertEqual(json.loads(text)["notes"], notes)
        self.assertEqual(
            (approval.arguments_size, approval.arguments_sha256),
            (len(text), hashlib.sha256(text.encode("utf-8")).hexdigest()),
        )
        self.assertLess(len(json.dumps(await self.manager.list_approvals())), 10_000)
        sent = [json.loads(queue.get_nowait()) for _ in range(queue.qsize())]
        self.assertEqual([event["arguments_size"] for event in sent if event["event"] == "approval"], [len(text)])

    async def test_a_multi_megabyte_call_is_asked_and_spent_within_a_bound_its_preview_escaped(self) -> None:
        notes = ["\u202e" + "n" * 999] + ["n" * 1000] * 4000  # 4 MB, a bidi override where the preview shows
        decide, write_out = McpToolGateway._governed_call_decision, McpToolGateway._governed_call_arguments
        loop_thread = threading.current_thread()
        ran: dict[str, str] = {}

        def where(step: str) -> None:
            ran[step] = "on the event loop" if threading.current_thread() is loop_thread else "in a worker thread"

        def decision(spec: Any, arguments: Any, **sized: Any) -> Any:
            # The ask sizes its preview; the spend builds only the digest it matches on.
            where("preview and digest" if sized.get("arguments_size") is not None else "spend digest")
            return decide(spec, arguments, **sized)

        def whole(spec: Any, arguments: Any) -> str:
            where("whole arguments")
            return write_out(spec, arguments)

        def file_digest(text: str) -> str:
            where("file digest")
            return hashlib.sha256(text.encode("utf-8")).hexdigest()

        started = time.monotonic()
        with (
            patch.object(McpToolGateway, "_governed_call_decision", staticmethod(decision)),
            patch.object(McpToolGateway, "_governed_call_arguments", staticmethod(whole)),
            patch("app.approvals._text_sha256", file_digest, create=True),
        ):
            approval_id = await self.ask(
                None,
                "browser.save_memory_profile",
                session_id=SESSION,
                profile_name="big",
                goal_summary="g",
                notes=notes,
            )
        asked = time.monotonic() - started
        await self.manager.approve(approval_id)
        self.manager.memory.root = Path(self.tempdir.name) / "memory"  # where the spent call saves the profile
        await self.manager.memory.startup()
        started = time.monotonic()
        with patch.object(McpToolGateway, "_governed_call_decision", staticmethod(decision)):
            spent = await self.call(
                "browser.save_memory_profile",
                session_id=SESSION,
                profile_name="big",
                goal_summary="g",
                notes=notes,
                workflow_profile="governed",
                approval_id=approval_id,
            )
        spending = time.monotonic() - started

        self.assertLess(max(asked, spending), ASK_SECONDS_BOUND, {"asked": asked, "spending": spending})
        # PDT-9: the ask's dumps and hashes of the whole arguments, each about 0.1 s for 4 MB, ran on the
        # event loop, which serves every session, and so did the spend's digest (0.065 s); a timing bound
        # does not see them, a thread does.
        steps = ("whole arguments", "preview and digest", "file digest", "spend digest")
        self.assertEqual(ran, dict.fromkeys(steps, "in a worker thread"))
        self.assertFalse(spent.isError, spent.content[0].text[:200])
        reason = (await self.manager.approvals.get(approval_id)).reason
        self.assertNotIn("\u202e", reason)  # escaped: the cut note's ellipsis is the one non-ASCII
        self.assertIn('"notes": ["\\u202ennn', reason)

    async def test_a_call_that_spends_its_approval_builds_no_preview_and_writes_out_nothing(self) -> None:
        # It matches on the stand-in's text alone: the preview and the whole arguments cost the spend what
        # they cost the ask.
        approval_id = await self.ask(None, "browser.eval_js", session_id=SESSION, expression="pay()")
        await self.manager.approve(approval_id)
        built: list[Any] = []
        decide, write_out = McpToolGateway._governed_call_decision, McpToolGateway._governed_call_arguments

        def decision(spec: Any, arguments: Any, **sized: Any) -> Any:
            built.append(("preview", sized.get("arguments_size")))
            return decide(spec, arguments, **sized)

        def whole(spec: Any, arguments: Any) -> str:
            built.append(("arguments", spec.name))
            return write_out(spec, arguments)

        with (
            patch.object(McpToolGateway, "_governed_call_decision", staticmethod(decision)),
            patch.object(McpToolGateway, "_governed_call_arguments", staticmethod(whole)),
        ):
            ran = await self.call(
                "browser.eval_js",
                session_id=SESSION,
                expression="pay()",
                workflow_profile="governed",
                approval_id=approval_id,
            )

        self.assertFalse(ran.isError, ran.content[0].text)
        self.assertEqual(built, [("preview", None)])

    async def tool_call_store(self, db_path: Path | None = None) -> tuple[ApprovalStore, Any]:
        store = ApprovalStore(Path(self.tempdir.name) / "own" / "approvals", db_path=str(db_path) if db_path else None)
        await store.startup()
        stand_in, _ = McpToolGateway._governed_call_decision(
            self.gateway._registry.get("browser.eval_js"), EvalJsInput(session_id=SESSION, expression="pay()")
        )
        return store, stand_in

    async def test_the_sqlite_store_keeps_the_arguments_size_and_digest(self) -> None:
        db_path = Path(self.tempdir.name) / "own" / "operator.db"
        store, stand_in = await self.tool_call_store(db_path)
        text = '{"expression": "pay()"}'

        asked = await store.create_or_reuse_pending(
            session_id=SESSION, kind="write", reason="r", action=stand_in, origin="tool_call", arguments=text
        )

        with sqlite3.connect(db_path) as conn:
            (payload,) = conn.execute("SELECT payload FROM approvals WHERE id = ?", (asked.id,)).fetchone()
        conn.close()
        restarted, _ = await self.tool_call_store(db_path)
        loaded = await restarted.get(asked.id)
        stored = json.loads(payload)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self.assertEqual((stored["arguments_size"], stored["arguments_sha256"]), (len(text), digest))
        self.assertEqual((loaded.arguments_size, loaded.arguments_sha256), (len(text), digest))

    async def test_no_record_is_listed_whose_arguments_were_not_written(self) -> None:
        # The arguments are written before the record: a record listed without them would be one an
        # operator approves without reading.
        store, stand_in = await self.tool_call_store()

        with (
            patch.object(store.file_store, "write_arguments", AsyncMock(side_effect=OSError("disk full"))),
            self.assertRaises(OSError),
        ):
            await store.create_or_reuse_pending(
                session_id=SESSION, kind="write", reason="r", action=stand_in, origin="tool_call", arguments="{}"
            )

        self.assertEqual(await store.list(), [])

    async def test_a_failed_removal_of_spent_arguments_does_not_fail_the_call(self) -> None:
        approval_id = await self.ask(None, "browser.eval_js", session_id=SESSION, expression="pay()")
        await self.manager.approve(approval_id)

        failed = AsyncMock(side_effect=PermissionError(13, "Permission denied"))
        with (
            patch.object(self.manager.approvals.file_store, "delete_arguments", failed),
            self.assertLogs("app.approvals", "WARNING") as logged,
        ):
            ran = await self.call(
                "browser.eval_js",
                session_id=SESSION,
                expression="pay()",
                workflow_profile="governed",
                approval_id=approval_id,
            )

        self.assertFalse(ran.isError, ran.content[0].text)
        self.assertEqual((failed.await_count, await self.status(approval_id)), (1, "executed"))
        self.assertIn(f"could not remove the arguments of approval {approval_id}", "\n".join(logged.output))

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    async def test_the_arguments_file_is_private(self) -> None:
        approval_id = await self.ask(None, "browser.eval_js", session_id=SESSION, expression="pay()")

        self.assertEqual(stat.S_IMODE(self.arguments_file(approval_id).stat().st_mode), PRIVATE_FILE_MODE)

    async def test_what_is_kept_is_written_as_escapes(self) -> None:
        approval_id = await self.ask(None, "browser.eval_js", session_id=SESSION, expression="pay(/*\u202e*/ 'а')")

        text = (await self.get(None, f"/approvals/{approval_id}/arguments")).json()["arguments"]

        self.assertTrue(text.isascii(), text)
        self.assertIn("\\u202e", text)
        self.assertIn("\\u0430", text)

    async def test_credentials_stay_out_of_what_is_kept_as_they_stay_out_of_the_preview(self) -> None:
        cookie = {"name": "sid", "value": "SECRET-SID", "url": "https://u:hunter2@bank.example/?key=K1&lang=en"}
        approval_id = await self.ask(None, "browser.set_cookies", session_id=SESSION, cookies=[cookie])

        text = (await self.get(None, f"/approvals/{approval_id}/arguments")).json()["arguments"]

        self.assertEqual(json.loads(text)["cookies"][0]["name"], "sid")
        self.assertIn("lang=en", text)  # a query value that is no credential is shown
        for secret in ("SECRET-SID", "hunter2", "K1"):
            self.assertNotIn(secret, text)

    async def test_a_return_url_behind_a_leading_space_keeps_its_credentials_out_of_both(self) -> None:
        # Codex's round-5 read: a browser strips the space and reads https://u:SECRET@..., the preview did not,
        # and the reason and what is kept both carried SECRET.
        url = "https://bank.example/?return=%20https%3A%2F%2Fu%3ASECRET%40h.example%2F%3Ftoken%3DSECRET"
        cookie = {"name": "sid", "value": "v", "url": url}
        approval_id = await self.ask(None, "browser.set_cookies", session_id=SESSION, cookies=[cookie])

        text = (await self.get(None, f"/approvals/{approval_id}/arguments")).json()["arguments"]
        reason = (await self.manager.approvals.get(approval_id)).reason

        for shown in (text, reason):
            self.assertNotIn("SECRET", shown)

    async def test_a_return_url_written_with_other_slashes_keeps_its_credentials_out_of_all_three(self) -> None:
        """Codex B 2: a browser reads https:u:SECRET@h.example/ and https:////u:SECRET@h.example/ as authority
        u:SECRET@h.example (a special scheme skips any run of slashes after the colon); urllib read no authority
        in either, so SECRET reached the reason, the arguments file and the webhook."""
        for nested in (
            "https%3Au%3ASECRET%40h.example%2F%3Ftoken%3DSECRET",
            "https%3A%2F%2F%2F%2Fu%3ASECRET%40h.example%2F%3Ftoken%3DSECRET",
        ):
            with self.subTest(nested=nested):
                cookie = {"name": "sid", "value": "v", "url": f"https://bank.example/?return={nested}&lang=en"}
                approval_id = await self.ask(None, "browser.set_cookies", session_id=SESSION, cookies=[cookie])
                approval = await self.manager.approvals.get(approval_id)
                client = Mock()
                client.post = AsyncMock(return_value=Mock(status_code=200))
                with patch.object(webhooks, "get_client", return_value=client):
                    await webhooks.dispatch_approval_event(approval, webhook_url="https://hooks.example.com/approval")

                text = (await self.get(None, f"/approvals/{approval_id}/arguments")).json()["arguments"]
                webhook = client.post.await_args.kwargs["content"].decode()

                for shown in (text, approval.reason, webhook):
                    self.assertNotIn("SECRET", shown)
                self.assertIn("lang=en", text)

    async def test_a_query_value_that_only_looks_like_a_url_is_kept_as_text_and_the_call_is_asked(self) -> None:
        # Codex's round-5 read: the preview parsed https://[draft as a URL, urllib raised, and the governed call
        # never reached its approval.
        cookie = {"name": "sid", "value": "v", "url": "https://bank.example/?note=https://[draft"}
        approval_id = await self.ask(None, "browser.set_cookies", session_id=SESSION, cookies=[cookie])

        text = (await self.get(None, f"/approvals/{approval_id}/arguments")).json()["arguments"]

        self.assertIn("note=https://[draft", text)

    async def test_the_webhook_says_how_long_the_arguments_are_and_carries_none_of_them(self) -> None:
        approval_id = await self.ask(None, "browser.eval_js", session_id=SESSION, expression="pay('x')")
        approval = await self.manager.approvals.get(approval_id)
        client = Mock()
        client.post = AsyncMock(return_value=Mock(status_code=200))

        with patch.object(webhooks, "get_client", return_value=client):
            await webhooks.dispatch_approval_event(approval, webhook_url="https://hooks.example.com/approval")

        body = json.loads(client.post.await_args.kwargs["content"])
        self.assertEqual(body["arguments_size"], approval.arguments_size)
        self.assertIsNotNone(approval.arguments_size)
        self.assertNotIn("arguments", body)

    async def test_a_rejected_approval_keeps_its_arguments_for_an_operator_who_approves_it_after_all(self) -> None:
        # Round 4: a reject is not final, so its arguments stay to be read. They were removed on the
        # reject, and an approve after it let the call run with only the preview left to read.
        approval_id = await self.ask(None, "browser.eval_js", session_id=SESSION, expression="pay()")
        await self.manager.reject(approval_id, comment="no")

        self.assertTrue(self.arguments_file(approval_id).exists())
        await self.manager.approve(approval_id, comment="yes after all")
        shown = await self.get(None, f"/approvals/{approval_id}/arguments")
        self.assertEqual((shown.status_code, json.loads(shown.json()["arguments"])), (200, {"expression": "pay()"}))
        ran = await self.call("browser.eval_js", session_id=SESSION, expression="pay()", approval_id=approval_id)
        self.assertFalse(ran.isError, ran.content[0].text)
        self.assertFalse(self.arguments_file(approval_id).exists())

    async def test_an_approve_whose_arguments_are_gone_is_refused_before_the_record_changes(self) -> None:
        for decided in ("pending", "rejected"):
            with self.subTest(decided=decided):
                approval_id = await self.ask(None, "browser.eval_js", session_id=SESSION, expression=f"{decided}()")
                if decided == "rejected":
                    await self.manager.reject(approval_id, comment="no")
                self.arguments_file(approval_id).unlink()  # lost: an older release removed it, or a hand

                with self.assertRaisesRegex(
                    PermissionError, "its arguments are no longer stored; the agent asks again"
                ):
                    await self.manager.approve(approval_id, comment="ok")

                self.assertEqual((await self.manager.approvals.get(approval_id)).status, decided)

    async def test_an_approve_of_what_is_no_longer_whole_says_why_over_rest(self) -> None:
        # The route answered both refusals as a bare "Conflict" (the dashboard read "Could not approve ...:
        # Conflict"), the same text a Windows file lock gives: nothing said the agent must ask again.
        gone = await self.ask(None, "browser.eval_js", session_id=SESSION, expression="gone()")
        self.arguments_file(gone).unlink()
        typing = BrowserActionDecision(
            action="type", reason="sign in", selector="#pin", text="2468", sensitive=True, risk_category="write"
        )
        unheld = await self.manager.approvals.create_or_reuse_pending(
            session_id=SESSION, kind="write", reason="sign in", action=typing
        )
        self.manager.approvals._sensitive_texts.pop(unheld.id)  # as after a restart, or on another worker

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
            answers = {
                approval_id: await client.post(f"/approvals/{approval_id}/approve", json={"comment": "ok"})
                for approval_id in (gone, unheld.id)
            }

        self.assertEqual(
            {approval_id: (answer.status_code, answer.json()) for approval_id, answer in answers.items()},
            {
                gone: (409, {"detail": f"approval {gone}: its arguments are no longer stored; the agent asks again"}),
                unheld.id: (409, {"detail": f"approval {unheld.id}: its text is no longer held; the agent asks again"}),
            },
        )

    async def test_startup_removes_the_arguments_of_approvals_spent_or_gone(self) -> None:
        # A call whose process died mid-way leaves its approval executed: nothing ran the removal.
        store = self.manager.approvals
        kept = {
            status: await self.ask(None, "browser.eval_js", session_id=SESSION, expression=f"{status}()")
            for status in ("pending", "approved", "rejected")
        }
        await store.approve(kept["approved"])
        await store.reject(kept["rejected"])
        spent = await self.ask(None, "browser.eval_js", session_id=SESSION, expression="spent()")
        await store.approve(spent)
        await store.claim_execution(spent)  # claimed, and never released
        stray = self.arguments_file("0123456789ab")
        stray.write_text("{}", encoding="utf-8")  # no record names it

        restarted = type(store)(self.settings.approval_root)
        await restarted.startup()

        self.assertEqual(
            {key: self.arguments_file(value).exists() for key, value in kept.items()}, dict.fromkeys(kept, True)
        )
        self.assertEqual((self.arguments_file(spent).exists(), stray.exists()), (False, False))

    async def rejected_either_side_of_the_ttl(self) -> tuple[str, str]:
        """Two rejected tool calls, one rejected just inside the approval TTL ago and one just past it."""
        store = self.manager.approvals
        rejected = {}
        for side, age in (("within", -30), ("lapsed", 30)):
            approval_id = await self.ask(None, "browser.eval_js", session_id=SESSION, expression=f"{side}()")
            await self.manager.reject(approval_id, comment="no")
            record = await store.get(approval_id)
            decided = datetime.now(UTC) - store.approval_ttl - timedelta(seconds=age)
            record.decided_at = decided.isoformat().replace("+00:00", "Z")
            await store._persist(record)
            rejected[side] = approval_id
        return rejected["within"], rejected["lapsed"]

    async def test_a_rejected_tool_calls_arguments_go_once_the_rejection_is_older_than_the_ttl(self) -> None:
        # OV4-7: a reject kept the file for good (an operator may approve again), so a rejected call's
        # arguments stayed readable, and on disk, forever. Now for the approval TTL after the rejection.
        within, lapsed = await self.rejected_either_side_of_the_ttl()

        await self.ask(None, "browser.eval_js", session_id=SESSION, expression="next()")  # each create prunes

        self.assertEqual((self.arguments_file(within).exists(), self.arguments_file(lapsed).exists()), (True, False))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
            refused = await client.post(f"/approvals/{lapsed}/approve", json={"comment": "after all"})
            approved = await client.post(f"/approvals/{within}/approve", json={"comment": "after all"})
        self.assertEqual(
            (refused.status_code, refused.json()),
            (409, {"detail": f"approval {lapsed}: its arguments are no longer stored; the agent asks again"}),
        )
        self.assertEqual((approved.status_code, approved.json()["status"]), (200, "approved"))
        self.assertEqual((await self.manager.approvals.get(lapsed)).status, "rejected")

    async def test_startup_removes_the_arguments_of_a_rejection_older_than_the_ttl(self) -> None:
        within, lapsed = await self.rejected_either_side_of_the_ttl()
        store = self.manager.approvals

        restarted = type(store)(
            self.settings.approval_root, approval_ttl_minutes=int(store.approval_ttl.total_seconds() // 60)
        )
        await restarted.startup()

        self.assertEqual((self.arguments_file(within).exists(), self.arguments_file(lapsed).exists()), (True, False))

    async def test_startup_keeps_the_arguments_of_an_approval_whose_record_is_unreadable(self) -> None:
        # PDT-8: an unreadable record must neither stop the controller starting nor cost the arguments that
        # an approve of the mended record reads.
        approval_id = await self.ask(None, "browser.eval_js", session_id=SESSION, expression="pay()")
        (Path(self.settings.approval_root) / f"{approval_id}.json").write_text("{not a record", encoding="utf-8")

        with self.assertLogs("app.approvals", "WARNING") as logged:
            await type(self.manager.approvals)(self.settings.approval_root).startup()

        self.assertTrue(self.arguments_file(approval_id).exists())
        self.assertIn(
            f"kept the arguments of approval {approval_id}: its record is unreadable", "\n".join(logged.output)
        )

    async def test_the_arguments_file_is_written_off_the_event_loop(self) -> None:
        # PDT-8: megabytes written on the loop hold up every session, and the 4 MB test's bound is too loose
        # to notice.
        loop_thread = threading.current_thread()
        wrote: list[str] = []

        def write(path: Path, text: str, **kwargs: Any) -> None:
            if path.suffix == ".arguments":
                wrote.append("on the event loop" if threading.current_thread() is loop_thread else "in a worker thread")
            atomic_write_text(path, text, **kwargs)

        with patch("app.approval_backends.atomic_write_text", write):
            await self.ask(None, "browser.eval_js", session_id=SESSION, expression="pay()")

        self.assertEqual(wrote, ["in a worker thread"])

    async def test_a_deferred_rejection_that_applies_keeps_the_arguments(self) -> None:
        approval_id = await self.ask(None, "browser.eval_js", session_id=SESSION, expression="pay()")
        await self.manager.approve(approval_id)
        refusal = PermissionError("refused before the call began")

        with self.assertRaises(PermissionError):
            async with held_for_execution(self.manager.approvals, approval_id, may_give_back=True):
                await self.manager.reject(approval_id, comment="no")  # deferred: the claim may still give it back
                self.assertTrue(self.arguments_file(approval_id).exists())
                refused_before_effect(refusal)
                raise refusal

        self.assertEqual((await self.manager.approvals.get(approval_id)).status, "rejected")
        self.assertTrue(self.arguments_file(approval_id).exists())

    async def test_an_approval_given_back_approved_keeps_its_arguments(self) -> None:
        approval_id = await self.ask(None, "browser.eval_js", session_id=SESSION, expression="pay()")
        await self.manager.approve(approval_id)
        refusal = PermissionError("refused before the call began")

        with self.assertRaises(PermissionError):
            async with held_for_execution(self.manager.approvals, approval_id, may_give_back=True):
                refused_before_effect(refusal)
                raise refusal

        self.assertEqual((await self.manager.approvals.get(approval_id)).status, "approved")
        self.assertTrue(self.arguments_file(approval_id).exists())

    async def test_the_arguments_go_once_the_approval_is_spent_and_the_record_keeps_their_digest(self) -> None:
        approval_id = await self.ask(None, "browser.eval_js", session_id=SESSION, expression="pay()")
        await self.manager.approve(approval_id)

        ran = await self.call("browser.eval_js", session_id=SESSION, expression="pay()", approval_id=approval_id)

        self.assertFalse(ran.isError, ran.content[0].text)
        approval = await self.manager.approvals.get(approval_id)
        self.assertEqual(approval.status, "executed")
        self.assertFalse(self.arguments_file(approval_id).exists())
        self.assertIsNotNone(approval.arguments_size)
