"""A staged skill candidate's signature has to be checked, not just produced.

GHSA-xmh3-cw7j-9gp5. Induction could sign a candidate envelope, but nothing on
the read side ever verified one, so a signature was an assertion the artifact
made about itself — the same shape as the pre-1.6.0 witness chain. Anyone able to
write to the staging root could edit a staged skill, or add one, and it would be
served as a governed candidate carrying provenance.

Two other honesty gaps close here: an unsigned envelope was a dict shaped exactly
like a signed one, and a candidate induced from a mock trace — no browser ever
ran — was indistinguishable from a converged one.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app.harness.induce import SkillCandidate
from app.harness.register import SkillStagingRegistry


def candidate(skill_id: str = "skill-1", **overrides) -> SkillCandidate:
    payload = {
        "skill_id": skill_id,
        "name": "example",
        "description": "example goal",
        "contract_hash": "contract-hash",
        "trace_hash": "trace-hash",
        "verifier_backend": "programmatic",
        "verifier_passed": True,
        "verifier_confidence": 1.0,
        "attempts": 1,
    }
    payload.update(overrides)
    return SkillCandidate(**payload)


def signed_envelope(contract_hash: str = "contract-hash", trace_hash: str = "trace-hash") -> dict:
    return {"contract_hash": contract_hash, "trace_hash": trace_hash, "signature": "valid"}


def accepting_verifier(envelope: dict) -> dict:
    if envelope.get("signature") != "valid":
        raise ValueError("bad signature")
    return envelope


class RegistryVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="auto-browser-skill-provenance-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def stage(self, entry: SkillCandidate) -> None:
        directory = self.root / entry.skill_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "candidate.json").write_text(entry.model_dump_json(), encoding="utf-8")

    def registry(self, *, verify: bool) -> SkillStagingRegistry:
        return SkillStagingRegistry(self.root, verifier=accepting_verifier if verify else None)

    def test_a_valid_signature_is_accepted(self) -> None:
        self.stage(candidate(signed=True, envelope=signed_envelope()))
        self.assertEqual(self.registry(verify=True).get_candidate("skill-1").skill_id, "skill-1")

    def test_a_forged_signature_is_refused(self) -> None:
        self.stage(candidate(signed=True, envelope={**signed_envelope(), "signature": "forged"}))
        with self.assertRaises(PermissionError):
            self.registry(verify=True).get_candidate("skill-1")

    def test_a_dropped_in_unsigned_candidate_is_refused(self) -> None:
        """Writing a candidate straight into the staging root must not work."""
        self.stage(candidate(envelope={"contract_hash": "x", "trace_hash": "y", "signed": False}))
        with self.assertRaises(PermissionError):
            self.registry(verify=True).get_candidate("skill-1")

    def test_a_candidate_with_no_envelope_at_all_is_refused(self) -> None:
        self.stage(candidate())
        with self.assertRaises(PermissionError):
            self.registry(verify=True).get_candidate("skill-1")

    def test_a_signature_over_a_different_candidate_is_refused(self) -> None:
        """A genuine signature stolen from another candidate is still a forgery."""
        self.stage(
            candidate(
                signed=True,
                contract_hash="mine",
                envelope=signed_envelope(contract_hash="somebody-elses"),
            )
        )
        with self.assertRaises(PermissionError):
            self.registry(verify=True).get_candidate("skill-1")

    def test_listing_omits_what_it_cannot_verify(self) -> None:
        self.stage(candidate("good", signed=True, envelope=signed_envelope()))
        self.stage(candidate("forged", signed=True, envelope={**signed_envelope(), "signature": "forged"}))
        listed = {entry["skill_id"] for entry in self.registry(verify=True).list_candidates()}
        self.assertEqual(listed, {"good"})

    def test_an_unsigned_deployment_still_reads_its_own_candidates(self) -> None:
        """No signer configured means nothing to check — not everything refused."""
        self.stage(candidate(envelope={"contract_hash": "x", "signed": False}))
        self.assertEqual(self.registry(verify=False).get_candidate("skill-1").skill_id, "skill-1")


class CandidateHonestyTests(unittest.TestCase):
    def test_unsigned_and_simulated_default_to_false_but_are_recorded(self) -> None:
        entry = candidate()
        self.assertFalse(entry.signed)
        self.assertFalse(entry.simulated)
        self.assertIn("simulated", json.loads(entry.model_dump_json()))


if __name__ == "__main__":
    unittest.main()
