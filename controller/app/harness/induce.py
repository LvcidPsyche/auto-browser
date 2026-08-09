from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from .contracts import TaskContract
from .trace import TraceEnvelope, hash_payload
from .verifier.base import VerificationResult

Signer = Callable[[dict[str, Any]], dict[str, Any]]


class SkillCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: str
    name: str
    description: str
    safety_class: str = "read_only"
    contract_hash: str
    trace_hash: str
    verifier_backend: str
    verifier_passed: bool | None
    verifier_confidence: float
    attempts: int
    cost_usd: float = 0.0
    model_tiers: dict[str, str] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)
    # Whether the envelope carries a real signature. Without this, an unsigned
    # envelope is just a dict that looks like a signed one — which is how a
    # candidate could claim "signed provenance" while being unsigned.
    signed: bool = False
    # Induced from a mock trace: no browser ever ran. Recorded on the candidate,
    # inside the provenance hash, and in the envelope, so a simulated skill
    # cannot quietly present itself as a converged one.
    simulated: bool = False
    files: dict[str, str] = Field(default_factory=dict)
    envelope: dict[str, Any] | None = None


class SkillInducer:
    def __init__(self, staging_root: str | Path, signer: Signer | None = None):
        self.staging_root = Path(staging_root)
        self.signer = signer

    def induce(
        self,
        *,
        contract: TaskContract,
        trace: TraceEnvelope,
        verification: VerificationResult,
        attempts: int,
        cost_usd: float = 0.0,
        model_tiers: dict[str, str] | None = None,
    ) -> SkillCandidate:
        skill_id = _skill_id(contract)
        staging_root = self.staging_root.resolve()
        target_dir = (self.staging_root / skill_id).resolve()
        try:
            target_dir.relative_to(staging_root)
        except ValueError as exc:
            raise ValueError(f"staged skill path escapes staging root: {skill_id}") from exc
        target_dir.mkdir(parents=True, exist_ok=True)
        contract_hash = contract.hash()
        trace_hash = trace.trace_hash
        candidate = SkillCandidate(
            skill_id=skill_id,
            name=contract.id,
            description=contract.goal,
            contract_hash=contract_hash,
            trace_hash=trace_hash,
            verifier_backend=verification.backend,
            verifier_passed=verification.passed,
            verifier_confidence=verification.confidence,
            attempts=attempts,
            cost_usd=cost_usd,
            model_tiers={key: value for key, value in (model_tiers or {}).items() if value},
            signed=self.signer is not None,
            simulated=str(trace.metadata.get("mode") or "").lower() == "mock",
        )
        skill_md = _render_skill_markdown(contract, candidate, verification)
        helper_py = _render_helper(contract)
        self_test = _render_self_test(contract)
        provenance = {
            "candidate": candidate.model_dump(mode="json", exclude={"files", "envelope"}),
            "contract": contract.model_dump(mode="json"),
            "trace": {
                "run_id": trace.run_id,
                "trace_hash": trace_hash,
                "event_count": len(trace.events),
                "evidence": trace.evidence,
                "simulated": candidate.simulated,
            },
            "verification": verification.model_dump(mode="json"),
        }
        envelope_payload = {
            "kind": "skill_candidate",
            "version": 1,
            "artifact_type": "auto_browser.skill_candidate",
            "schema_version": "1",
            "candidate_id": skill_id,
            "skill_id": skill_id,
            "name": contract.id,
            "contract_hash": contract_hash,
            "trace_hash": trace_hash,
            "verifier": verification.model_dump(mode="json"),
            "provenance_hash": hash_payload(provenance),
            "metadata": {
                "task_class": contract.task_class,
                "attempts": attempts,
                "cost_usd": cost_usd,
                "model_tiers": candidate.model_tiers,
                "validated_executor_model": candidate.model_tiers.get("executor", ""),
                "credential_free": True,
                "simulated": candidate.simulated,
            },
        }
        if self.signer is not None:
            candidate.envelope = self.signer(envelope_payload)
        else:
            # Say so, in the artifact itself. An unsigned payload that is shaped
            # exactly like a signed one is how "signed provenance" becomes a
            # claim nobody can check.
            candidate.envelope = {**envelope_payload, "signed": False}

        files = {
            "SKILL.md": skill_md,
            "helper.py": helper_py,
            "test_skill.py": self_test,
            "provenance.json": json.dumps(provenance, indent=2, sort_keys=True),
            "envelope.json": json.dumps(candidate.envelope, indent=2, sort_keys=True),
        }
        for name, content in files.items():
            path = target_dir / name
            path.write_text(content, encoding="utf-8")
            candidate.files[name] = str(path)
        (target_dir / "contract.json").write_text(contract.model_dump_json(indent=2), encoding="utf-8")
        (target_dir / "trace.json").write_text(trace.model_dump_json(indent=2), encoding="utf-8")
        candidate.files["contract.json"] = str(target_dir / "contract.json")
        candidate.files["trace.json"] = str(target_dir / "trace.json")
        (target_dir / "candidate.json").write_text(candidate.model_dump_json(indent=2), encoding="utf-8")
        candidate.files["candidate.json"] = str(target_dir / "candidate.json")
        return candidate


def _skill_id(contract: TaskContract) -> str:
    return _slug(contract.id, suffix=contract.hash()[:12])


def _slug(value: str, *, suffix: str | None = None) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-.").lower()
    base = normalized if normalized and not set(normalized) <= {"."} else "skill"
    digest = suffix or hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    base = base[: max(1, 80 - len(digest) - 1)].rstrip("-.") or "skill"
    return f"{base}-{digest}"


def _render_skill_markdown(
    contract: TaskContract,
    candidate: SkillCandidate,
    verification: VerificationResult,
) -> str:
    postconditions = "\n".join(f"- `{item.kind}`: {item.value}" for item in contract.postconditions)
    return f"""---
name: {candidate.skill_id}
description: {contract.goal}
status: staging
safety_class: {candidate.safety_class}
contract_hash: {candidate.contract_hash}
trace_hash: {candidate.trace_hash}
verifier: {candidate.verifier_backend}
---

# {contract.id}

## Goal

{contract.goal}

## Preconditions

{_render_list([f"{item.kind}: {item.value}" for item in contract.preconditions])}

## Postconditions

{postconditions}

## Verification

- passed: `{verification.passed}`
- confidence: `{verification.confidence:.3f}`
- notes: {verification.notes}

## Runtime

Use `helper.py` as the generated replay helper. This candidate is staged and must be reviewed before promotion.
"""


def _render_list(items: list[str]) -> str:
    if not items:
        return "- none"
    return "\n".join(f"- {item}" for item in items)


def _render_helper(contract: TaskContract) -> str:
    start_url = contract.start_url or "about:blank"
    return f'''from __future__ import annotations

from playwright.sync_api import Page


def run(page: Page) -> dict:
    """Replay helper generated from the convergence harness."""
    page.goto({start_url!r})
    return {{"url": page.url, "title": page.title()}}
'''


def _render_self_test(contract: TaskContract) -> str:
    return f"""from __future__ import annotations

import json
from pathlib import Path


def test_contract_is_embedded() -> None:
    contract = json.loads((Path(__file__).parent / "contract.json").read_text(encoding="utf-8"))
    assert contract["id"] == {contract.id!r}
    assert contract["postconditions"]
"""
