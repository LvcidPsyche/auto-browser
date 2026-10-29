"""A staged skill candidate is bound to its signature and to the directory it is read from.

The registry used to compare only the contract and trace hashes (and the three generated files) with
the signed envelope, so the rest of candidate.json (safety_class, verifier_backend, the `files` map,
...) could be edited and still read as verified, and a candidate.json copied into another directory was
served under its stored skill_id with the files of the genuine directory.

Entry points: SkillStagingRegistry and HarnessService (what harness.get_candidate and
harness.list_candidates call), with a real mesh-identity signer and verifier.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from app.harness import Budget, EvidenceRequirement, Postcondition, TaskContract, TraceEnvelope
from app.harness.induce import SkillInducer
from app.harness.iterate import HarnessService
from app.harness.register import SkillStagingRegistry, mesh_identity_signer, mesh_identity_verifier
from app.harness.verifier.base import VerificationResult
from app.mesh.identity import NodeIdentity


def _contract() -> TaskContract:
    return TaskContract(
        id="example-read",
        goal="Open the example page and confirm done state.",
        postconditions=[Postcondition(kind="url_contains", value="example.com/done")],
        evidence_required=[EvidenceRequirement(kind="trace")],
        budget=Budget(max_attempts=1, max_steps=2, max_wall_seconds=300),
    )


def _induce(inducer: SkillInducer):
    contract = _contract()
    trace = TraceEnvelope(run_id="t1", contract_hash=contract.hash(), final_observation={"url": "ok"})
    return inducer.induce(
        contract=contract,
        trace=trace,
        verification=VerificationResult(passed=True, confidence=0.9, backend="programmatic"),
        attempts=1,
    )


class CandidateSignatureCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.staging = root / "staging"
        identity = NodeIdentity(root / "identity")
        self.candidate = _induce(SkillInducer(self.staging, signer=mesh_identity_signer(identity)))
        self.registry = SkillStagingRegistry(self.staging, verifier=mesh_identity_verifier(identity))
        self.dir = self.staging / self.candidate.skill_id

    def _edit_candidate_json(self, directory: Path, **changes) -> None:
        path = directory / "candidate.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(changes)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _copy_candidate_to(self, name: str) -> Path:
        copy = self.staging / name
        shutil.copytree(self.dir, copy)
        return copy

    def test_baseline_untampered_candidate_verifies(self) -> None:
        self.assertEqual(self.registry.get_candidate(self.candidate.skill_id).skill_id, self.candidate.skill_id)
        self.assertEqual(len(self.registry.list_candidates()), 1)

    def test_served_files_map_is_the_loaded_directory(self) -> None:
        served = self.registry.get_candidate(self.candidate.skill_id)
        self.assertEqual(set(served.files), set(self.candidate.files))
        for path in served.files.values():
            self.assertEqual(Path(path).parent.resolve(), self.dir.resolve())
        listed = self.registry.list_candidates()[0]["files"]
        self.assertEqual(listed, served.files)

    # ---- HARN-01: every field of candidate.json is bound to the signature ----

    def test_harn01_tampered_unsigned_fields_are_refused_by_get_candidate(self) -> None:
        tampered = {
            "safety_class": "destructive",
            "verifier_confidence": 0.0,
            "attempts": 99,
            "cost_usd": 1234.5,
            "name": "attacker-name",
            "description": "attacker description",
            "verifier_backend": "attacker",
            "model_tiers": {"executor": "attacker-model"},
            "created_at": 1.0,
            "signed": False,
        }
        for field, value in tampered.items():
            with self.subTest(field=field):
                original = json.loads((self.dir / "candidate.json").read_text(encoding="utf-8"))
                self._edit_candidate_json(self.dir, **{field: value})
                try:
                    with self.assertRaises(
                        PermissionError, msg=f"tampered candidate.json field {field!r} was accepted"
                    ):
                        self.registry.get_candidate(self.candidate.skill_id)
                finally:
                    self._edit_candidate_json(self.dir, **{field: original[field]})

    def test_harn01_tampered_candidate_is_not_listed(self) -> None:
        self._edit_candidate_json(self.dir, safety_class="destructive")
        listed = self.registry.list_candidates()
        self.assertEqual(
            listed,
            [],
            f"list_candidates served a tampered candidate: safety_class={listed[0]['safety_class']!r}"
            if listed
            else "",
        )

    def test_harn01_candidate_staged_without_a_candidate_hash_is_refused_with_a_reason(self) -> None:
        # What an older version signed: the same payload without candidate_hash.
        identity = NodeIdentity(Path(self._tmp.name) / "identity")
        payload = self.registry.verifier(self.candidate.envelope)
        legacy_payload = {k: v for k, v in payload.items() if k != "candidate_hash"}
        self._edit_candidate_json(self.dir, envelope=mesh_identity_signer(identity)(legacy_payload))
        with self.assertRaisesRegex(PermissionError, "induce it again"):
            self.registry.get_candidate(self.candidate.skill_id)
        self.assertEqual(self.registry.list_candidates(), [])

    # ---- HARN-02: files are hashed in the directory the candidate was read from ----

    def test_harn02_copied_candidate_with_tampered_files_is_refused(self) -> None:
        copy = self._copy_candidate_to("attacker-copy")
        (copy / "helper.py").write_text("import os\nos.system('echo owned')\n", encoding="utf-8")
        (copy / "SKILL.md").write_text("# attacker skill body\n", encoding="utf-8")
        with self.assertRaises(
            PermissionError, msg="candidate loaded from attacker-copy/ verified against the files in the genuine dir"
        ):
            self.registry.get_candidate("attacker-copy")

    def test_harn02_copied_candidate_is_not_listed_as_a_second_skill(self) -> None:
        copy = self._copy_candidate_to("attacker-copy")
        (copy / "helper.py").write_text("print('owned')\n", encoding="utf-8")
        ids = [c["skill_id"] for c in self.registry.list_candidates()]
        self.assertEqual(ids, [self.candidate.skill_id], f"list_candidates served the copied dir too: {ids}")

    def test_harn02_untampered_copy_is_refused_too(self) -> None:
        # The directory name must be the signed skill_id even when the files are the genuine ones.
        self._copy_candidate_to("attacker-copy")
        with self.assertRaises(PermissionError):
            self.registry.get_candidate("attacker-copy")


class VerifyDirectoryContractTests(unittest.TestCase):
    """_verify hashes the files in the directory it is handed, not the one the skill_id names."""

    def test_verify_refuses_an_altered_directory_named_for_the_skill_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity = NodeIdentity(root / "identity")
            candidate = _induce(SkillInducer(root / "staging", signer=mesh_identity_signer(identity)))
            registry = SkillStagingRegistry(root / "staging", verifier=mesh_identity_verifier(identity))
            genuine = root / "staging" / candidate.skill_id
            # Same name, so the directory-name check passes; the files are not the signed ones.
            altered = root / "elsewhere" / candidate.skill_id
            shutil.copytree(genuine, altered)
            (altered / "helper.py").write_text("import os\nos.system('id')\n", encoding="utf-8")
            registry._verify(candidate, genuine)
            with self.assertRaisesRegex(PermissionError, "helper.py"):
                registry._verify(candidate, altered)


class FileContainmentContractTests(unittest.TestCase):
    """The same file-link refusal as below, simulated: file symlinks need privileges this box may lack."""

    def test_file_resolving_outside_the_directory_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity = NodeIdentity(root / "identity")
            candidate = _induce(SkillInducer(root / "staging", signer=mesh_identity_signer(identity)))
            registry = SkillStagingRegistry(root / "staging", verifier=mesh_identity_verifier(identity))
            directory = root / "staging" / candidate.skill_id
            registry._verify(candidate, directory)
            real_resolve = Path.resolve

            def resolve_helper_elsewhere(self: Path, *args, **kwargs) -> Path:
                resolved = real_resolve(self, *args, **kwargs)
                return root / "outside" / resolved.name if resolved.name == "helper.py" else resolved

            with unittest.mock.patch.object(Path, "resolve", resolve_helper_elsewhere):
                with self.assertRaisesRegex(PermissionError, "helper.py outside its directory"):
                    registry._verify(candidate, directory)


def _link_dir(link: Path, target: Path) -> bool:
    """A directory symlink, or on Windows a junction (no admin needed). False if neither works."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        try:
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
            return True
        except OSError:
            pass
    return False


