from __future__ import annotations

import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .contracts import TaskContract
from .register import SkillStagingRegistry
from .trace import TraceEnvelope
from .verifier.base import VerificationResult, VerifierAdapter
from .verifier.programmatic import ProgrammaticVerifier


class DriftCheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: str
    status: str
    checked_at: float = Field(default_factory=time.time)
    contract_hash: str = ""
    trace_hash: str = ""
    verification: VerificationResult | None = None
    notes: list[str] = Field(default_factory=list)

    @field_validator("verification")
    @classmethod
    def _scrub_verification(cls, value: VerificationResult | None) -> VerificationResult | None:
        # The result is written to drift.json; the verifier's result holds the URL it saw.
        return value.scrubbed() if value is not None else None


class SkillDriftMonitor:
    def __init__(
        self,
        registry: SkillStagingRegistry,
        *,
        verifier: VerifierAdapter | None = None,
    ) -> None:
        self.registry = registry
        self.verifier = verifier or ProgrammaticVerifier()

    async def check_candidate(self, skill_id: str) -> DriftCheckResult:
        try:
            candidate = self.registry.get_candidate(skill_id)
        except KeyError:
            return DriftCheckResult(skill_id=skill_id, status="missing", notes=["candidate not found"])

        candidate_dir = self.registry.candidate_dir(candidate.skill_id)
        try:
            contract = TaskContract.model_validate_json(_read_artifact(candidate_dir, "contract.json"))
            trace = TraceEnvelope.read_json(_artifact_path(candidate_dir, "trace.json"))
        except Exception as exc:
            result = DriftCheckResult(
                skill_id=skill_id,
                status="degraded",
                notes=[f"candidate artifacts unreadable: {type(exc).__name__}"],
            )
            _write_result(candidate_dir, result)
            return result

        notes: list[str] = []
        contract_hash = contract.hash()
        trace_hash = trace.trace_hash
        if contract_hash != candidate.contract_hash:
            notes.append("contract hash differs from candidate provenance")
        if trace_hash != candidate.trace_hash:
            notes.append("trace hash differs from candidate provenance")

        verification = await self.verifier.verify(contract, trace)
        if verification.passed is True and not notes:
            status = "healthy"
        elif verification.passed is None and not notes:
            status = "unverified"
        else:
            status = "degraded"
        if verification.notes:
            notes.append(verification.notes)

        result = DriftCheckResult(
            skill_id=skill_id,
            status=status,
            contract_hash=contract_hash,
            trace_hash=trace_hash,
            verification=verification,
            notes=notes,
        )
        _write_result(candidate_dir, result)
        return result

    async def check_all(self) -> list[DriftCheckResult]:
        results: list[DriftCheckResult] = []
        for item in self.registry.list_candidates():
            skill_id = str(item.get("skill_id") or "")
            if skill_id:
                results.append(await self.check_candidate(skill_id))
        return results


def _artifact_path(candidate_dir: Path, name: str) -> Path:
    root = candidate_dir.resolve()
    path = (candidate_dir / name).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise FileNotFoundError(name) from exc
    if not path.exists():
        raise FileNotFoundError(name)
    return path


def _read_artifact(candidate_dir: Path, name: str) -> str:
    return _artifact_path(candidate_dir, name).read_text(encoding="utf-8")


def _write_result(candidate_dir: Path, result: DriftCheckResult) -> Path:
    candidate_dir.mkdir(parents=True, exist_ok=True)
    target = candidate_dir / "drift.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(target)
    return target
