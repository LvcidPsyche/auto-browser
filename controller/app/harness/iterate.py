from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .contracts import TaskContract
from .converge import ConvergenceDecision, ConvergenceDetector
from .drift import SkillDriftMonitor
from .induce import SkillCandidate, SkillInducer
from .register import SkillStagingRegistry
from .trace import TraceEnvelope, TraceRecorder
from .verifier.base import VerificationResult, VerifierAdapter
from .verifier.programmatic import ProgrammaticVerifier

logger = logging.getLogger(__name__)


class HarnessAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    trace_path: str
    verification: VerificationResult
    created_at: float = Field(default_factory=time.time)


class HarnessRunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    contract_id: str
    contract_hash: str
    status: str = "created"
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    attempts: list[HarnessAttempt] = Field(default_factory=list)
    decision: ConvergenceDecision | None = None
    candidate: SkillCandidate | None = None
    strategy_path: str = ""
    model_tiers: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class HarnessRunStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.runs_root = self.root / "runs"
        self.staging_root = self.root / "skills" / "staging"
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.staging_root.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str) -> Path:
        return self.runs_root / run_id

    def save(self, record: HarnessRunRecord) -> None:
        record.updated_at = time.time()
        target = self.run_dir(record.id) / "run.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(target)

    def get(self, run_id: str) -> HarnessRunRecord:
        path = self.run_dir(run_id) / "run.json"
        if not path.exists():
            raise KeyError(run_id)
        return HarnessRunRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self, *, status: str | None = None, limit: int = 50) -> list[HarnessRunRecord]:
        records: list[HarnessRunRecord] = []
        for path in sorted(self.runs_root.glob("*/run.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                record = HarnessRunRecord.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.debug("skipping unreadable harness run record %s: %s", path, exc)
                continue
            if status and record.status != status:
                continue
            records.append(record)
            if len(records) >= limit:
                break
        return records


class HarnessService:
    def __init__(
        self,
        root: str | Path,
        *,
        verifier: VerifierAdapter | None = None,
        signer=None,
        model_tiers: dict[str, str] | None = None,
    ):
        self.store = HarnessRunStore(root)
        self.verifier = verifier or ProgrammaticVerifier()
        self.model_tiers = {key: value for key, value in (model_tiers or {}).items() if value}
        self.inducer = SkillInducer(self.store.staging_root, signer=signer)
        self.registry = SkillStagingRegistry(self.store.staging_root)
        self.drift_monitor = SkillDriftMonitor(self.registry, verifier=self.verifier)

    async def start_convergence(
        self,
        contract: TaskContract,
        *,
        mock_final_observation: dict[str, Any] | None = None,
        orchestrator=None,
        session_id: str | None = None,
        provider: str = "openai",
        max_attempts: int | None = None,
    ) -> HarnessRunRecord:
        run_id = uuid.uuid4().hex[:12]
        run_dir = self.store.run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "contract.json").write_text(contract.model_dump_json(indent=2), encoding="utf-8")
        strategy_path = run_dir / "strategy.md"
        strategy_path.write_text(_initial_strategy(contract), encoding="utf-8")
        record = HarnessRunRecord(
            id=run_id,
            contract_id=contract.id,
            contract_hash=contract.hash(),
            status="running",
            strategy_path=str(strategy_path),
            model_tiers=self.model_tiers,
        )
        self.store.save(record)
        attempt_budget = min(max_attempts or contract.budget.max_attempts, contract.budget.max_attempts)
        detector = ConvergenceDetector(
            required_successes=min(3, max(1, contract.budget.max_attempts)),
            max_attempts=attempt_budget,
        )
        results: list[VerificationResult] = []
        latest_trace: TraceEnvelope | None = None
        deadline = time.monotonic() + contract.budget.max_wall_seconds
        for attempt_index in range(1, attempt_budget + 1):
            trace_path = run_dir / f"attempt-{attempt_index}" / "trace.json"
            try:
                trace = await asyncio.wait_for(
                    self._run_attempt(
                        contract,
                        run_id=run_id,
                        attempt_index=attempt_index,
                        mock_final_observation=mock_final_observation,
                        orchestrator=orchestrator,
                        session_id=session_id,
                        provider=provider,
                    ),
                    timeout=_remaining_seconds(deadline),
                )
                latest_trace = trace
                trace.write_json(trace_path)
                verification = await asyncio.wait_for(
                    self.verifier.verify(contract, trace),
                    timeout=_remaining_seconds(deadline),
                )
            except asyncio.TimeoutError as exc:
                trace = _error_trace(contract, run_id=run_id, attempt_index=attempt_index, error=exc)
                trace.write_json(trace_path)
                verification = VerificationResult(
                    passed=None,
                    confidence=0.0,
                    notes="Harness budget exceeded: max_wall_seconds",
                    backend="harness",
                    details={"budget": "max_wall_seconds"},
                )
                record.attempts.append(
                    HarnessAttempt(index=attempt_index, trace_path=str(trace_path), verification=verification)
                )
                record.decision = ConvergenceDecision(
                    status="over_budget",
                    converged=False,
                    should_continue=False,
                    reason="max_wall_seconds budget exhausted",
                    successful_attempts=sum(1 for result in results if result.passed is True),
                    total_attempts=len(record.attempts),
                )
                record.status = "over_budget"
                record.notes.append(verification.notes)
                _append_reflexion(strategy_path, verification, attempt_index)
                self.store.save(record)
                break
            except Exception as exc:
                trace = _error_trace(contract, run_id=run_id, attempt_index=attempt_index, error=exc)
                trace.write_json(trace_path)
                verification = VerificationResult(
                    passed=None,
                    confidence=0.0,
                    notes=f"Harness attempt failed: {type(exc).__name__}",
                    backend="harness",
                    details={"error_type": type(exc).__name__},
                )
                record.attempts.append(
                    HarnessAttempt(index=attempt_index, trace_path=str(trace_path), verification=verification)
                )
                record.decision = ConvergenceDecision(
                    status="failed",
                    converged=False,
                    should_continue=False,
                    reason=f"attempt {attempt_index} failed",
                    successful_attempts=sum(1 for result in results if result.passed is True),
                    total_attempts=len(record.attempts),
                )
                record.status = "failed"
                record.notes.append(verification.notes)
                _append_reflexion(strategy_path, verification, attempt_index)
                self.store.save(record)
                break
            results.append(verification)
            record.attempts.append(
                HarnessAttempt(
                    index=attempt_index,
                    trace_path=str(trace_path),
                    verification=verification,
                )
            )
            decision = detector.decide(results)
            record.decision = decision
            record.status = decision.status
            if verification.passed is not True:
                _append_reflexion(strategy_path, verification, attempt_index)
            self.store.save(record)
            if not decision.should_continue:
                break

        if record.status == "converged" and latest_trace is not None and results:
            record.candidate = self.inducer.induce(
                contract=contract,
                trace=latest_trace,
                verification=results[-1],
                attempts=len(record.attempts),
                model_tiers=self.model_tiers,
            )
            record.notes.append("Staged skill candidate emitted; promotion requires review.")
            self.store.save(record)
        return record

    def get_status(self, run_id: str) -> dict[str, Any]:
        return self.store.get(run_id).model_dump(mode="json")

    def list_runs(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return [record.model_dump(mode="json") for record in self.store.list(status=status, limit=limit)]

    def get_trace(self, run_id: str, *, attempt_index: int | None = None) -> dict[str, Any]:
        record = self.store.get(run_id)
        if not record.attempts:
            raise KeyError(f"run has no attempts: {run_id}")
        selected_index = attempt_index or len(record.attempts)
        if selected_index < 1 or selected_index > len(record.attempts):
            raise KeyError(f"attempt {selected_index} not found for run: {run_id}")
        attempt = record.attempts[selected_index - 1]
        return TraceEnvelope.read_json(attempt.trace_path).model_dump(mode="json")

    def list_candidates(self) -> list[dict[str, Any]]:
        return self.registry.list_candidates()

    def get_candidate(self, skill_id: str) -> dict[str, Any]:
        return self.registry.get_candidate(skill_id).model_dump(mode="json")

    async def check_drift(self, skill_id: str) -> dict[str, Any]:
        return (await self.drift_monitor.check_candidate(skill_id)).model_dump(mode="json")

    async def check_all_drifts(self) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in await self.drift_monitor.check_all()]

    def graduate(self, run_id: str) -> dict[str, Any]:
        record = self.store.get(run_id)
        if record.status != "converged" or record.candidate is None:
            raise ValueError("Only converged runs with staged candidates can be graduated")
        return {
            "status": "staged",
            "message": "Candidate is staged only. Governed promotion is intentionally not automatic.",
            "candidate": record.candidate.model_dump(mode="json"),
        }

    async def _run_attempt(
        self,
        contract: TaskContract,
        *,
        run_id: str,
        attempt_index: int,
        mock_final_observation: dict[str, Any] | None,
        orchestrator,
        session_id: str | None,
        provider: str,
    ) -> TraceEnvelope:
        recorder = TraceRecorder(
            self.store.run_dir(run_id) / f"attempt-{attempt_index}",
            run_id=f"{run_id}-{attempt_index}",
            contract_hash=contract.hash(),
            nest_run_dir=False,
        )
        recorder.append("model_decision", {"strategy": "initial" if attempt_index == 1 else "reflexion"})
        if orchestrator is not None and session_id:
            result = await orchestrator.run(
                session_id=session_id,
                provider_name=provider,
                goal=contract.goal,
                max_steps=contract.budget.max_steps,
                context_hints=f"Harness contract {contract.id}; satisfy postconditions before done.",
                workflow_profile="governed",
            )
            trace = TraceEnvelope.from_agent_result(
                contract_hash=contract.hash(),
                result=result.model_dump() if hasattr(result, "model_dump") else result,
                run_id=f"{run_id}-{attempt_index}",
            )
            return trace

        final_observation = mock_final_observation or contract.metadata.get("mock_final_observation") or {}
        recorder.append("action", {"action": "mock_attempt", "goal": contract.goal}, step_idx=1)
        recorder.append("screenshot", {"available": False, "reason": "mock_mode"}, step_idx=1)
        return recorder.finalize(
            final_observation=final_observation,
            metadata={
                "mode": "mock",
                "attempt_index": attempt_index,
                "model_tiers": self.model_tiers,
            },
        )


def _initial_strategy(contract: TaskContract) -> str:
    return f"""# Strategy for {contract.id}

Goal: {contract.goal}

Start with the most deterministic path. Prefer stable selectors, accessible names, and direct network/API evidence when available.
"""


def _append_reflexion(path: Path, verification: VerificationResult, attempt_index: int) -> None:
    failed = ", ".join(verification.failed_postconditions) or "none"
    missing = ", ".join(verification.missing_evidence) or "none"
    note = (
        f"\n## Reflexion after attempt {attempt_index}\n\n"
        f"- verifier: {verification.backend}\n"
        f"- failed postconditions: {failed}\n"
        f"- missing evidence: {missing}\n"
        f"- next mutation: harden selectors or seek a more deterministic evidence source.\n"
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(note)


def _error_trace(contract: TaskContract, *, run_id: str, attempt_index: int, error: Exception) -> TraceEnvelope:
    from .trace import TraceEvent

    event = TraceEvent(
        run_id=f"{run_id}-{attempt_index}",
        step_idx=attempt_index,
        event_type="error",
        payload={
            "error_type": type(error).__name__,
            "message": "attempt failed",
        },
    ).with_hash()
    return TraceEnvelope(
        run_id=f"{run_id}-{attempt_index}",
        contract_hash=contract.hash(),
        events=[event],
        final_observation={},
        metadata={"mode": "error", "attempt_index": attempt_index},
    )


def _remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise asyncio.TimeoutError
    return remaining
