#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "controller"
if str(CONTROLLER) not in sys.path:
    sys.path.insert(0, str(CONTROLLER))

from app.agent_eval import (  # noqa: E402
    ControllerEvalClient,
    build_matrix,
    load_cases,
    plan_payload,
    render_markdown_report,
    score_from_result_dir,
    score_result,
    summarize_scores,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run or score repeatable auto-browser agent evaluations.")
    parser.add_argument("--cases", default=str(ROOT / "evals" / "agent_cases.json"))
    parser.add_argument("--providers", default="", help="Comma-separated provider override, e.g. openai,claude.")
    parser.add_argument("--profiles", default="", help="Comma-separated workflow profile override, e.g. fast,governed.")
    parser.add_argument(
        "--results-dir",
        default="",
        help="Directory containing <case>__<provider>__<profile>.json files.",
    )
    parser.add_argument("--execute", action="store_true", help="Execute cases against a running controller.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Controller base URL for --execute.")
    parser.add_argument("--bearer-token", default="")
    parser.add_argument("--operator-id", default="")
    parser.add_argument("--operator-name", default="")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument("--report-file", default="", help="Write a Markdown report to this path.")
    args = parser.parse_args()

    cases = load_cases(args.cases)
    matrix = build_matrix(
        cases,
        providers=_csv(args.providers),
        workflow_profiles=_csv(args.profiles),
    )

    if args.execute:
        client = ControllerEvalClient(
            args.base_url,
            bearer_token=args.bearer_token or None,
            operator_id=args.operator_id or None,
            operator_name=args.operator_name or None,
        )
        scores = []
        for spec in matrix:
            result = client.run_spec(spec)
            scores.append(score_result(spec, result))
        _write_report(args.report_file, render_markdown_report(matrix, scores=scores))
        return _emit_scores(scores, json_output=args.json)

    if args.results_dir:
        scores = score_from_result_dir(matrix, args.results_dir)
        _write_report(args.report_file, render_markdown_report(matrix, scores=scores))
        return _emit_scores(scores, json_output=args.json)

    plan = plan_payload(matrix)
    _write_report(args.report_file, render_markdown_report(matrix))
    if args.json:
        print(json.dumps(plan, indent=2))
    else:
        print(f"Planned {len(matrix)} agent eval run(s) from {len(cases)} case(s).")
        for run in plan["runs"]:
            print(f"- {run['case_id']} provider={run['provider']} profile={run['workflow_profile']}")
    return 0


def _emit_scores(scores, *, json_output: bool) -> int:
    payload = {
        "summary": summarize_scores(scores),
        "scores": [score.to_dict() for score in scores],
    }
    if json_output:
        print(json.dumps(payload, indent=2))
    else:
        for key, summary in payload["summary"].items():
            print(
                f"{key}: {summary['successes']}/{summary['runs']} success, "
                f"avg={summary['average_score']:.4f}"
            )
        for score in payload["scores"]:
            status = "PASS" if score["success"] else "FAIL"
            print(f"- {status} {score['case_id']} {score['provider']}/{score['workflow_profile']}")
    return 0 if all(score.success for score in scores) else 1


def _csv(value: str):
    items = tuple(item.strip() for item in value.split(",") if item.strip())
    return items or None


def _write_report(path: str, content: str) -> None:
    if not path:
        return
    report_path = Path(path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
