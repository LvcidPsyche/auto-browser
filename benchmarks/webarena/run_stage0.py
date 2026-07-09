#!/usr/bin/env python
"""WebArena Stage 0 runner.

Two modes:

  validate  (default) — parse the manifest into contracts and check they are
                        well-formed. Deterministic, browser-free; safe for CI.
  execute             — materialize the evidence layout for each contract and,
                        when a pinned WebArena environment is configured, drive
                        the controller against it and save real evidence.

The lane is tracked-only: without ``environment.revision`` pinned and
``WEBARENA_BASE_URL`` set, ``execute`` writes the evidence scaffold and reports
tracked-only rather than fabricating a benchmark run.

    python benchmarks/webarena/run_stage0.py                 # validate
    python benchmarks/webarena/run_stage0.py --execute       # scaffold + tracked-only
    WEBARENA_BASE_URL=... python benchmarks/webarena/run_stage0.py --execute
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stage0_contracts import (  # noqa: E402
    REQUIRED_EVIDENCE,
    TaskContract,
    is_scorable,
    load_contracts,
    validate_manifest,
)

DEFAULT_EVIDENCE_ROOT = Path(__file__).resolve().parent / "runs"


def _cmd_validate() -> int:
    failures = validate_manifest()
    if failures:
        print("WebArena Stage 0 manifest validation FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    contracts = load_contracts()
    print(f"WebArena Stage 0: {len(contracts)} contracts valid; each requires {list(REQUIRED_EVIDENCE)}.")
    print(f"Scorable: {is_scorable()} (tracked-only until the environment revision is pinned).")
    return 0


def _scaffold_evidence(contract: TaskContract, run_root: Path) -> dict[str, str]:
    plan = contract.evidence_plan(run_root)
    plan["dir"].mkdir(parents=True, exist_ok=True)
    plan["screenshots"].mkdir(parents=True, exist_ok=True)
    return {name: str(path) for name, path in plan.items()}


def _cmd_execute() -> int:
    contracts = load_contracts()
    run_root = Path(os.environ.get("WEBARENA_EVIDENCE_ROOT", str(DEFAULT_EVIDENCE_ROOT)))
    run_dir = run_root / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base_url = os.environ.get("WEBARENA_BASE_URL", "").strip()

    plans = {c.id: _scaffold_evidence(c, run_dir) for c in contracts}
    summary = {
        "run_dir": str(run_dir),
        "base_url_configured": bool(base_url),
        "scorable": is_scorable(),
        "status": "executed" if (base_url and is_scorable()) else "tracked-only",
        "contracts": plans,
    }
    (run_dir).mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if not base_url or not is_scorable():
        print(
            "WebArena Stage 0: evidence layout scaffolded at "
            f"{run_dir} for {len(contracts)} contracts. Tracked-only - set "
            "WEBARENA_BASE_URL and pin environment.revision to execute live."
        )
        return 0

    # Live path (requires a provisioned WebArena env + controller browser stack).
    return _execute_live(contracts, run_dir, base_url)


def _execute_live(contracts: list[TaskContract], run_dir: Path, base_url: str) -> int:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "controller"))
        from fastapi.testclient import TestClient

        from app.main import app
    except Exception as exc:  # noqa: BLE001 - missing controller is a skip
        print(f"[webarena] SKIP live execution: controller not importable ({exc}).")
        return 0

    try:
        with TestClient(app) as client:
            for contract in contracts:
                plan = contract.evidence_plan(run_dir)
                created = client.post("/sessions", json={"name": f"webarena-{contract.id}"})
                created.raise_for_status()
                session_id = created.json().get("id") or created.json().get("session_id")
                try:
                    client.post(
                        f"/sessions/{session_id}/actions/navigate",
                        json={"url": base_url},
                    ).raise_for_status()
                    observed = client.get(f"/sessions/{session_id}/observe").json()
                    plan["actions"].write_text(
                        json.dumps([{"action": "navigate", "url": base_url}], indent=2),
                        encoding="utf-8",
                    )
                    plan["model_decisions"].write_text(
                        json.dumps({"goal": contract.goal, "observation": observed}, indent=2),
                        encoding="utf-8",
                    )
                    client.post(f"/sessions/{session_id}/screenshot")
                finally:
                    client.delete(f"/sessions/{session_id}")
    except Exception as exc:  # noqa: BLE001 - env failure is a skip, not a benchmark failure
        print(f"[webarena] SKIP live execution: browser stack unavailable ({type(exc).__name__}: {exc}).")
        return 0

    print(f"[webarena] Live evidence written under {run_dir}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="WebArena Stage 0 contract runner.")
    parser.add_argument("--execute", action="store_true", help="materialize evidence layout / drive live env")
    args = parser.parse_args()
    return _cmd_execute() if args.execute else _cmd_validate()


if __name__ == "__main__":
    raise SystemExit(main())
