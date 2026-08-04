from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from .contracts import TaskContract
from .iterate import HarnessService


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one auto-browser convergence harness task contract.")
    parser.add_argument("--contract", required=True, help="Path to a TaskContract JSON file.")
    parser.add_argument("--root", default=".autonomous/harness", help="Harness state root.")
    parser.add_argument(
        "--mock-final-observation", default="", help="JSON object or path for deterministic local runs."
    )
    parser.add_argument("--mock-final-url", default="", help="Convenience URL for deterministic local runs.")
    parser.add_argument("--mock-final-text", default="", help="Convenience text for deterministic local runs.")
    parser.add_argument("--max-attempts", type=int, default=None)
    parser.add_argument("--json", action="store_true", help="Emit the full run record as JSON.")
    args = parser.parse_args()

    contract = TaskContract.model_validate_json(Path(args.contract).read_text(encoding="utf-8"))
    mock_final_observation = _load_mock_observation(args)
    service = HarnessService(Path(args.root))
    record = asyncio.run(
        service.start_convergence(
            contract,
            mock_final_observation=mock_final_observation,
            max_attempts=args.max_attempts,
        )
    )
    payload = record.model_dump(mode="json")
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        # `candidate` is present but null on any non-converged run, so the {} default
        # never applies — guard on the value, not on the key.
        candidate = payload.get("candidate") or {}
        candidate_path = candidate.get("files", {}).get("candidate.json", "")
        print(f"harness run {record.id}: {record.status} after {len(record.attempts)} attempt(s)")
        if candidate_path:
            print(f"staged candidate: {candidate_path}")
    return 0 if record.status == "converged" else 1


def _load_mock_observation(args: argparse.Namespace) -> dict[str, Any] | None:
    payload: dict[str, Any] = {}
    if args.mock_final_observation:
        source_text = args.mock_final_observation.strip()
        if source_text.startswith("{"):
            raw = args.mock_final_observation
        else:
            source = Path(args.mock_final_observation)
            raw = source.read_text(encoding="utf-8") if source.exists() else args.mock_final_observation
        loaded = json.loads(raw)
        if not isinstance(loaded, dict):
            raise ValueError("--mock-final-observation must decode to a JSON object")
        payload.update(loaded)
    if args.mock_final_url:
        payload["url"] = args.mock_final_url
    if args.mock_final_text:
        payload["text"] = args.mock_final_text
    return payload or None


if __name__ == "__main__":
    raise SystemExit(main())
