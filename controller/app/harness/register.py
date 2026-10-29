from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Callable

from ..session_ownership import may_use
from .induce import SIGNED_SKILL_FILES, SkillCandidate
from .trace import hash_payload

logger = logging.getLogger(__name__)

# Takes a stored envelope, returns the payload it attests to, raises if invalid.
Verifier = Callable[[dict[str, Any]], dict[str, Any]]

# The files induction writes into a candidate's directory.
CANDIDATE_FILES = (
    *SIGNED_SKILL_FILES,
    "provenance.json",
    "envelope.json",
    "contract.json",
    "trace.json",
    "candidate.json",
)


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

    A candidate staged by a token-verified operator is that operator's: other
    operators do not see it listed and are refused when they ask for it by id.
    Unowned candidates are open, as before.
    """

    def __init__(self, staging_root: str | Path, verifier: Verifier | None = None):
        self.staging_root = Path(staging_root)
        self.verifier = verifier

    def list_candidates(self) -> list[dict[str, Any]]:
        if not self.staging_root.exists():
            return []
        candidates: list[dict[str, Any]] = []
        seen: set[Path] = set()
        for listed in sorted(self.staging_root.glob("*/candidate.json")):
            try:
                path = self._contained(listed)
            except KeyError:
                logger.warning("skipping staged skill candidate %s: it resolves outside the staging root", listed)
                continue
            if path in seen:
                # A link inside staging to another candidate's directory: same candidate.
                continue
            seen.add(path)
            try:
                candidate = SkillCandidate.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.debug("skipping unreadable staged skill candidate %s: %s", path, exc)
                continue
            if not may_use(candidate.owner):
                continue
            try:
                self._verify(candidate, path.parent)
            except PermissionError as exc:
                logger.warning("skipping unverifiable staged skill candidate %s: %s", path, exc)
                continue
            candidates.append(_with_files_of(candidate, path.parent).model_dump())
        return candidates

    def get_candidate(self, skill_id: str) -> SkillCandidate:
        path = self._candidate_path(skill_id)
        if not path.exists():
            raise KeyError(skill_id)
        candidate = SkillCandidate.model_validate_json(path.read_text(encoding="utf-8"))
        if not may_use(candidate.owner):
            raise PermissionError(f"staged skill candidate {candidate.skill_id} belongs to another operator")
        self._verify(candidate, path.parent)
        return _with_files_of(candidate, path.parent)

    def _verify(self, candidate: SkillCandidate, directory: Path) -> None:
        """Check `candidate`, as read from `directory`, against its signed envelope."""
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
        signed_claims = {
            "contract_hash": (payload.get("contract_hash"), candidate.contract_hash),
            "trace_hash": (payload.get("trace_hash"), candidate.trace_hash),
            "skill_id": (payload.get("skill_id"), candidate.skill_id),
            "verifier passed": ((payload.get("verifier") or {}).get("passed"), candidate.verifier_passed),
            "simulated": ((payload.get("metadata") or {}).get("simulated", False), candidate.simulated),
            "owner": ((payload.get("metadata") or {}).get("owner"), candidate.owner),
        }
        for field, (signed, stored) in signed_claims.items():
            if signed != stored:
                # A valid signature over a different candidate is still a forged
                # candidate, so the envelope has to match the artifact it sits in.
                raise PermissionError(
                    f"staged skill candidate {candidate.skill_id} does not match its signed envelope ({field})"
                )
        if directory.name != candidate.skill_id:
            # A candidate.json copied into another directory is not that directory's skill.
            raise PermissionError(
                f"staged skill candidate {candidate.skill_id} was read from {directory.name}, not its own directory"
            )
        signed_candidate_hash = payload.get("candidate_hash")
        if not isinstance(signed_candidate_hash, str):
            raise PermissionError(
                f"staged skill candidate {candidate.skill_id} was signed without a candidate hash; induce it again"
            )
        if signed_candidate_hash != hash_payload(candidate.model_dump(mode="json", exclude={"files", "envelope"})):
            raise PermissionError(
                f"staged skill candidate {candidate.skill_id} does not match its signed envelope (candidate.json)"
            )
        self._verify_files(candidate, payload.get("files_sha256"), directory)

    def _verify_files(self, candidate: SkillCandidate, signed_hashes: Any, directory: Path) -> None:
        """The files a reviewer reads and promotes are the ones that were signed."""
        if not isinstance(signed_hashes, dict) or set(signed_hashes) != set(SIGNED_SKILL_FILES):
            raise PermissionError(
                f"staged skill candidate {candidate.skill_id} was signed without hashes of its files; induce it again"
            )
        for name, digest in signed_hashes.items():
            path = directory / name
            if path.resolve().parent != directory.resolve():
                raise PermissionError(f"staged skill candidate {candidate.skill_id} has a {name} outside its directory")
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise PermissionError(
                    f"staged skill candidate {candidate.skill_id} has a {name} that is not the one it was signed with"
                )

    def candidate_dir(self, skill_id: str) -> Path:
        path = self._candidate_path(skill_id).parent
        if not path.exists():
            raise KeyError(skill_id)
        return path

    def _candidate_path(self, skill_id: str) -> Path:
        return self._contained(self.staging_root / skill_id / "candidate.json")

    def _contained(self, path: Path) -> Path:
        """`path`, resolved through any link, if it stays inside the staging root."""
        resolved = path.resolve()
        try:
            resolved.relative_to(self.staging_root.resolve())
        except ValueError as exc:
            raise KeyError(path.parent.name) from exc
        return resolved


def _with_files_of(candidate: SkillCandidate, directory: Path) -> SkillCandidate:
    """The candidate as served: its `files` map comes from the directory it was read from.

    The stored map is unsigned, and a reviewer is shown these paths.
    """
    return candidate.model_copy(update={"files": {name: str(directory / name) for name in CANDIDATE_FILES}})


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
