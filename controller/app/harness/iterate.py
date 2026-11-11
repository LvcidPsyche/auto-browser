from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from ..session_ownership import may_use, verified_operator
from ..utils import holds_lone_surrogate, run_to_completion, without_lone_surrogates
from .contracts import RUN_ID_PATTERN, TaskContract
from .converge import ConvergenceDecision, ConvergenceDetector
from .drift import SkillDriftMonitor
from .induce import SkillCandidate, SkillInducer, skill_id_for
from .register import CANDIDATE_FILES, SkillStagingRegistry
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
# How often a converged run tries the staging lock again while retention's prune holds it (one candidate's removal):
# it waits on the event loop without holding the loop.
_STAGING_LOCK_RETRY_SECONDS = 0.01


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
    # The HarnessRunStore.generation of the process that created the run; None for a run saved before it existed.
    generation: str | None = None
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
        # Runs this process is driving (ID to owner), and this process's generation, written on the runs it creates:
        # the restart sweep touches neither. A generation, not a start time: a clock stepped back across a restart
        # made an earlier process's run look created by this one.
        self._active: dict[str, str | None] = {}
        self._active_lock = threading.Lock()
        self.generation = uuid.uuid4().hex
        # Shared with the SkillInducer: prune holds it from a candidate's age check through its rename to a hidden name,
        # staging around its rename into place, so a candidate re-staged in between is never removed as old, and
        # candidate reads (HarnessService, the drift monitor) around theirs, so none meets a candidate half replaced or
        # half removed. Never held for a removal, so a read on the event loop waits at most for one rename.
        self.staging_lock = threading.Lock()

    def begin_run(self, run_id: str, owner: str | None, *, max_owned: int = 0) -> bool:
        """Mark a new run active, unless `owner` already has `max_owned` runs stored or starting (0: no cap).

        The stored runs are read holding no lock, so the step is not atomic between callers: HarnessService
        serialises its admissions (and reads the stored runs in a worker thread) instead of calling this.
        """
        counted = self.starting_runs(owner) | (self._owned_runs(owner) if max_owned > 0 else set())
        return self.reserve(run_id, owner, counted, max_owned=max_owned)

    def starting_runs(self, owner: str | None) -> set[str]:
        """The IDs of the runs this process is driving or starting for `owner`."""
        with self._active_lock:
            return {run for run, who in self._active.items() if who == owner}

    def reserve(self, run_id: str, owner: str | None, counted: set[str], *, max_owned: int = 0) -> bool:
        """Mark a new run active unless `counted` (with `owner`'s runs starting now) reaches `max_owned` (0: no cap).

        A run counts from here, before its run.json is written, so a start that holds its place is counted by the
        next one.
        """
        with self._active_lock:
            if max_owned > 0:
                starting = {run for run, who in self._active.items() if who == owner}
                if len(counted | starting) >= max_owned:
                    return False
            self._active[run_id] = owner
            return True

    def release(self, run_id: str) -> None:
        with self._active_lock:
            self._active.pop(run_id, None)

    def mark_stale_running(self) -> int:
        """Flip every run an earlier process left `running` to `interrupted`; returns how many.

        Run once at startup, before requests are served. A run this process is driving, or one it created (its
        generation is this process's), is left alone; every other `running` record is flipped, one saved without a
        generation included. A record that cannot be rewritten is logged and left `running`: one bad record must not
        stop the harness from starting.
        """
        flipped = 0
        for path in self.runs_root.glob("*/run.json"):
            try:
                record = _read_record(path)
            except Exception as exc:
                logger.debug("skipping unreadable harness run record %s: %s", path, exc)
                continue
            if record.status != "running" or record.id in self._active or record.generation == self.generation:
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

    def recover_interrupted_staging(self) -> int:
        """Undo what a kill during staging left behind; returns how many entries it restored, removed or marked.

        Run once at startup, before the prune and before requests are served. Re-staging renames the old candidate
        aside (`.tmp-<hex>.old`), then the new build into place: a kill between the two left the candidate only in
        the aside. An aside whose candidate.json names a plain skill id is renamed back when that id is missing and
        removed when it is there; one whose candidate.json names no such id is left, with a warning. An aside with no
        candidate.json (a kill during its removal) cannot be restored: it is removed when its envelope.json (a
        signed one's payload included) or provenance.json names a target that is in place, or when it holds nothing
        but the files a staged candidate has (empty included), and is left, with a warning, otherwise (W2-29).
        Every other `.tmp-*` entry is a
        build that was never placed, and is removed. A `converged` run with no candidate (the process died while
        staging it) gets the candidate staged for its contract and last attempt's trace, when one is there, and is
        marked `failed` otherwise. Nothing here raises: what cannot be done is logged.
        """
        changed = 0
        try:
            entries = [entry for entry in self.staging_root.iterdir() if entry.name.startswith(".tmp-")]
        except OSError as exc:
            logger.warning("harness staging recovery could not list %s: %s", self.staging_root, exc)
            entries = []
        for aside in [entry for entry in entries if entry.name.endswith(".old") and entry.is_dir()]:
            skill_id = self._aside_skill_id(aside)
            if skill_id is None:
                has_candidate = (aside / "candidate.json").exists()
                named = None if has_candidate else self._half_removed_aside_skill_id(aside)
                if not has_candidate and (
                    (named is not None and (self.staging_root / named).exists()) or self._only_staged_files(aside)
                ):
                    # A kill during the aside's removal took its candidate.json (CB3-6): no candidate is left to
                    # restore, whether its target is in place or nothing but staged files (or nothing) is left.
                    # Left, it was warned about at every start, and prune no longer reclaims it by default.
                    try:
                        shutil.rmtree(aside)
                        changed += 1
                    except OSError as exc:
                        logger.warning("harness staging recovery could not remove %s: %s", aside, exc)
                    continue
                if has_candidate:
                    logger.warning(
                        "harness staging recovery left %s: its candidate.json names no plain skill id", aside
                    )
                else:
                    logger.warning(
                        "harness staging recovery left %s: it has no candidate.json and holds entries a staged "
                        "candidate does not",
                        aside,
                    )
                continue
            target = self.staging_root / skill_id
            try:
                if target.exists():
                    shutil.rmtree(aside)
                else:
                    os.replace(aside, target)
                    logger.warning("harness staging recovery restored candidate %s from %s", skill_id, aside.name)
                changed += 1
            except OSError as exc:
                logger.warning("harness staging recovery could not settle %s: %s", aside, exc)
        for build in [entry for entry in entries if not entry.name.endswith(".old")]:
            try:
                shutil.rmtree(build) if build.is_dir() else build.unlink()
                changed += 1
            except OSError as exc:
                logger.warning("harness staging recovery could not remove %s: %s", build, exc)
        for path in self.runs_root.glob("*/run.json"):
            try:
                record = _read_record(path)
            except Exception as exc:
                logger.debug("skipping unreadable harness run record %s: %s", path, exc)
                continue
            if record.status != "converged" or record.candidate is not None or record.id in self._active:
                continue
            staged = self._staged_candidate_of(record)
            if staged is not None:
                # Killed after the build was in place, before run.json was saved with it (CB3-5).
                record.candidate = staged
                record.notes.append("Staged skill candidate recorded at startup: the last process staged it.")
                try:
                    self.save(record)
                except (OSError, ValueError) as exc:
                    logger.warning("could not record harness run %s's staged candidate: %s", record.id, exc)
                    continue
                logger.warning("harness staging recovery recorded run %s's staged candidate", record.id)
                changed += 1
                continue
            record.status = "failed"
            record.decision = ConvergenceDecision(
                status="failed",
                converged=False,
                should_continue=False,
                reason="no skill candidate was recorded for this run",
                successful_attempts=record.decision.successful_attempts if record.decision else 0,
                total_attempts=len(record.attempts),
            )
            record.notes.append(
                "The run converged, but no skill candidate is recorded for it and none staged matches its last "
                "attempt: the controller stopped before the candidate was recorded."
            )
            try:
                self.save(record)
            except (OSError, ValueError) as exc:
                logger.warning("could not mark harness run %s failed after interrupted staging: %s", record.id, exc)
                continue
            changed += 1
        return changed

    def _staged_candidate_of(self, record: HarnessRunRecord) -> SkillCandidate | None:
        """The candidate staged for `record`: its contract and owner, and the trace of its last attempt."""
        expected = record.attempts[-1].trace_hash if record.attempts else ""
        if not expected:
            return None
        directory = self.staging_root / skill_id_for(record.contract_id, record.contract_hash, record.owner)
        try:
            candidate = SkillCandidate.model_validate_json((directory / "candidate.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if candidate.contract_hash != record.contract_hash or candidate.owner != record.owner:
            return None
        # Induction and the attempt hash the same scrubbed trace, so the hashes match or the candidate is another's.
        return candidate if candidate.trace_hash == expected else None

    def _aside_skill_id(self, aside: Path) -> str | None:
        """The skill id an aside's candidate.json names, when it is one plain name that stays inside the root."""
        try:
            skill_id = json.loads((aside / "candidate.json").read_text(encoding="utf-8")).get("skill_id")
        except (OSError, ValueError, AttributeError):
            return None
        return self._plain_skill_id(skill_id)

    @staticmethod
    def _only_staged_files(aside: Path) -> bool:
        """Whether every entry of `aside` (none included) is a file name a staged candidate has."""
        try:
            return all(entry.name in CANDIDATE_FILES and not entry.is_dir() for entry in aside.iterdir())
        except OSError:
            return False

    def _half_removed_aside_skill_id(self, aside: Path) -> str | None:
        """For an aside whose candidate.json is gone, the plain skill id its envelope.json (an unsigned one's own
        field, a signed one's payload) or provenance.json names."""
        if (aside / "candidate.json").exists():
            return None
        for name, keys in (
            ("envelope.json", ("skill_id",)),
            ("envelope.json", ("payload", "skill_id")),  # mesh_identity_signer's SignedEnvelope
            ("provenance.json", ("candidate", "skill_id")),
        ):
            try:
                value: Any = json.loads((aside / name).read_text(encoding="utf-8"))
                for key in keys:
                    value = value.get(key)
            except (OSError, ValueError, AttributeError):
                continue
            skill_id = self._plain_skill_id(value)
            if skill_id is not None:
                return skill_id
        return None

    def _plain_skill_id(self, skill_id: Any) -> str | None:
        """`skill_id` when it is one plain name that stays inside the staging root."""
        if not isinstance(skill_id, str) or not skill_id or skill_id.startswith(".") or skill_id in {".", ".."}:
            return None
        if "/" in skill_id or "\\" in skill_id or "\x00" in skill_id:
            return None
        root = self.staging_root.resolve()
        if (root / skill_id).resolve().parent != root:
            return None
        return skill_id

    def _owned_runs(self, owner: str | None) -> set[str]:
        """The IDs of the stored runs that belong to `owner` (None: the runs nobody owns)."""
        owned: set[str] = set()
        for path in self.runs_root.glob("*/run.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            # A record that is not a JSON object ([] or null) belongs to no one; .get on it would end every start.
            if isinstance(record, dict) and record.get("owner") == owner:
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
        candidates: list[tuple[Path, str, bool]] = []
        for root, marker, staged in (
            (self.runs_root, "run.json", False),
            (self.staging_root, "candidate.json", True),
        ):
            try:
                candidates += [(path, marker, staged) for path in root.iterdir()]
            except OSError as exc:
                logger.warning("harness prune could not list %s: %s", root, exc)
        for directory, marker, staged in candidates:
            if directory.name in self._active:
                continue
            doomed = directory
            try:
                # Inside the try (W9-7): is_dir() raises for EACCES or EIO, and one such entry ended the prune.
                if not directory.is_dir():
                    continue
                # A staged entry's age check and its rename to a hidden `.tmp-` name are one step against staging's
                # rename into place and against candidate reads, which hold the lock too; it is removed after, under
                # that name, which nothing serves (and startup recovery removes), so no read meets a candidate half
                # removed or waits for its removal. Staging never replaces a run directory: removing one takes no lock.
                with self.staging_lock if staged else contextlib.nullcontext():
                    marker_path = directory / marker
                    newest = max(
                        directory.stat().st_mtime, marker_path.stat().st_mtime if marker_path.exists() else 0.0
                    )
                    if newest >= cutoff:
                        continue
                    if staged:
                        hidden = directory.with_name(f".tmp-{uuid.uuid4().hex[:12]}")
                        os.replace(directory, hidden)
                        doomed = hidden  # once renamed: a failed rename is logged for the candidate it left
                shutil.rmtree(doomed)
                removed += 1
            except OSError as exc:
                logger.warning("harness prune could not remove %s: %s", doomed, exc)
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
        for _, path in sorted(_with_mtimes(self.runs_root.glob("*/run.json")), key=lambda item: item[0], reverse=True):
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


def _with_mtimes(paths: Iterable[Path]) -> list[tuple[float, Path]]:
    """Each path with its modification time, leaving out one removed since it was listed (a prune in between) and,
    with a warning, one that cannot be stat'ed (W5-9): one such entry must not fail the whole listing."""
    stamped: list[tuple[float, Path]] = []
    for path in paths:
        try:
            stamped.append((path.stat().st_mtime, path))
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("skipping harness run record %s: cannot read its modification time: %s", path, exc)
            continue
    return stamped


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
        self._admission = asyncio.Lock()
        self.verifier = verifier or ProgrammaticVerifier()
        self.model_tiers = {key: value for key, value in (model_tiers or {}).items() if value}
        self.inducer = SkillInducer(self.store.staging_root, signer=signer, lock=self.store.staging_lock)
        # Signing without a matching check is decoration; the registry gets the
        # counterpart to whatever the inducer signs with.
        self.registry = SkillStagingRegistry(self.store.staging_root, verifier=envelope_verifier)
        self.drift_monitor = SkillDriftMonitor(self.registry, verifier=self.verifier, lock=self.store.staging_lock)

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
        # The metadata's values and a mock observation are the caller's own (Any). A lone surrogate in one, which
        # UTF-8 cannot encode, could be neither hashed nor written: the contract's write raised and left the run's
        # empty directory, and the attempt failed. Replaced with U+FFFD before either, as the stores replace it
        # (what a live attempt observed: TraceEnvelope.from_agent_result).
        if holds_lone_surrogate(contract.metadata):
            contract = contract.model_copy(update={"metadata": without_lone_surrogates(contract.metadata)})
        mock_final_observation = without_lone_surrogates(mock_final_observation)
        owner = verified_operator()
        run_id = uuid.uuid4().hex[:12]
        # Admissions run one at a time (W5-5). With a cap set, the stored runs are read in a worker thread holding no
        # lock, so release (on the event loop) never waits for the scan, and the place is reserved on the loop with
        # no await before the try that gives it back: a cancel during the scan reserves nothing.
        async with self._admission:
            counted: set[str] = set()
            if self.max_runs_per_operator > 0:
                counted = self.store.starting_runs(owner)
                counted |= await asyncio.to_thread(self.store._owned_runs, owner)
            admitted = self.store.reserve(run_id, owner, counted, max_owned=self.max_runs_per_operator)
        if not admitted:
            raise ValueError(
                f"harness run limit reached: {self.max_runs_per_operator} stored runs for this operator "
                "(HARNESS_MAX_RUNS_PER_OPERATOR); HARNESS_RETENTION_DAYS, when set, removes old runs"
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
                generation=self.store.generation,
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
            # Run to its end: the candidate is staged in a worker thread, which a cancel cannot stop, so a cancel waits
            # for it, and the record names what it staged, before the cancel goes on.
            await run_to_completion(self._stage_candidate(record, contract, latest_trace, results[-1]))

    async def _stage_candidate(
        self, record: HarnessRunRecord, contract: TaskContract, trace: TraceEnvelope, verification: VerificationResult
    ) -> None:
        """Stage a converged run's skill candidate and record it.

        Built and signed in a worker thread, then placed on the event loop once the staging lock is free, which the loop
        does not wait for (R5C-12): retention's prune holds that lock in a thread from a candidate's age check through
        its rename aside (it held it through the removal), and waiting for it stalled every task. Placed on the loop,
        not in the thread: a re-stage's two renames ran between candidate reads on the loop, and a read found no
        candidate or the new files under the old one's signature. The record is saved right after, with no await
        between, so a shutdown that cancels this places nothing its record does not name. The candidate it replaced is
        removed after that, in a worker thread: removed on the loop, a slow removal stalled every task.
        """
        try:
            candidate, build_dir, target_dir = await asyncio.to_thread(
                self.inducer.build,
                contract=contract,
                trace=trace,
                verification=verification,
                attempts=len(record.attempts),
                model_tiers=self.model_tiers,
                owner=record.owner,
            )
            placed = False
            try:
                while not self.inducer.try_place(build_dir, target_dir):
                    await asyncio.sleep(_STAGING_LOCK_RETRY_SECONDS)
                placed = True
            finally:
                if not placed:
                    shutil.rmtree(build_dir, ignore_errors=True)
            record.candidate = candidate
        except Exception as exc:
            # The inducer cleans up after itself; the run must not stay "converged" with no candidate.
            logger.exception("harness run %s: staging the skill candidate failed", record.id)
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
        await asyncio.to_thread(self.inducer.remove_aside, build_dir)

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
        # Under the staging lock: retention removes a candidate in a worker thread (HarnessRunStore.staging_lock).
        with self.store.staging_lock:
            return self.registry.list_candidates()

    def get_candidate(self, skill_id: str) -> dict[str, Any]:
        with self.store.staging_lock:
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
