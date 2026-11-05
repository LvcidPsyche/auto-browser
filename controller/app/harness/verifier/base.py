from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ..contracts import TaskContract


class VerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    passed: bool | None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    failed_postconditions: list[str] = Field(default_factory=list)
    satisfied_postconditions: list[str] = Field(default_factory=list)
    forbidden_state_hits: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    notes: str = ""
    backend: str = "programmatic"
    details: dict[str, Any] = Field(default_factory=dict)

    def scrubbed(self) -> "VerificationResult":
        """A copy that is safe to store: the trace's scrubbing applied to every field.

        The verifier copies raw observation values into its result (the URL, a matched condition's
        text), so a result is scrubbed before it is written to run.json, drift.json or a candidate's
        provenance and envelope. A caller that keeps the result in process may keep the original.
        """
        from ..trace import _redact_sensitive

        return type(self).model_validate(_redact_sensitive(self.model_dump(mode="json")))


class VerifierAdapter(Protocol):
    async def verify(self, contract: TaskContract, trace: Any) -> VerificationResult: ...


class EnsembleVerifier:
    def __init__(self, verifiers: list[VerifierAdapter]):
        self.verifiers = verifiers

    async def verify(self, contract: TaskContract, trace: Any) -> VerificationResult:
        results = [await verifier.verify(contract, trace) for verifier in self.verifiers]
        decisive = [result for result in results if result.passed is not None]
        if not decisive:
            return VerificationResult(
                passed=None,
                confidence=0.0,
                notes="No verifier produced a decisive result.",
                backend="ensemble",
                details={"results": [result.model_dump() for result in results]},
            )
        if len(self.verifiers) > 1 and len(decisive) < 2:
            return VerificationResult(
                passed=None,
                confidence=sum(result.confidence for result in decisive) / len(decisive),
                notes="Insufficient decisive verifier votes; abstaining.",
                backend="ensemble",
                details={"results": [result.model_dump() for result in results]},
            )
        pass_votes = sum(1 for result in decisive if result.passed is True)
        fail_votes = sum(1 for result in decisive if result.passed is False)
        if pass_votes >= 2 or (len(decisive) == 1 and pass_votes == 1):
            passed: bool | None = True
        elif fail_votes >= 2 or (len(decisive) == 1 and fail_votes == 1):
            passed = False
        else:
            passed = None
        confidence = sum(result.confidence for result in decisive) / len(decisive)
        failed: list[str] = []
        satisfied: list[str] = []
        forbidden: list[str] = []
        missing: list[str] = []
        for result in results:
            failed.extend(result.failed_postconditions)
            satisfied.extend(result.satisfied_postconditions)
            forbidden.extend(result.forbidden_state_hits)
            missing.extend(result.missing_evidence)
        return VerificationResult(
            passed=passed,
            confidence=confidence,
            failed_postconditions=sorted(set(failed)),
            satisfied_postconditions=sorted(set(satisfied)),
            forbidden_state_hits=sorted(set(forbidden)),
            missing_evidence=sorted(set(missing)),
            notes="Ensemble verification complete." if passed is not None else "Verifier disagreement; abstaining.",
            backend="ensemble",
            details={"results": [result.model_dump() for result in results]},
        )