class CandidateContainmentTests(unittest.TestCase):
    """A linked candidate directory is held to the same containment as get_candidate's."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        identity = NodeIdentity(self.root / "identity")
        self.service = HarnessService(
            self.root / "harness",
            signer=mesh_identity_signer(identity),
            envelope_verifier=mesh_identity_verifier(identity),
        )
        self.candidate = _induce(self.service.inducer)
        self.dir = self.service.store.staging_root / self.candidate.skill_id

    def _relink_outside_staging(self) -> None:
        outside = self.root / "outside" / self.candidate.skill_id
        outside.parent.mkdir()
        shutil.copytree(self.dir, outside)
        shutil.rmtree(self.dir)
        if not _link_dir(self.dir, outside):
            self.skipTest("neither a symlink nor a junction can be created here")

    def test_linked_candidate_directory_is_neither_listed_nor_served(self) -> None:
        self._relink_outside_staging()
        self.assertEqual(self.service.list_candidates(), [])
        with self.assertRaises(KeyError):
            self.service.get_candidate(self.candidate.skill_id)

    def test_link_inside_staging_to_a_candidate_lists_it_once(self) -> None:
        if not _link_dir(self.dir.parent / "attacker-link", self.dir):
            self.skipTest("neither a symlink nor a junction can be created here")
        listed = [c["skill_id"] for c in self.service.list_candidates()]
        self.assertEqual(listed, [self.candidate.skill_id])

    def test_file_linked_out_of_the_candidate_directory_is_refused(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        target = outside / "helper.py"
        shutil.copy(self.dir / "helper.py", target)
        (self.dir / "helper.py").unlink()
        try:
            os.symlink(target, self.dir / "helper.py")
        except (OSError, NotImplementedError):
            self.skipTest("file symlinks cannot be created here")
        with self.assertRaises(PermissionError):
            self.service.get_candidate(self.candidate.skill_id)
        self.assertEqual(self.service.list_candidates(), [])


class CandidateBindingPropertyTests(unittest.TestCase):
    """Through HarnessService, stated as properties rather than as one particular fix."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        identity = NodeIdentity(self.root / "identity")
        self.service = HarnessService(
            self.root / "harness",
            signer=mesh_identity_signer(identity),
            envelope_verifier=mesh_identity_verifier(identity),
        )
        self.candidate = _induce(self.service.inducer)
        self.staging = self.service.store.staging_root
        self.dir = self.staging / self.candidate.skill_id

    def test_control_untampered_candidate_is_served(self) -> None:
        served = self.service.get_candidate(self.candidate.skill_id)
        self.assertEqual(served["skill_id"], self.candidate.skill_id)

    def test_harn01_served_files_map_stays_inside_the_candidate_dir(self) -> None:
        evil = self.root / "evil"
        evil.mkdir()
        (evil / "helper.py").write_text("import os\nos.system('id')\n", encoding="utf-8")
        data = json.loads((self.dir / "candidate.json").read_text(encoding="utf-8"))
        data["files"]["helper.py"] = str(evil / "helper.py")
        (self.dir / "candidate.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            served = self.service.get_candidate(self.candidate.skill_id)
        except PermissionError:
            return  # refusing the edited candidate is a correct fix
        outside = {
            name: path for name, path in served["files"].items() if Path(path).resolve().parent != self.dir.resolve()
        }
        self.assertEqual(outside, {}, "a verified candidate was served with files outside its directory")

    def test_harn02_copied_dir_is_refused_or_served_under_its_own_id(self) -> None:
        copy = self.staging / "attacker-copy"
        shutil.copytree(self.dir, copy)
        (copy / "helper.py").write_text("import os\nos.system('id')\n", encoding="utf-8")
        try:
            served = self.service.get_candidate("attacker-copy")
        except PermissionError:
            return  # refusing the copy is a correct fix
        self.assertEqual(
            served["skill_id"],
            "attacker-copy",
            "get_candidate('attacker-copy') verified and served the genuine candidate's identity",
        )


if __name__ == "__main__":
    unittest.main()
