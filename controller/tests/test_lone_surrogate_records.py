"""A lone surrogate in a record's text no longer costs the record.

A cookie's name, a page's title or a model's reply can hold a lone surrogate, and UTF-8 cannot encode one: the audit
store, the session store and the agent job store raised on it, after the change they were recording had been made.
Each now writes U+FFFD in its place, as the witness chain and actions.jsonl do, and the session event stream sends
it so.

A harness run failed the same way on a value of its contract's metadata, on its mock observation, and in a live
attempt on a page's accessible name (the accessibility outline decodes the aria snapshot's escapes, a lone
surrogate's too). Its contract and its trace are hashed as UTF-8, so it replaces one before it hashes or writes.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from app import events, utils
from app.agent_jobs import AgentJobStore
from app.audit import AuditStore
from app.browser.aria_outline import outline_from_aria_snapshot
from app.harness import Budget, Postcondition, TaskContract
from app.harness.iterate import HarnessService
from app.harness.trace import TraceEnvelope
from app.harness.verifier import UniversalVerifierAdapter
from app.harness.verifier.base import VerificationResult
from app.models import AgentJobCheckpoint, OperatorIdentity, SessionRecord
from app.session_store import FileSessionStore, RedisSessionStore
from app.utils import utc_now, without_lone_surrogates
from tests.test_persist_after_close import _MemoryRedis

# A lone surrogate, as a JSON "\ud800" escape decodes to: UTF-8 cannot encode it.
LONE = "\ud800"
REPLACED = "�"
# Another lone surrogate (a low one): a key holding it differs from one holding LONE only until both are replaced.
OTHER_LONE = chr(0xDC00)


class _Tmp(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)


class AuditStoreTests(_Tmp):
    async def _append_everywhere(self, store: AuditStore) -> None:
        await store.startup()

        event = await store.append(
            event_type=f"type{LONE}",
            status=f"ok{LONE}",
            action=f"browser.set_cookies{LONE}",
            session_id=f"s1{LONE}",
            approval_id=f"a1{LONE}",
            job_id=f"j1{LONE}",
            operator=OperatorIdentity(id=f"alice{LONE}", name=f"Alice{LONE}", source="header", asserted_id=LONE),
            details={"digest": {"cookies": [{"name": f"sid{LONE}", f"key{LONE}": ("a", LONE)}]}},
        )

        stored = await store.list(limit=10)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0], event)
        self.assertEqual(
            stored[0].details, {"digest": {"cookies": [{"name": f"sid{REPLACED}", f"key{REPLACED}": ["a", REPLACED]}]}}
        )
        self.assertEqual(
            (stored[0].event_type, stored[0].status, stored[0].action, stored[0].session_id),
            (f"type{REPLACED}", f"ok{REPLACED}", f"browser.set_cookies{REPLACED}", f"s1{REPLACED}"),
        )
        self.assertEqual((stored[0].approval_id, stored[0].job_id), (f"a1{REPLACED}", f"j1{REPLACED}"))
        self.assertEqual(
            stored[0].operator,
            OperatorIdentity(id=f"alice{REPLACED}", name=f"Alice{REPLACED}", source="header", asserted_id=REPLACED),
        )
        self.assertEqual(await store.list(session_id=f"s1{REPLACED}"), stored)

    async def test_an_append_holding_a_lone_surrogate_lands_readable_in_the_file_store(self) -> None:
        store = AuditStore(self.root / "audit")

        await self._append_everywhere(store)

        raw = (self.root / "audit" / "events.jsonl").read_bytes()
        self.assertIn(f"sid{REPLACED}".encode(), raw)
        self.assertNotIn(b"\\ud800", raw)

    async def test_an_append_holding_a_lone_surrogate_lands_readable_in_the_sqlite_store(self) -> None:
        store = AuditStore(self.root / "audit", db_path=str(self.root / "state.db"))

        await self._append_everywhere(store)

        self.assertIs(store._primary, store.sqlite_store)
        self.assertEqual(len(await store.file_store.list(limit=10)), 1)

    async def test_an_ordinary_event_is_stored_as_it_was_given(self) -> None:
        store = AuditStore(self.root / "audit")
        await store.startup()
        details = {"target": {"selector": "#pay"}, "count": 3, "ratio": 0.5, "ok": True, "none": None, "text": "né €"}

        event = await store.append(event_type="browser_action", status="ok", session_id="s1", details=details)

        self.assertEqual(event.details, details)
        self.assertEqual((await store.list(session_id="s1"))[0].details, details)


class SessionStoreTests(_Tmp):
    @staticmethod
    def _record() -> SessionRecord:
        return SessionRecord(
            id="s1",
            name="s1",
            created_at=utc_now(),
            updated_at=utc_now(),
            status="active",
            current_url="https://example.com/",
            title=f"Sale{LONE}today",
            artifact_dir="artifacts/s1",
            takeover_url="http://127.0.0.1:6080/vnc.html",
            remote_access={},
            downloads=[{"id": "d1", "suggested_filename": f"a{LONE}.txt"}],
        )

    def _assert_saved_with_the_replacement_character(self, saved: SessionRecord, record: SessionRecord) -> None:
        self.assertEqual(saved.title, f"Sale{REPLACED}today")
        self.assertEqual(saved.downloads, [{"id": "d1", "suggested_filename": f"a{REPLACED}.txt"}])
        self.assertEqual(
            saved.model_dump(exclude={"title", "downloads"}), record.model_dump(exclude={"title", "downloads"})
        )

    async def test_a_record_whose_title_holds_a_lone_surrogate_is_saved_with_the_replacement_character(self) -> None:
        store = FileSessionStore(self.root / "sessions")
        await store.startup()
        record = self._record()

        await store.upsert(record)

        self._assert_saved_with_the_replacement_character(await store.get("s1"), record)

    async def test_the_redis_store_saves_such_a_record_the_same_way(self) -> None:
        # The real RedisSessionStore over an in-memory client: its write is a line of its own, which no test held.
        store = RedisSessionStore("redis://127.0.0.1:6379/0", "t")
        store.client = _MemoryRedis()  # type: ignore[assignment]
        record = self._record()

        await store.upsert(record)

        self._assert_saved_with_the_replacement_character(await store.get("s1"), record)


class AgentJobStoreTests(_Tmp):
    async def test_a_finished_run_whose_result_holds_a_lone_surrogate_keeps_its_result(self) -> None:
        store = AgentJobStore(self.root / "jobs")
        await store.startup()
        job = await store.create(session_id="s1", kind="agent_run", request={"goal": "g"}, operator=None)

        await store.append_checkpoint(
            job.id,
            AgentJobCheckpoint(step_index=1, created_at=utc_now(), status="acted", title=f"Sale{LONE}", reason=LONE),
        )
        await store.finish(
            job.id, status="completed", result={"steps": [{"observation": {"title": f"Sale{LONE}"}}]}, error=None
        )

        saved = await store.get(job.id)
        self.assertEqual(saved.status, "completed")
        self.assertEqual(saved.result, {"steps": [{"observation": {"title": f"Sale{REPLACED}"}}]})
        self.assertEqual([(item.title, item.reason) for item in saved.checkpoints], [(f"Sale{REPLACED}", REPLACED)])


DONE = {"url": "https://example.com/done", "text": "done"}
# A verifier command: it prints the file it is given, whatever it is sent.
PRINT_THE_FILE = "import pathlib, sys; sys.stdout.write(pathlib.Path(sys.argv[1]).read_text(encoding='ascii'))"


def _contract(**extra: Any) -> TaskContract:
    return TaskContract(
        id="example-read",
        goal="Open the example page and confirm done state.",
        postconditions=[Postcondition(kind="url_contains", value="example.com/done")],
        budget=Budget(max_attempts=1, max_steps=2, max_wall_seconds=300),
        **extra,
    )


class _OneStep:
    """Stands in for the orchestrator of a live attempt: one step, whose observation holds the outline it was given."""

    def __init__(self, accessibility_outline: dict[str, Any]) -> None:
        self.accessibility_outline = accessibility_outline

    async def run(self, **_: object) -> dict[str, Any]:
        observation = {
            "url": "https://example.com/done",
            "title": "Done",
            "accessibility_outline": self.accessibility_outline,
        }
        step = {
            "status": "acted",
            "decision": {"action": "done", "reason": "finished"},
            "observation": observation,
            "execution": {"action": "done"},
        }
        final_session = {"current_url": "https://example.com/done", "title": "Done"}
        return {"status": "completed", "steps": [step], "final_session": final_session}


class HarnessRunTests(_Tmp):
    """A run's contract and trace are hashed as UTF-8 before they are written, so the replacement comes first."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.service = HarnessService(self.root / "harness")

    def _run_dirs(self) -> list[Path]:
        return sorted((self.root / "harness" / "runs").iterdir())

    async def test_a_contract_whose_metadata_holds_a_lone_surrogate_runs_and_is_stored_under_its_own_hash(self) -> None:
        # A value of the caller's: the contract refuses a lone surrogate in every string it types, a metadata key too.
        contract = _contract(metadata={"note": f"n{LONE}", "nested": {f"k{LONE}": [LONE]}})

        record = await self.service.start_convergence(contract, mock_final_observation=DONE)

        self.assertEqual(record.status, "converged", record.attempts)
        (run_dir,) = self._run_dirs()  # and no directory of a run that never was
        stored = TaskContract.model_validate_json((run_dir / "contract.json").read_text(encoding="utf-8"))
        self.assertEqual(stored.metadata, {"note": f"n{REPLACED}", "nested": {f"k{REPLACED}": [REPLACED]}})
        self.assertEqual(stored.hash(), record.contract_hash)

    async def test_a_mock_observation_holding_a_lone_surrogate_is_recorded_with_the_replacement_character(self) -> None:
        observation = {**DONE, "text": f"done{LONE}", "title": f"Done{LONE}"}

        record = await self.service.start_convergence(_contract(), mock_final_observation=observation)

        self.assertEqual(record.status, "converged", record.attempts)
        trace = TraceEnvelope.read_json(record.attempts[0].trace_path)  # its chain verifies
        self.assertEqual(trace.final_observation, {**DONE, "text": f"done{REPLACED}", "title": f"Done{REPLACED}"})

    async def test_a_live_attempt_on_a_page_with_a_lone_surrogate_in_an_accessible_name_keeps_its_trace(self) -> None:
        # As Chromium's aria snapshot writes a button named "Pay" and a lone surrogate: the name's escape, which the
        # outline decodes. (A page's title and text reach the controller with U+FFFD already.)
        outline = outline_from_aria_snapshot('- button "Pay\\ud800"')
        self.assertEqual(outline["nodes"][0]["name"], f"Pay{LONE}")

        record = await self.service.start_convergence(_contract(), orchestrator=_OneStep(outline), session_id="s1")

        self.assertEqual(record.status, "converged", record.attempts)
        trace = TraceEnvelope.read_json(record.attempts[0].trace_path)  # its chain verifies
        (action,) = [event for event in trace.events if event.event_type == "action"]
        self.assertEqual(action.payload["observation"]["accessibility_outline"]["nodes"][0]["name"], f"Pay{REPLACED}")
        self.assertEqual(trace.final_observation["accessibility_outline"]["nodes"][0]["name"], f"Pay{REPLACED}")

    def _verifier_that_replies(self, reply: dict[str, Any]) -> UniversalVerifierAdapter:
        """The Universal Verifier over a command that prints ``reply`` as JSON, a lone surrogate as its escape."""
        path = self.root / "reply.json"
        path.write_text(json.dumps(reply), encoding="ascii")
        return UniversalVerifierAdapter(command=[sys.executable, "-c", PRINT_THE_FILE, str(path)], timeout_seconds=60)

    async def test_a_verifiers_reply_holding_a_lone_surrogate_keeps_its_verdict_and_the_run_its_record(self) -> None:
        # json.loads decodes the escape. In a value the result keeps untyped, the run's record could not be written:
        # the call raised and run.json read "running". In a string the result types, or in a key, the verdict was lost.
        replies = {
            "a value the result keeps untyped": {"passed": True, "extra": f"fine{LONE}"},
            "the notes": {"passed": True, "notes": f"fine{LONE}"},
            "a postcondition's name": {"passed": False, "failed_postconditions": [f"p{LONE}"]},
            "a key": {"passed": True, f"k{LONE}": [{f"j{LONE}": 1}]},
        }
        for number, (where, reply) in enumerate(replies.items()):
            with self.subTest(where=where):
                reply = {**reply, "confidence": 1.0}
                service = HarnessService(self.root / f"harness-{number}", verifier=self._verifier_that_replies(reply))

                record = await service.start_convergence(_contract(), mock_final_observation=DONE)

                self.assertEqual(record.status, "converged" if reply["passed"] else "unconverged", record.notes)
                stored = service.store.get(record.id)  # its record reads back, and says the same
                self.assertEqual(stored.status, record.status)
                verification = stored.attempts[0].verification
                self.assertIs(verification.passed, reply["passed"], verification.notes)
                self.assertEqual(verification.details["raw"], without_lone_surrogates(reply))
                self.assertIn(REPLACED, json.dumps(verification.details["raw"], ensure_ascii=False))

    async def test_a_run_whose_result_holds_a_lone_surrogate_is_saved_in_the_state_it_ended_in(self) -> None:
        # The store's own write, for a result that did not come through the adapter above (HarnessService takes any
        # verifier). The write raised after the run's state was set, and run.json went on reading "running".
        class OfItsOwn:
            def __init__(self, passed: bool) -> None:
                self.passed = passed

            async def verify(self, contract: TaskContract, trace: Any) -> VerificationResult:
                return VerificationResult(passed=self.passed, confidence=1.0, details={"raw": {"notes": f"n{LONE}"}})

        for passed in (False, True):
            with self.subTest(passed=passed):
                service = HarnessService(self.root / f"harness-{passed}", verifier=OfItsOwn(passed))

                record = await service.start_convergence(_contract(), mock_final_observation=DONE)

                stored = service.store.get(record.id)
                self.assertEqual(stored.status, record.status)
                self.assertNotEqual(stored.status, "running")
                if not passed:
                    # A run that passed stages a candidate from the verifier's own result, a step of its own.
                    self.assertEqual(stored.status, "unconverged", record.notes)
                self.assertEqual(stored.attempts[0].verification.details, {"raw": {"notes": f"n{REPLACED}"}})


class SessionEventStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_observe_event_whose_title_holds_a_lone_surrogate_is_sent_with_the_replacement_character(
        self,
    ) -> None:
        queue = events.subscribe("s1")
        self.addCleanup(events.unsubscribe, "s1", queue)
        everything = events.subscribe_all()
        self.addCleanup(events.unsubscribe_all, everything)

        events.emit_observe("s1", "https://example.com/", f"Sale{LONE}today")

        for payload in (queue.get_nowait(), everything.get_nowait()):
            # Neither the raw surrogate nor its escape, which a strict JSON parser refuses.
            payload.encode("utf-8")
            self.assertNotIn("\\ud800", payload)
            self.assertEqual(json.loads(payload)["title"], f"Sale{REPLACED}today")


class KeysThatMeetTests(_Tmp):
    """Two keys that differ only in a lone surrogate take one name once it is replaced: neither value is dropped."""

    def test_a_changed_key_whose_name_is_taken_is_kept_under_a_numbered_name(self) -> None:
        # The key that needed no change keeps its name, wherever among them it stands.
        cleaned = without_lone_surrogates({LONE: 1, REPLACED: 2, OTHER_LONE: 3})

        self.assertEqual(list(cleaned.items()), [(f"{REPLACED} (2)", 1), (REPLACED, 2), (f"{REPLACED} (3)", 3)])

    def test_two_changed_keys_that_meet_keep_the_first_ones_name_plain(self) -> None:
        cleaned = without_lone_surrogates({f"a{LONE}": 1, f"a{OTHER_LONE}": 2, "b": 3})

        self.assertEqual(list(cleaned.items()), [(f"a{REPLACED}", 1), (f"a{REPLACED} (2)", 2), ("b", 3)])

    def test_a_numbered_name_that_is_taken_too_is_passed_over(self) -> None:
        cleaned = without_lone_surrogates({f"k{LONE}": 1, f"k{REPLACED}": 2, f"k{REPLACED} (2)": 3})

        self.assertEqual(cleaned, {f"k{REPLACED} (3)": 1, f"k{REPLACED}": 2, f"k{REPLACED} (2)": 3})

    def test_keys_that_meet_deeper_down_are_kept_and_what_they_hold_is_cleaned(self) -> None:
        cleaned = without_lone_surrogates({"outer": [{f"a{LONE}": LONE, f"a{OTHER_LONE}": {"deep": [LONE]}}]})

        self.assertEqual(cleaned, {"outer": [{f"a{REPLACED}": REPLACED, f"a{REPLACED} (2)": {"deep": [REPLACED]}}]})

    def test_keys_that_do_not_meet_come_back_under_the_names_they_had(self) -> None:
        value = {f"b{LONE}": 1, "a": 2, f"c{LONE}": [LONE], 3: "x", None: "y", "a (2)": 4}

        cleaned = without_lone_surrogates(value)

        self.assertEqual(
            list(cleaned.items()),
            [(f"b{REPLACED}", 1), ("a", 2), (f"c{REPLACED}", [REPLACED]), (3, "x"), (None, "y"), ("a (2)", 4)],
        )

    async def test_an_audit_event_whose_details_hold_such_keys_keeps_every_value(self) -> None:
        store = AuditStore(self.root / "audit")
        await store.startup()

        await store.append(
            event_type="browser_action", status="ok", session_id="s1", details={LONE: 1, REPLACED: 2, OTHER_LONE: 3}
        )

        (stored,) = await store.list(limit=10)
        self.assertEqual(stored.details, {f"{REPLACED} (2)": 1, REPLACED: 2, f"{REPLACED} (3)": 3})


