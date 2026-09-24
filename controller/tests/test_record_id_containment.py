"""Record ids must name a record inside the store, never a path out of it.

The file-backed stores built paths as ``root / f"{id}.json"``. REST path
parameters cannot carry "/", but MCP tool arguments can, so an id such as
"../outside" read a file next to the store root. Each test plants a record that
would parse successfully just outside the root, so a regression returns it
instead of failing for an unrelated reason.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.agent_jobs import AgentJobStore
from app.approvals import FileApprovalStore
from app.models import SessionRecord
from app.session_store import FileSessionStore
from app.witness import WitnessRecorder

RECEIPT = {
    "profile": "normal",
    "event_type": "browser_action",
    "status": "ok",
    "action": "click",
    "action_class": "write",
    "operator": {"id": "alice", "source": "header"},
}
ESCAPES = ("../planted", "nested/../../planted", "/tmp/planted", "..", ".hidden", "")


def _session_record(record_id: str) -> SessionRecord:
    return SessionRecord(
        id=record_id,
        name="planted",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        status="closed",
        current_url="https://example.com",
        title="planted",
        artifact_dir="/tmp",
        takeover_url="http://127.0.0.1:6080",
        remote_access={},
    )


class RecordPathTests(unittest.TestCase):
    def test_plain_ids_resolve_inside_the_root(self) -> None:
        from app.utils import record_path

        root = Path("/data/store")
        self.assertEqual(record_path(root, "a1b2c3d4e5f6", ".json"), root / "a1b2c3d4e5f6.json")
        self.assertEqual(record_path(root, "auth-profiles", ".jsonl"), root / "auth-profiles.jsonl")

    def test_ids_that_are_not_one_plain_name_do_not_exist(self) -> None:
        from app.utils import record_path

        for record_id in ESCAPES:
            with self.subTest(record_id=record_id), self.assertRaises(KeyError):
                record_path(Path("/data/store"), record_id, ".json")


class StoreContainmentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "store"
        self.root.mkdir()

    async def test_session_store(self) -> None:
        (self.base / "planted.json").write_text(_session_record("planted").model_dump_json(), encoding="utf-8")
        store = FileSessionStore(self.root)

        with self.assertRaises(KeyError):
            await store.get("../planted")

    async def test_agent_job_store(self) -> None:
        (self.base / "planted.json").write_text("{}", encoding="utf-8")
        store = AgentJobStore(self.root)

        with self.assertRaises(KeyError):
            await store.get("../planted")

    async def test_approval_store(self) -> None:
        (self.base / "planted.json").write_text("{}", encoding="utf-8")
        store = FileApprovalStore(self.root)

        with self.assertRaises(KeyError):
            await store.get("../planted")

    async def test_witness_scope(self) -> None:
        planted_witness = WitnessRecorder(self.base)
        await planted_witness.record("planted", **RECEIPT)
        store = WitnessRecorder(self.root)

        with self.assertRaises(KeyError):
            await store.list("../planted")

        # A plain scope still reads normally.
        await store.record("session-1", **RECEIPT)
        self.assertEqual(len(await store.list("session-1")), 1)


if __name__ == "__main__":
    unittest.main()
