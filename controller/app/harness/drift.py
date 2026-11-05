from __future__ import annotations

import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .contracts import Postcondition, TaskContract
from .register import SkillStagingRegistry
from .trace import TraceEnvelope, _scrub_string, _scrub_text, _scrub_url, _scrub_url_and_path, _scrub_urls_only
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

        try:
            expected = _expecting_scrubbed_text(contract)
        except Exception as exc:
            result = DriftCheckResult(
                skill_id=skill_id,
                status="unverified",
                contract_hash=contract_hash,
                trace_hash=trace_hash,
                notes=[*notes, f"expected text could not be scrubbed: {type(exc).__name__}"],
            )
            _write_result(candidate_dir, result)
            return result

        verification = await self.verifier.verify(expected, trace)
        if verification.passed is True and not notes:
            status = "healthy"
        elif verification.passed is None and not notes:
            status = "unverified"
        else:
            status = "degraded"
        if verification.notes:
            # drift.json and the drift responses hold the notes: the scrubbed copy, as for `verification`.
            notes.append(_scrub_string(verification.notes))

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


def _scrub_url_contains(value: str) -> str:
    """A `url_contains` value scrubbed the way the part of the stored URL it should match was.

    The stored URL has its query and fragment rewritten and its host and path read as text. A value with a `?` or `#`
    is a URL or its tail, and goes through both; one with neither but an `=` (`email=...`, `&email=...`) is a piece of
    a query, and is scrubbed as one; anything else (a path, bare text) is scrubbed as text, as the stored path was.
    """
    if "?" in value or "#" in value:
        return _scrub_url_and_path(value)
    if "=" in value:
        scrubbed = _scrub_url("?" + value)[1:]
        # A scrubber that returned nothing would turn the expectation into "" (which every URL contains): keep it.
        return scrubbed or value
    return _scrub_text(value)


# How the stored trace scrubs the field each kind of postcondition reads. `url_matches` is a regex, left alone.
_STORED_TEXT_SCRUBBERS = {
    "url_contains": _scrub_url_contains,
    "text_contains": _scrub_string,
    "dom_contains": _scrub_string,
    "network_response_shape": _scrub_string,
    "extracted_data_schema": _scrub_urls_only,
}


def _expecting_scrubbed_text(contract: TaskContract) -> TaskContract:
    """The contract with its expected strings scrubbed the way the stored trace was.

    The candidate keeps a scrubbed trace, so an email the live run matched is "[REDACTED]" there. Only the
    copy handed to the verifier changes; the contract hash is taken from the original.
    """
    postconditions = [
        item.model_copy(update={"value": _scrubbed_or_as_written(item)})
        if item.kind in _STORED_TEXT_SCRUBBERS
        else item
        for item in contract.postconditions
    ]
    return contract.model_copy(update={"postconditions": postconditions})


def _scrubbed_or_as_written(item: Postcondition) -> str:
    """The value scrubbed, or as written if a non-empty value scrubbed to nothing.

    "" is in every page, so it would pass the check whatever the page shows. A failed scrubber gives it: scrub_url
    catches the PII scrubber's error and returns the value's path, which is "" for a `?...` or `#...` value.
    """
    return _STORED_TEXT_SCRUBBERS[item.kind](item.value) or item.value


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
