#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "evals" / "fixture_cases.json"
DEFAULT_FIXTURE_ROOT = ROOT / "evals" / "fixtures"
MANDATORY_CASES = {
    "auth-profile-reuse",
    "popup-download-recovery",
    "governed-blocks-write",
    "approval-required-upload",
    "resume-after-failure",
    "multi-tab-recovery",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate local HTML fixtures for release-critical agent evals.")
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--fixture-root", default=str(DEFAULT_FIXTURE_ROOT))
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args()

    case_path = Path(args.cases)
    fixture_root = Path(args.fixture_root)
    failures = validate_fixtures(case_path, fixture_root)
    payload = {
        "case_file": str(case_path),
        "fixture_root": str(fixture_root),
        "failures": failures,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    elif failures:
        print("Fixture eval failed:")
        for failure in failures:
            print(f"- {failure}")
    else:
        cases = _load_cases(case_path)
        print(f"Fixture eval passed: {len(cases)} cases validated.")
    return 1 if failures else 0


def validate_fixtures(case_path: Path, fixture_root: Path) -> list[str]:
    failures: list[str] = []
    if not case_path.is_file():
        return [f"missing fixture case file: {case_path}"]
    if not fixture_root.is_dir():
        return [f"missing fixture directory: {fixture_root}"]

    try:
        cases = _load_cases(case_path)
    except Exception as exc:
        return [f"invalid fixture case file: {exc}"]

    ids = {str(case.get("id") or "") for case in cases}
    missing_mandatory = sorted(MANDATORY_CASES - ids)
    if missing_mandatory:
        failures.append(f"missing mandatory fixture cases: {', '.join(missing_mandatory)}")
    if len(cases) < len(MANDATORY_CASES):
        failures.append(f"expected at least {len(MANDATORY_CASES)} fixture cases; got {len(cases)}")

    for case in cases:
        case_id = str(case.get("id") or "<missing>")
        fixture_name = str(case.get("fixture") or "")
        if not fixture_name:
            failures.append(f"{case_id}: fixture is required")
            continue
        fixture_path = (fixture_root / fixture_name).resolve()
        try:
            fixture_path.relative_to(fixture_root.resolve())
        except ValueError:
            failures.append(f"{case_id}: fixture path escapes fixture root")
            continue
        if not fixture_path.is_file():
            failures.append(f"{case_id}: missing fixture file {fixture_name}")
            continue
        content = fixture_path.read_text(encoding="utf-8")
        failures.extend(_missing_fragments(case_id, content, case.get("required_text"), label="text"))
        failures.extend(_missing_fragments(case_id, content, case.get("required_fragments"), label="fragment"))

    governed = next((case for case in cases if case.get("id") == "governed-blocks-write"), None)
    if governed is None:
        failures.append("governed-blocks-write: mandatory case missing")
    else:
        expected = governed.get("expected_status_by_profile")
        if not isinstance(expected, dict):
            failures.append("governed-blocks-write: expected_status_by_profile is required")
        elif expected.get("governed") != "approval_required":
            failures.append("governed-blocks-write: governed profile must expect approval_required")
        elif expected.get("fast") == "approval_required":
            failures.append("governed-blocks-write: fast profile must diverge from governed approval blocking")
    return failures


def _load_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases") if isinstance(payload, dict) else payload
    if not isinstance(cases, list):
        raise ValueError("fixture cases must be a list or an object with a cases list")
    return [case for case in cases if isinstance(case, dict)]


def _missing_fragments(case_id: str, content: str, values: Any, *, label: str) -> list[str]:
    if not values:
        return []
    failures: list[str] = []
    for value in values:
        fragment = str(value)
        if fragment not in content:
            failures.append(f"{case_id}: missing required {label}: {fragment}")
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
