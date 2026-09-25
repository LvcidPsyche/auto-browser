from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.audit import AuditStore, reset_current_operator, set_current_operator


class AuditStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_append_and_filter_events(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = AuditStore(Path(tempdir))
            await store.startup()

            token = set_current_operator("operator-1", name="Alice")
            try:
                await store.append(
                    event_type="session_created",
                    status="ok",
                    action="create_session",
                    session_id="session-1",
                    details={"start_url": "https://example.com"},
                )
            finally:
                reset_current_operator(token)

            token = set_current_operator("operator-2", name="Bob")
            try:
                await store.append(
                    event_type="browser_action",
                    status="ok",
                    action="click",
                    session_id="session-2",
                )
            finally:
                reset_current_operator(token)

            all_events = await store.list(limit=10)
            self.assertEqual(len(all_events), 2)
            self.assertEqual(all_events[0].operator.id, "operator-2")

            session_events = await store.list(limit=10, session_id="session-1")
            self.assertEqual(len(session_events), 1)
            self.assertEqual(session_events[0].details["start_url"], "https://example.com")

            operator_events = await store.list(limit=10, operator_id="operator-1")
            self.assertEqual(len(operator_events), 1)
            self.assertEqual(operator_events[0].operator.name, "Alice")

    async def test_sqlite_store_enforces_retention(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "audit"
            db_path = Path(tempdir) / "db" / "operator.db"
            store = AuditStore(root, db_path=str(db_path), max_events=2)
            await store.startup()

            for index in range(3):
                token = set_current_operator(f"operator-{index}")
                try:
                    await store.append(
                        event_type="browser_action",
                        status="ok",
                        action="click",
                        session_id=f"session-{index}",
                    )
                finally:
                    reset_current_operator(token)

            events = await store.list(limit=10)
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0].session_id, "session-2")
            self.assertEqual(events[-1].session_id, "session-1")
            self.assertTrue(db_path.exists())

    async def test_file_store_trims_on_interval_not_every_append(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "audit"
            store = AuditStore(root, max_events=2, file_trim_interval=2)
            await store.startup()

            for index in range(3):
                token = set_current_operator(f"operator-{index}")
                try:
                    await store.append(
                        event_type="browser_action",
                        status="ok",
                        action="click",
                        session_id=f"session-{index}",
                    )
                finally:
                    reset_current_operator(token)

            raw_lines = (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len([line for line in raw_lines if line.strip()]), 3)

            token = set_current_operator("operator-3")
            try:
                await store.append(
                    event_type="browser_action",
                    status="ok",
                    action="click",
                    session_id="session-3",
                )
            finally:
                reset_current_operator(token)

            events = await store.list(limit=10)
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0].session_id, "session-3")
            self.assertEqual(events[-1].session_id, "session-2")


class FileAuditListTests(unittest.IsolatedAsyncioTestCase):
    async def _store(self, tempdir: str) -> AuditStore:
        store = AuditStore(Path(tempdir), file_trim_interval=10_000)
        await store.startup()
        return store

    async def test_returns_the_newest_events_first_up_to_the_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = await self._store(tempdir)
            for index in range(10):
                await store.append(event_type="browser_action", status="ok", action=f"a{index}")
            events = await store.list(limit=3)
            self.assertEqual([event.action for event in events], ["a9", "a8", "a7"])
            self.assertEqual(await store.list(limit=0), [])

    async def test_filters_match_exactly_not_by_substring(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = await self._store(tempdir)
            # "session-1" is a substring of "session-10"; and "session-1" also
            # appears in the second event's details without being its session.
            await store.append(event_type="browser_action", status="ok", session_id="session-1")
            await store.append(
                event_type="browser_action",
                status="ok",
                session_id="session-10",
                details={"note": "session-1"},
            )
            events = await store.list(limit=10, session_id="session-1")
            self.assertEqual([event.session_id for event in events], ["session-1"])

    async def test_values_json_escapes_still_match(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = await self._store(tempdir)
            for session_id in ('quote"d', "back\\slash", "café", "tab\there"):
                await store.append(event_type="browser_action", status="ok", session_id=session_id)
                events = await store.list(limit=10, session_id=session_id)
                self.assertEqual([event.session_id for event in events], [session_id])

    async def test_a_torn_line_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = await self._store(tempdir)
            await store.append(event_type="browser_action", status="ok", session_id="s1")
            with store.file_store.events_path.open("a", encoding="utf-8") as handle:
                handle.write('{"id": "torn", "session_id": "s1"\n')
            await store.append(event_type="browser_action", status="ok", session_id="s1")
            with self.assertLogs("app.audit", level="WARNING"):
                events = await store.list(limit=10, session_id="s1")
            self.assertEqual(len(events), 2)
