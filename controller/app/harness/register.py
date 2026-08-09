from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from .induce import SkillCandidate

logger = logging.getLogger(__name__)

# Takes a stored envelope, returns the payload it attests to, raises if invalid.
Verifier = Callable[[dict[str, Any]], dict[str, Any]]


class SkillStagingRegistry:
    """Staged skill candidates, read back with their signatures checked.

    Induction could sign a candidate envelope, but nothing on the read side ever
    verified one — so a signature was an assertion the artifact made about
    itself, exactly like the pre-1.6.0 witness chain. Anyone who could write to
    the staging root could edit a staged skill, or drop in a new one, and it
    would be served as a governed, provenance-carrying candidate
    (GHSA-xmh3-cw7j-9gp5).

    `verifier` takes the stored envelope and returns the payload it actually
    attests to, raising if it does not verify. When one is configured, a
    candidate that fails to verify is refused rather than downgraded.
    """

    def __init__(self, staging_root: str | Path, verifier: Verifier | None = None):
        self.staging_root = Path(staging_root)
        self.verifier = verifier

    def list_candidates(self) -> list[dict[str, Any]]:
        if not self.staging_root.exists():
            return []
        candidates: list[dict[str, Any]] = []
        for path in sorted(self.staging_root.glob("*/candidate.json")):
            try:
                candidate = SkillCandidate.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.debug("skipping unreadable staged skill candidate %s: %s", path, exc)
                continue
            try:
                self._verify(candidate)
            except PermissionError as exc:
                logger.warning("skipping unverifiable staged skill candidate %s: %s", path, exc)
                continue
            candidates.append(candidate.model_dump())
        return candidates

    def get_candidate(self, skill_id: str) -> SkillCandidate:
        path = self._candidate_path(skill_id)
        if not path.exists():
            raise KeyError(skill_id)
        candidate = SkillCandidate.model_validate_json(path.read_text(encoding="utf-8"))
        self._verify(candidate)
        return candidate

    def _verify(self, candidate: SkillCandidate) -> None:
        if self.verifier is None:
            # Nothing was ever signed in this deployment, so there is nothing to
            # check. `candidate.signed` still records which it was.
            return
        envelope = candidate.envelope
        if not envelope or envelope.get("signed") is False:
            raise PermissionError(f"staged skill candidate {candidate.skill_id} carries no signature")
        try:
            payload = self.verifier(envelope)
        except Exception as exc:
            raise PermissionError(f"staged skill candidate {candidate.skill_id} failed signature check: {exc}") from exc
        for field, expected in (("contract_hash", candidate.contract_hash), ("trace_hash", candidate.trace_hash)):
            if payload.get(field) != expected:
                # A valid signature over a different candidate is still a forged
                # candidate, so the envelope has to match the artifact it sits in.
                raise PermissionError(
                    f"staged skill candidate {candidate.skill_id} does not match its signed envelope ({field})"
                )

    def candidate_dir(self, skill_id: str) -> Path:
        path = self._candidate_path(skill_id).parent
        if not path.exists():
            raise KeyError(skill_id)
        return path

    def _candidate_path(self, skill_id: str) -> Path:
        staging_root = self.staging_root.resolve()
        path = (self.staging_root / skill_id / "candidate.json").resolve()
        try:
            path.relative_to(staging_root)
        except ValueError as exc:
            raise KeyError(skill_id) from exc
        return path


def mesh_identity_signer(identity) -> Any:
    from app.mesh.models import PeerRecord
    from app.mesh.transport import make_envelope, verify_envelope

    def sign(payload: dict[str, Any]) -> dict[str, Any]:
        envelope = make_envelope(identity, payload, recipient_node_id="")
        peer = PeerRecord(node_id=identity.node_id, pubkey_b64=identity.pubkey_b64)
        verified_payload = verify_envelope(envelope, peer, expected_recipient_node_id="")
        if verified_payload != payload:
            raise RuntimeError("mesh skill candidate envelope verification round-trip failed")
        return envelope.model_dump(mode="json")

    return sign


def mesh_identity_verifier(identity) -> Verifier:
    """Counterpart to mesh_identity_signer: check what induction signed."""
    from app.mesh.models import PeerRecord, SignedEnvelope
    from app.mesh.transport import verify_envelope

    def verify(envelope: dict[str, Any]) -> dict[str, Any]:
        peer = PeerRecord(node_id=identity.node_id, pubkey_b64=identity.pubkey_b64)
        return verify_envelope(SignedEnvelope.model_validate(envelope), peer, expected_recipient_node_id="")

    return verify
