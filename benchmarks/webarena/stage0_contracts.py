#!/usr/bin/env python
"""Executable contracts for the WebArena Stage 0 lane.

Turns ``stage0-manifest.json`` from a declarative list into typed, validated
``TaskContract`` objects with a concrete per-task evidence plan. This is the
contract layer the runner (``run_stage0.py``) executes against a pinned WebArena
environment; on its own it is deterministic and browser-free, so CI can validate
the manifest without provisioning anything.

The lane stays tracked-only until ``environment.revision`` is pinned and
``competitive_score_allowed`` is flipped — see ``README.md``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MANIFEST_PATH = Path(__file__).resolve().parent / "stage0-manifest.json"

# The evidence every contract must produce before it can count.
REQUIRED_EVIDENCE = ("trace", "actions", "screenshots", "model_decisions")
# The task classes Stage 0 is allowed to express.
KNOWN_TASK_CLASSES = frozenset({"read_only", "authenticated_read", "governed_write_block"})
EXPECTED_TASK_COUNT = 5


@dataclass(frozen=True)
class TaskContract:
    id: str
    domain: str
    task_class: str
    goal: str
    required_evidence: tuple[str, ...]

    def evidence_plan(self, run_root: str | Path) -> dict[str, Path]:
        """Concrete evidence paths for one execution of this contract."""
        base = Path(run_root) / self.id
        return {
            "dir": base,
            "trace": base / "trace.zip",
            "actions": base / "actions.json",
            "screenshots": base / "screenshots",
            "model_decisions": base / "model_decisions.json",
        }


def _load_raw(manifest_path: str | Path = MANIFEST_PATH) -> dict[str, Any]:
    return json.loads(Path(manifest_path).read_text(encoding="utf-8"))


def load_contracts(manifest_path: str | Path = MANIFEST_PATH) -> list[TaskContract]:
    """Parse the manifest tasks into typed contracts (raises on malformed input)."""
    raw = _load_raw(manifest_path)
    contracts: list[TaskContract] = []
    for task in raw.get("tasks", []):
        contracts.append(
            TaskContract(
                id=str(task["id"]),
                domain=str(task["domain"]),
                task_class=str(task["task_class"]),
                goal=str(task["goal"]),
                required_evidence=tuple(task.get("required_evidence", ())),
            )
        )
    return contracts


def validate_manifest(manifest_path: str | Path = MANIFEST_PATH) -> list[str]:
    """Return a list of contract violations; empty means the manifest is well-formed."""
    failures: list[str] = []
    try:
        raw = _load_raw(manifest_path)
    except Exception as exc:  # noqa: BLE001 - surfaced as a validation failure
        return [f"manifest is not valid JSON: {exc}"]

    env = raw.get("environment")
    if not isinstance(env, dict):
        failures.append("environment block is required")
    elif "revision" not in env:
        failures.append("environment.revision key is required (may be null until pinned)")

    if not isinstance(raw.get("evidence_layout"), dict):
        failures.append("evidence_layout block is required")

    tasks = raw.get("tasks", [])
    if len(tasks) != EXPECTED_TASK_COUNT:
        failures.append(f"expected {EXPECTED_TASK_COUNT} tasks; got {len(tasks)}")

    seen_ids: set[str] = set()
    for task in tasks:
        task_id = str(task.get("id") or "<missing>")
        if task_id in seen_ids:
            failures.append(f"{task_id}: duplicate task id")
        seen_ids.add(task_id)
        for field in ("id", "domain", "task_class", "goal", "required_evidence"):
            if not task.get(field):
                failures.append(f"{task_id}: missing required field '{field}'")
        task_class = task.get("task_class")
        if task_class and task_class not in KNOWN_TASK_CLASSES:
            failures.append(f"{task_id}: unknown task_class '{task_class}'")
        evidence = tuple(task.get("required_evidence", ()))
        missing_evidence = [e for e in REQUIRED_EVIDENCE if e not in evidence]
        if missing_evidence:
            failures.append(f"{task_id}: missing required evidence {missing_evidence}")

    return failures


def is_scorable(manifest_path: str | Path = MANIFEST_PATH) -> bool:
    """True only when the environment is pinned and scoring is explicitly allowed."""
    raw = _load_raw(manifest_path)
    env = raw.get("environment") or {}
    return bool(raw.get("competitive_score_allowed")) and bool(env.get("revision"))
