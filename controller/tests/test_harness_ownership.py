"""Harness runs and staged skill candidates belong to the operator who started them.

A run's traces carry page content from a live session another operator may own,
and graduating a candidate is the step before a skill is promoted. Ownership
follows the rest of the controller: only a token-verified operator owns
anything, so a shared-token deployment, and every record written before this
release, stays open to everyone.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from app.audit import reset_current_operator, set_current_operator
from app.harness import Budget, EvidenceRequirement, Postcondition, TaskContract
from app.harness.iterate import HarnessRunRecord, HarnessService

DONE = {"url": "https://example.com/done", "text": "done"}


@contextmanager
def operator(operator_id: str | None, *, source: str = "token"):
    token = set_current_operator(operator_id, source=source)
    try:
        yield
    finally:
        reset_current_operator(token)


def _contract(contract_id: str = "example-read") -> TaskContract:
    return TaskContract(
        id=contract_id,
        goal="Open the example page and confirm done state.",
        postconditions=[
            Postcondition(kind="url_contains", value="example.com/done"),
            Postcondition(kind="text_contains", value="done"),
        ],
        evidence_required=[EvidenceRequirement(kind="trace"), EvidenceRequirement(kind="actions")],
        budget=Budget(max_attempts=1, max_steps=2, max_wall_seconds=300),
    )


class HarnessOwnershipTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.service = HarnessService(self.root)

    async def run_as(self, operator_id: str | None, *, contract_id: str = "example-read") -> HarnessRunRecord:
        with operator(operator_id):
            return await self.service.start_convergence(_contract(contract_id), mock_final_observation=DONE)

    async def test_run_and_candidate_record_the_verified_operator(self) -> None:
        record = await self.run_as("alice")

        self.assertEqual(record.status, "converged")
        self.assertEqual(record.owner, "alice")
        self.assertEqual(record.candidate.owner, "alice")

    async def test_other_operator_cannot_list_read_or_graduate_a_run(self) -> None:
        record = await self.run_as("alice")

        with operator("bob"):
            self.assertEqual(self.service.list_runs(), [])
            with self.assertRaises(PermissionError):
                self.service.get_status(record.id)
            with self.assertRaises(PermissionError):
                self.service.get_trace(record.id)
            with self.assertRaises(PermissionError):
                self.service.graduate(record.id)

    async def test_owner_keeps_full_access_to_their_run(self) -> None:
        record = await self.run_as("alice")

        with operator("alice"):
            self.assertEqual([item["id"] for item in self.service.list_runs()], [record.id])
            self.assertEqual(self.service.get_status(record.id)["owner"], "alice")
            self.assertEqual(self.service.get_trace(record.id)["final_observation"]["url"], DONE["url"])
            self.assertEqual(self.service.graduate(record.id)["status"], "staged")

    async def test_list_limit_counts_only_runs_the_caller_may_see(self) -> None:
        mine = await self.run_as("bob", contract_id="bob-read")
        for index in range(3):
            await self.run_as("alice", contract_id=f"alice-read-{index}")

        with operator("bob"):
            self.assertEqual([item["id"] for item in self.service.list_runs(limit=1)], [mine.id])

    async def test_header_asserted_identity_cannot_reach_an_owned_run(self) -> None:
        record = await self.run_as("alice")

        with operator("alice", source="header"):
            self.assertEqual(self.service.list_runs(), [])
            with self.assertRaises(PermissionError):
                self.service.get_status(record.id)

    async def test_other_operator_cannot_list_read_or_drift_check_a_candidate(self) -> None:
        record = await self.run_as("alice")
        skill_id = record.candidate.skill_id

        with operator("bob"):
            self.assertEqual(self.service.list_candidates(), [])
            self.assertEqual(await self.service.check_all_drifts(), [])
            with self.assertRaises(PermissionError):
                self.service.get_candidate(skill_id)
            with self.assertRaises(PermissionError):
                await self.service.check_drift(skill_id)
        self.assertFalse((self.root / "skills" / "staging" / skill_id / "drift.json").exists())

    async def test_owner_sees_and_checks_their_candidate(self) -> None:
        record = await self.run_as("alice")
        skill_id = record.candidate.skill_id

        with operator("alice"):
            self.assertEqual([item["skill_id"] for item in self.service.list_candidates()], [skill_id])
            self.assertEqual(self.service.get_candidate(skill_id)["owner"], "alice")
            self.assertEqual((await self.service.check_drift(skill_id))["status"], "healthy")
            self.assertEqual(len(await self.service.check_all_drifts()), 1)

    async def test_same_contract_from_two_operators_stages_two_candidates(self) -> None:
        alice = await self.run_as("alice")
        bob = await self.run_as("bob")

        self.assertNotEqual(alice.candidate.skill_id, bob.candidate.skill_id)
        with operator("alice"):
            self.assertEqual(self.service.get_candidate(alice.candidate.skill_id)["owner"], "alice")
            self.assertEqual(len(self.service.list_candidates()), 1)

    async def test_unowned_runs_stay_open_to_every_caller(self) -> None:
        # Shared-token, header-only and anonymous callers have no verified identity.
        shared = await self.run_as(None)
        header = None
        with operator("carol", source="header"):
            header = await self.service.start_convergence(_contract("header-read"), mock_final_observation=DONE)

        self.assertIsNone(shared.owner)
        self.assertIsNone(header.owner)
        self.assertIsNone(shared.candidate.owner)
        for caller in ("alice", "bob"):
            with operator(caller):
                self.assertEqual({item["id"] for item in self.service.list_runs()}, {shared.id, header.id})
                self.assertEqual(self.service.get_status(shared.id)["id"], shared.id)
                self.assertEqual(len(self.service.list_candidates()), 2)
                self.assertEqual(self.service.graduate(header.id)["status"], "staged")

    async def test_record_written_before_owners_existed_loads_and_stays_open(self) -> None:
        record = await self.run_as(None)
        run_json = self.root / "runs" / record.id / "run.json"
        legacy = json.loads(run_json.read_text(encoding="utf-8"))
        legacy.pop("owner", None)
        legacy["candidate"].pop("owner", None)
        run_json.write_text(json.dumps(legacy), encoding="utf-8")

        with operator("bob"):
            self.assertEqual([item["id"] for item in self.service.list_runs()], [record.id])
            self.assertIsNone(self.service.get_status(record.id)["owner"])
            self.assertEqual(self.service.graduate(record.id)["status"], "staged")

    async def test_signed_owner_cannot_be_stripped_to_open_a_candidate(self) -> None:
        from app.harness.register import mesh_identity_signer, mesh_identity_verifier
        from app.mesh.identity import NodeIdentity

        identity = NodeIdentity(self.root / "identity")
        service = HarnessService(
            self.root / "signed",
            signer=mesh_identity_signer(identity),
            envelope_verifier=mesh_identity_verifier(identity),
        )
        with operator("alice"):
            record = await service.start_convergence(_contract(), mock_final_observation=DONE)
        skill_id = record.candidate.skill_id
        with operator("alice"):
            self.assertEqual(service.get_candidate(skill_id)["owner"], "alice")

        path = Path(record.candidate.files["candidate.json"])
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored["owner"] = None
        path.write_text(json.dumps(stored), encoding="utf-8")

        with operator("bob"):
            with self.assertRaises(PermissionError):
                service.get_candidate(skill_id)
            self.assertEqual(service.list_candidates(), [])

    async def test_unknown_fields_are_still_refused(self) -> None:
        with self.assertRaises(ValueError):
            HarnessRunRecord.model_validate({"id": "x", "contract_id": "c", "contract_hash": "h", "bogus": 1})


if __name__ == "__main__":
    unittest.main()