class CostOfKeysThatMeetTests(unittest.TestCase):
    """Each key and each item is cleaned once, whatever the keys do: a level more costs what the level before did.

    The dict was cleaned once to find that its keys met and again to rename them, so each level of such dicts doubled
    the work: 20 levels (441 bytes of JSON) held the event loop for eight seconds. Nothing here is timed.
    """

    @staticmethod
    def _nested(depth: int) -> Any:
        """A dict ``depth`` levels deep, two keys of every level taking one name once cleaned."""
        value: Any = "x"
        for _ in range(depth):
            value = {LONE: value, OTHER_LONE: 1}
        return value

    @staticmethod
    def _cleaned_and_visits(value: Any, *, at_most: int) -> tuple[Any, int]:
        """``value`` cleaned, and how many keys, items and containers the cleaner was handed on the way."""
        clean = utils.without_lone_surrogates
        visits = 0

        def counted(part: Any) -> Any:
            nonlocal visits
            visits += 1
            if visits > at_most:
                raise AssertionError(f"the cleaner was handed more than {at_most} parts")
            return clean(part)

        with patch.object(utils, "without_lone_surrogates", counted):
            return counted(value), visits

    def test_a_dict_nested_60_deep_whose_keys_meet_at_every_level_is_cleaned(self) -> None:
        cleaned, _ = self._cleaned_and_visits(self._nested(60), at_most=1000)

        for _ in range(60):
            self.assertEqual(list(cleaned), [REPLACED, f"{REPLACED} (2)"])
            self.assertEqual(cleaned[f"{REPLACED} (2)"], 1)
            cleaned = cleaned[REPLACED]
        self.assertEqual(cleaned, "x")

    def test_a_level_more_of_keys_that_meet_costs_the_same_whatever_the_depth(self) -> None:
        visits = [self._cleaned_and_visits(self._nested(depth), at_most=100_000)[1] for depth in range(1, 9)]

        steps = {more - fewer for fewer, more in zip(visits, visits[1:], strict=False)}
        self.assertEqual(len(steps), 1, visits)

    def test_keys_of_one_dict_that_all_meet_are_each_named_at_one_try(self) -> None:
        # Every key counted up from " (2)" again, so 4,000 keys that met (68 KB of JSON) tried eight million names and
        # held the event loop for five seconds. The names are the same: none below the last one given is ever free.
        keys = 500
        tried = 0

        class Name(str):
            """A cleaned key that counts each numbered name made from it."""

            def __format__(self, spec: str) -> str:
                nonlocal tried
                tried += 1
                if tried > 10 * keys:
                    raise AssertionError(f"more than {10 * keys} numbered names were tried for {keys} keys")
                return str(self).__format__(spec)

        clean = utils.without_lone_surrogates

        def named(part: Any) -> Any:
            cleaned = clean(part)
            return Name(cleaned) if isinstance(cleaned, str) else cleaned

        meeting = {f"k{chr(0xD800 + number)}": number for number in range(keys)}
        with patch.object(utils, "without_lone_surrogates", named):
            cleaned = clean(meeting)

        wanted = f"k{REPLACED}"
        self.assertEqual(list(cleaned), [wanted, *(f"{wanted} ({number})" for number in range(2, keys + 1))])
        self.assertEqual(list(cleaned.values()), list(range(keys)))
        self.assertEqual(tried, keys - 1)

    def test_a_numbered_name_a_later_key_holds_is_passed_over_by_every_key_that_meets_it(self) -> None:
        taken = f"a{REPLACED} (3)"

        cleaned = without_lone_surrogates({f"a{LONE}": 1, f"a{OTHER_LONE}": 2, f"a{chr(0xD801)}": 3, taken: 4})

        self.assertEqual(
            list(cleaned.items()), [(f"a{REPLACED}", 1), (f"a{REPLACED} (2)", 2), (f"a{REPLACED} (4)", 3), (taken, 4)]
        )
