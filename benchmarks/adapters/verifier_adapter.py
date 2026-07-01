#!/usr/bin/env python
"""Adapt Auto Browser run artifacts into the external verifier evidence lanes.

First adapter pass for issue #40: map a saved ``AgentRunResult`` (see
``controller/app/models.py``) into the fields required by the CUAVerifier and
Online-Mind2Web Stage 1 lanes. The adapter is pure and dict-based so it can run
over serialized result JSON without importing the controller.

It NEVER produces a score: ``verifier_result`` is left ``None`` and every record
is stamped ``"scored": false``. Scoring stays gated until each lane pins its
upstream revision and deterministic subset (see the lane manifests /
``benchmarks/adapters/README.md``).

    python benchmarks/adapters/verifier_adapter.py run_result.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Fields each lane needs from an Auto Browser run. Surfacing these is the
# "identify the trace/result fields" deliverable.
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "online_mind2web": (
        "provider",
        "workflow_profile",
        "auth_profile",
        "final_url",
        "action_sequence",
        "verifier_result",
    ),
    "cuaverifier": (
        "task_goal",
        "provider",
        "status",
        "final_url",
        "action_sequence",
        "evidence",
        "verifier_result",
    ),
}


def _final_session(run: dict[str, Any]) -> dict[str, Any]:
    value = run.get("final_session")
    return value if isinstance(value, dict) else {}


def _final_url(run: dict[str, Any]) -> str | None:
    return _final_session(run).get("current_url")


def _action_sequence(run: dict[str, Any]) -> list[dict[str, Any]]:
    """One entry per executed step: the chosen action + where it landed."""
    sequence: list[dict[str, Any]] = []
    for step in run.get("steps", []):
        decision = step.get("decision") if isinstance(step.get("decision"), dict) else {}
        sequence.append(
            {
                "action": decision.get("action"),
                "element_id": decision.get("element_id"),
                "url": decision.get("url"),
                "risk_category": decision.get("risk_category"),
                "step_status": step.get("status"),
            }
        )
    return sequence


def to_mind2web_result(run: dict[str, Any]) -> dict[str, Any]:
    final = _final_session(run)
    return {
        "lane": "online-mind2web-stage1",
        "scored": False,
        "provider": run.get("provider"),
        "workflow_profile": run.get("workflow_profile"),
        "auth_profile": final.get("auth_profile"),
        "final_url": _final_url(run),
        "action_sequence": _action_sequence(run),
        # Produced by the external verifier; pending until the lane is pinned.
        "verifier_result": None,
    }


def to_cuaverifier_input(run: dict[str, Any]) -> dict[str, Any]:
    final = _final_session(run)
    return {
        "lane": "cuaverifier-stage1",
        "scored": False,
        "task_goal": run.get("goal"),
        "provider": run.get("provider"),
        "model": run.get("model"),
        "status": run.get("status"),
        "final_url": _final_url(run),
        "action_sequence": _action_sequence(run),
        "evidence": {
            # Refs into the saved session artifacts; filled when the lane is pinned.
            "artifact_dir": final.get("artifact_dir"),
            "trace": final.get("trace_path"),
            "screenshots": None,
        },
        "verifier_result": None,
    }


def adapt(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map one run into both verifier lanes."""
    return {
        "online_mind2web": to_mind2web_result(run),
        "cuaverifier": to_cuaverifier_input(run),
    }


def missing_source_fields(run: dict[str, Any]) -> list[str]:
    """Report source fields the lanes need that this run does not carry."""
    gaps: list[str] = []
    if not run.get("provider"):
        gaps.append("provider")
    if _final_url(run) is None:
        gaps.append("final_session.current_url")
    if not run.get("steps"):
        gaps.append("steps (action_sequence would be empty)")
    return gaps


def main() -> int:
    parser = argparse.ArgumentParser(description="Adapt an Auto Browser run result into verifier lane records.")
    parser.add_argument("run_result", help="path to a saved AgentRunResult JSON file")
    args = parser.parse_args()

    run = json.loads(Path(args.run_result).read_text(encoding="utf-8"))
    mapped = adapt(run)
    gaps = missing_source_fields(run)
    if gaps:
        print(f"# warning: run is missing lane source fields: {gaps}", file=sys.stderr)
    print(json.dumps(mapped, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
