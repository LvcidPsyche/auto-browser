"""A background persist never moves a closed session back to active (STORE-44)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.models import SessionRecord
from app.session_ownership import acting_as
from app.session_store import DurableSessionStore
from tests.test_auth_state_ownership import ALICE, build_manager


def _record(status: str, *, live: bool) -> SessionRecord:
    return SessionRecord(
        id="s1",
        name="s1",
        created_at="2026-01-01T00:00:01Z",
        updated_at="2026-01-01T00:00:00Z",
        status=status,  # type: ignore[arg-type]
        live=live,
        current_url="https://example.com",
        title="Example",
        artifact_dir="/tmp/artifacts",
        takeover_url="http://localhost:6080",
        remote_access={},
    )


class PersistAfterCloseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.manager, _ = build_manager(Path(self.tmp.name))
        self.manager.settings.max_sessions = 5
        with acting_as(ALICE):
            self.session_id = (await self.manager.create_session(name="alice"))["id"]
        self.session = self.manager.sessions[self.session_id]

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def stored_status(self) -> str:
        return (await self.manager.session_store.get(self.session_id)).status

    async def test_a_persist_after_the_close_leaves_the_record_closed(self) -> None:
        with acting_as(ALICE):
            await self.manager.close_session(self.session_id)

        await self.manager._persist_session(self.session, status="active")

        self.assertEqual(await self.stored_status(), "closed")

    async def test_a_session_closed_while_the_summary_is_built_is_not_persisted_active(self) -> None:
        real_summary = self.manager._session_summary

        async def summary_then_close(session, **kwargs):
            summary = await real_summary(session, **kwargs)
            # Once: the close builds its own summary through this same method.
            self.manager._session_summary = real_summary  # type: ignore[method-assign]
            with acting_as(ALICE):
                await self.manager.close_session(self.session_id)
            return summary

        self.manager._session_summary = summary_then_close  # type: ignore[method-assign]
        await self.manager._persist_session(self.session, status="active")

        self.assertEqual(await self.stored_status(), "closed")

    async def test_a_live_session_still_persists_active(self) -> None:
        self.session.last_action = "click"

        await self.manager._persist_session(self.session, status="active")

        self.assertEqual(await self.stored_status(), "active")

    async def test_the_store_refuses_to_reopen_a_closed_record(self) -> None:
        store = DurableSessionStore(file_root=Path(self.tmp.name) / "rec", redis_url=None, redis_prefix="t")
        await store.startup()
        await store.upsert(_record("closed", live=False))

        await store.upsert(_record("active", live=True))

        self.assertEqual((await store.get("s1")).status, "closed")

    async def test_the_store_still_marks_active_records_interrupted_and_closes_them(self) -> None:
        store = DurableSessionStore(file_root=Path(self.tmp.name) / "rec", redis_url=None, redis_prefix="t")
        await store.startup()
        await store.upsert(_record("active", live=True))
        await store.mark_all_active_interrupted()
        self.assertEqual((await store.get("s1")).status, "interrupted")

        await store.upsert(_record("closed", live=False))

        self.assertEqual((await store.get("s1")).status, "closed")


if __name__ == "__main__":
    unittest.main()
