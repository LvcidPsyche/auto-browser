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
import tempfile
import unittest
from pathlib import Path
from typing import Any

from app import events
from app.agent_jobs import AgentJobStore
from app.audit import AuditStore
from app.browser.aria_outline import outline_from_aria_snapshot
from app.harness import Budget, Postcondition, TaskContract
from app.harness.iterate import HarnessService
from app.harness.trace import TraceEnvelope
from app.models import AgentJobCheckpoint, OperatorIdentity, SessionRecord
from app.session_store import FileSessionStore
from app.utils import utc_now

# A lone surrogate, as a JSON "\ud800" escape decodes to: UTF-8 cannot encode it.
LONE = "\ud800"
REPLACED = "�"


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
    async def test_a_record_whose_title_holds_a_lone_surrogate_is_saved_with_the_replacement_character(self) -> None:
        store = FileSessionStore(self.root / "sessions")
        await store.startup()
        record = SessionRecord(
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

        await store.upsert(record)

        saved = await store.get("s1")
        self.assertEqual(saved.title, f"Sale{REPLACED}today")
        self.assertEqual(saved.downloads, [{"id": "d1", "suggested_filename": f"a{REPLACED}.txt"}])
        self.assertEqual(
            saved.model_dump(exclude={"title", "downloads"}), record.model_dump(exclude={"title", "downloads"})
        )


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
