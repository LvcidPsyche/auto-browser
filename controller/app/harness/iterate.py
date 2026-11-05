from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from ..session_ownership import may_use, verified_operator
from .contracts import RUN_ID_PATTERN, TaskContract
from .converge import ConvergenceDecision, ConvergenceDetector
from .drift import SkillDriftMonitor
from .induce import SkillCandidate, SkillInducer
from .register import SkillStagingRegistry
from .trace import TraceEnvelope, TraceIntegrityError, TraceRecorder
from .verifier.base import VerificationResult, VerifierAdapter
from .verifier.programmatic import ProgrammaticVerifier

logger = logging.getLogger(__name__)

# Validation context key for a run record read back from run.json.
_STORED_RECORD = "stored_record"
_RUN_ID_RE = re.compile(RUN_ID_PATTERN)
# The event loop may fire a wait_for timer a clock tick early (15 ms on Windows); within this much of the deadline the
# wall-clock budget is what ran out.
_DEADLINE_SLACK_SECONDS = 0.05


class HarnessAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    trace_path: str
    verification: VerificationResult
    created_at: float = Field(default_factory=time.time)
    # Set once the verification has been scrubbed, and kept when the run is saved. A run.json written by 1.14.x
    # has no marker (its verification is raw), so it is scrubbed when read; one this version wrote is not scrubbed
    # again, so a scrubber that fails later cannot drop the run from a listing.
    scrubbed: bool = False
    # The hash of the trace as written to trace_path (the scrubbed copy), checked when the trace is read back so an
    # edit to its observation or extracted data is caught. Empty for a run recorded before this field existed.
    trace_hash: str = ""

    @model_validator(mode="after")
    def _scrub_verification(self, info: ValidationInfo) -> "HarnessAttempt":
        if self.scrubbed:
            return self
        stored = bool(info.context and info.context.get(_STORED_RECORD))
        try:
            self.verification = self.verification.scrubbed()
        except Exception as exc:
            if not stored:
                raise
            # A record that is already on disk: keep the run, with the verdict and nothing else of its result.
            logger.warning(
                "harness attempt %s: its stored verification was withheld (%s)", self.index, type(exc).__name__
            )
            self.verification = VerificationResult(
                passed=self.verification.passed,
                confidence=self.verification.confidence,
                notes="verification withheld: it could not be scrubbed",
                backend="harness",
            )
        self.scrubbed = True
        return self


class HarnessRunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    contract_id: str
    contract_hash: str
    status: str = "created"
    # The token-verified operator who started the run; None when the caller had
    # no proven identity, and for runs recorded before owners existed.
    owner: str | None = None
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
        # Runs this process is driving (ID to owner), and when it began: the restart sweep touches neither.
        self._active: dict[str, str | None] = {}
        self._active_lock = threading.Lock()
        self._started_at = time.time()

    def begin_run(self, run_id: str, owner: str | None, *, max_owned: int = 0) -> bool:
        """Mark a new run active, unless `owner` already has `max_owned` runs stored or starting (0: no cap).

        The count and the mark are one step under the store's lock, and a run counts from here, before its
        run.json is written, so two starts at once cannot both pass a cap that only one of them fits under.
        """
        with self._active_lock:
            if max_owned > 0:
                starting = {run for run, who in self._active.items() if who == owner}
                if len(self._owned_runs(owner) | starting) >= max_owned:
                    return False
            self._active[run_id] = owner
            return True

    def mark_active(self, run_id: str, owner: str | None = None) -> None:
        with self._active_lock:
            self._active[run_id] = owner

    def release(self, run_id: str) -> None:
        with self._active_lock:
            self._active.pop(run_id, None)

    def mark_stale_running(self) -> int:
        """Flip every run an earlier process left `running` to `interrupted`; returns how many.

        Run once at startup, before requests are served. A run this process is driving, or created since it
        started, is left alone. A record that cannot be rewritten is logged and left `running`: one bad record must
        not stop the harness from starting.
        """
        flipped = 0
        for path in self.runs_root.glob("*/run.json"):
            try:
                record = _read_record(path)
            except Exception as exc:
                logger.debug("skipping unreadable harness run record %s: %s", path, exc)
                continue
            if record.status != "running" or record.id in self._active or record.created_at >= self._started_at:
                continue
            record.status = "interrupted"
            record.notes.append("Harness run interrupted by a controller restart.")
            try:
                self.save(record)
            except (OSError, ValueError) as exc:
                logger.warning("could not mark harness run %s interrupted; it still reads running: %s", record.id, exc)
                continue
            flipped += 1
        return flipped

    def _owned_runs(self, owner: str | None) -> set[str]:
        """The IDs of the stored runs that belong to `owner` (None: the runs nobody owns)."""
        owned: set[str] = set()
        for path in self.runs_root.glob("*/run.json"):
            try:
                stored = json.loads(path.read_text(encoding="utf-8")).get("owner")
            except (OSError, ValueError):
                continue
            if stored == owner:
                owned.add(path.parent.name)
        return owned

    def prune(self, max_age_days: float) -> int:
        """Remove run and staged-candidate directories untouched for `max_age_days`; returns how many.

        Age is the modification time of run.json (candidate.json for a staged skill, the directory for anything
        else, such as a leftover .tmp-* build). A run this process is driving is never removed. 0 or less keeps
        everything. A directory that cannot be removed is logged and left; this never raises.
        """
        if max_age_days <= 0:
            return 0
        cutoff = time.time() - max_age_days * 86400
        removed = 0
        candidates: list[tuple[Path, str]] = []
        for root, marker in ((self.runs_root, "run.json"), (self.staging_root, "candidate.json")):
            try:
                candidates += [(path, marker) for path in root.iterdir()]
            except OSError as exc:
                logger.warning("harness prune could not list %s: %s", root, exc)
        for directory, marker in candidates:
            if not directory.is_dir() or directory.name in self._active:
                continue
            try:
                marker_path = directory / marker
                newest = max(directory.stat().st_mtime, marker_path.stat().st_mtime if marker_path.exists() else 0.0)
                if newest >= cutoff:
                    continue
                shutil.rmtree(directory)
                removed += 1
            except OSError as exc:
                logger.warning("harness prune could not remove %s: %s", directory, exc)
        return removed

    def run_dir(self, run_id: str) -> Path:
        if not _RUN_ID_RE.fullmatch(run_id):
            raise ValueError(f"invalid harness run id: {run_id!r}")
        return self.runs_root / run_id

    def save(self, record: HarnessRunRecord) -> None:
        record.updated_at = time.time()
        target = self.run_dir(record.id) / "run.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(target)

    def get(self, run_id: str) -> HarnessRunRecord:
        if not _RUN_ID_RE.fullmatch(run_id):
            raise KeyError(run_id)
        path = self.run_dir(run_id) / "run.json"
        if not path.exists():
            raise KeyError(run_id)
        return _read_record(path)

    def list(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        visible: Callable[[HarnessRunRecord], bool] | None = None,
    ) -> list[HarnessRunRecord]:
        records: list[HarnessRunRecord] = []
        for path in sorted(self.runs_root.glob("*/run.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                record = _read_record(path)
            except Exception as exc:
                logger.debug("skipping unreadable harness run record %s: %s", path, exc)
                continue
            if status and record.status != status:
                continue
            if visible is not None and not visible(record):
                continue
            records.append(record)
            if len(records) >= limit:
                break
        return records


def _read_record(path: Path) -> HarnessRunRecord:
    return HarnessRunRecord.model_validate_json(path.read_text(encoding="utf-8"), context={_STORED_RECORD: True})


class HarnessService:
    def __init__(
        self,
        root: str | Path,
        *,
        verifier: VerifierAdapter | None = None,
        signer=None,
        envelope_verifier=None,
        model_tiers: dict[str, str] | None = None,
        max_runs_per_operator: int = 0,
    ):
        self.store = HarnessRunStore(root)
        self.max_runs_per_operator = max_runs_per_operator
        self.verifier = verifier or ProgrammaticVerifier()
        self.model_tiers = {key: value for key, value in (model_tiers or {}).items() if value}
        self.inducer = SkillInducer(self.store.staging_root, signer=signer)
        # Signing without a matching check is decoration; the registry gets the
        # counterpart to whatever the inducer signs with.
        self.registry = SkillStagingRegistry(self.store.staging_root, verifier=envelope_verifier)
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
        owner = verified_operator()
        run_id = uuid.uuid4().hex[:12]
        if not self.store.begin_run(run_id, owner, max_owned=self.max_runs_per_operator):
            raise ValueError(
                f"harness run limit reached: {self.max_runs_per_operator} stored runs for this operator "
                "(HARNESS_MAX_RUNS_PER_OPERATOR); old runs are removed after HARNESS_RETENTION_DAYS"
            )
        try:
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
                owner=owner,
                strategy_path=str(strategy_path),
                model_tiers=self.model_tiers,
            )
            self.store.save(record)
        except BaseException:
            # No record was saved, so nothing reads "running": just give the place back.
            self.store.release(run_id)
            raise
        try:
            await self._drive(
                record,
                contract,
                run_dir=run_dir,
                strategy_path=strategy_path,
                mock_final_observation=mock_final_observation,
                orchestrator=orchestrator,
                session_id=session_id,
                provider=provider,
                max_attempts=max_attempts,
            )
        except BaseException as exc:
            # A cancel (CancelledError is a BaseException) or a crash must not leave the run reading "running".
            self._close_unfinished(record, exc)
            raise
        finally:
            self.store.release(run_id)
        return record

    async def _drive(
        self,
        record: HarnessRunRecord,
        contract: TaskContract,
        *,
        run_dir: Path,
        strategy_path: Path,
        mock_final_observation: dict[str, Any] | None,
        orchestrator,
        session_id: str | None,
        provider: str,
        max_attempts: int | None,
    ) -> None:
        run_id = record.id
        attempt_budget = min(max_attempts or contract.budget.max_attempts, contract.budget.max_attempts)
        detector = ConvergenceDetector(
            required_successes=min(3, attempt_budget),
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
            except Exception as exc:
                self._stop_on_error(
                    record,
                    contract,
                    run_id=run_id,
                    attempt_index=attempt_index,
                    trace_path=trace_path,
                    strategy_path=strategy_path,
                    error=exc,
                    deadline=deadline,
                    successes=sum(1 for result in results if result.passed is True),
                )
                break
            results.append(verification)
            record.attempts.append(
                HarnessAttempt(
                    index=attempt_index,
                    trace_path=str(trace_path),
                    verification=verification,
                    trace_hash=trace.scrubbed().trace_hash,
                )
            )
            decision = detector.decide(results)
            record.decision = decision
            record.status = decision.status
            if verification.passed is not True:
                # strategy.md is a stored file: the attempt's scrubbed copy, not the verifier's raw result.
                _append_reflexion(strategy_path, record.attempts[-1].verification, attempt_index)
            self.store.save(record)
            if not decision.should_continue:
                break

        if record.status == "converged" and latest_trace is not None and results:
            try:
                record.candidate = self.inducer.induce(
                    contract=contract,
                    trace=latest_trace,
                    verification=results[-1],
                    attempts=len(record.attempts),
                    model_tiers=self.model_tiers,
                    owner=record.owner,
                )
            except Exception as exc:
                # The inducer cleans up after itself; the run must not stay "converged" with no candidate.
                logger.exception("harness run %s: staging the skill candidate failed", run_id)
                record.status = "failed"
                record.decision = ConvergenceDecision(
                    status="failed",
                    converged=False,
                    should_continue=False,
                    reason=f"staging the skill candidate failed: {type(exc).__name__}",
                    successful_attempts=record.decision.successful_attempts if record.decision else 0,
                    total_attempts=len(record.attempts),
                )
                record.notes.append(f"Skill candidate staging failed: {type(exc).__name__}")
                self.store.save(record)
                return
            record.notes.append("Staged skill candidate emitted; promotion requires review.")
            self.store.save(record)

    def _close_unfinished(self, record: HarnessRunRecord, error: BaseException) -> None:
        if record.status != "running":
            return
        if isinstance(error, Exception):
            record.status = "failed"
            record.notes.append(f"Harness run failed: {type(error).__name__}")
        else:
            record.status = "cancelled"
            record.notes.append("Harness run cancelled before it finished.")
        try:
            self.store.save(record)
        except Exception:
            logger.exception("harness run %s: could not record its %s end", record.id, record.status)

    def _stop_on_error(
        self,
        record: HarnessRunRecord,
        contract: TaskContract,
        *,
        run_id: str,
        attempt_index: int,
        trace_path: Path,
        strategy_path: Path,
        error: Exception,
        deadline: float,
        successes: int,
    ) -> None:
        """End the run on an attempt that raised: over budget only when the wall-clock deadline is what ran out.

        A TimeoutError from inside the attempt or the verifier (an orchestrator or UV timeout) with budget left
        is an ordinary failed attempt.
        """
        over_budget = isinstance(error, asyncio.TimeoutError) and time.monotonic() >= deadline - _DEADLINE_SLACK_SECONDS
        trace = _error_trace(contract, run_id=run_id, attempt_index=attempt_index, error=error)
        trace.write_json(trace_path)
        if over_budget:
            verification = VerificationResult(
                passed=None,
                confidence=0.0,
                notes="Harness budget exceeded: max_wall_seconds",
                backend="harness",
                details={"budget": "max_wall_seconds"},
            )
            status, reason = "over_budget", "max_wall_seconds budget exhausted"
        else:
            verification = VerificationResult(
                passed=None,
                confidence=0.0,
                notes=f"Harness attempt failed: {type(error).__name__}",
                backend="harness",
                details={"error_type": type(error).__name__},
            )
            status, reason = "failed", f"attempt {attempt_index} failed"
        record.attempts.append(
            HarnessAttempt(
                index=attempt_index,
                trace_path=str(trace_path),
                verification=verification,
                trace_hash=trace.scrubbed().trace_hash,
            )
        )
        record.decision = ConvergenceDecision(
            status=status,
            converged=False,
            should_continue=False,
            reason=reason,
            successful_attempts=successes,
            total_attempts=len(record.attempts),
        )
        record.status = status
        record.notes.append(verification.notes)
        _append_reflexion(strategy_path, verification, attempt_index)
        self.store.save(record)

    def _get_owned(self, run_id: str) -> HarnessRunRecord:
        """The run, refused to every operator but its owner (unowned runs are open)."""
        record = self.store.get(run_id)
        if not may_use(record.owner):
            raise PermissionError(f"harness run {run_id} belongs to another operator")
        return record

    def get_status(self, run_id: str) -> dict[str, Any]:
        return self._get_owned(run_id).model_dump(mode="json")

    def list_runs(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        records = self.store.list(status=status, limit=limit, visible=lambda record: may_use(record.owner))
        return [record.model_dump(mode="json") for record in records]

    def get_trace(self, run_id: str, *, attempt_index: int | None = None) -> dict[str, Any]:
        record = self._get_owned(run_id)
        if not record.attempts:
            raise KeyError(f"run has no attempts: {run_id}")
        selected_index = attempt_index or len(record.attempts)
        if selected_index < 1 or selected_index > len(record.attempts):
            raise KeyError(f"attempt {selected_index} not found for run: {run_id}")
        attempt = record.attempts[selected_index - 1]
        trace = TraceEnvelope.read_json(attempt.trace_path)
        if attempt.trace_hash and trace.trace_hash != attempt.trace_hash:
            raise TraceIntegrityError(
                f"trace for attempt {selected_index} of run {run_id} changed since it was written"
            )
        return trace.model_dump(mode="json")

    def list_candidates(self) -> list[dict[str, Any]]:
        return self.registry.list_candidates()

    def get_candidate(self, skill_id: str) -> dict[str, Any]:
        return self.registry.get_candidate(skill_id).model_dump(mode="json")

    async def check_drift(self, skill_id: str) -> dict[str, Any]:
        return (await self.drift_monitor.check_candidate(skill_id)).model_dump(mode="json")

    async def check_all_drifts(self) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in await self.drift_monitor.check_all()]

    def graduate(self, run_id: str) -> dict[str, Any]:
        record = self._get_owned(run_id)
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
        # The mock attempt's decision and action are made up, not a model's or a browser's: they say so.
        live = orchestrator is not None and bool(session_id)
        strategy = "initial" if attempt_index == 1 else "reflexion"
        recorder.append("model_decision", {"strategy": strategy, "simulated": not live})
        if live:
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
        recorder.append("action", {"action": "mock_attempt", "goal": contract.goal, "simulated": True}, step_idx=1)
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
