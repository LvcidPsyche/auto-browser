from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AgentEvalCase:
    id: str
    name: str
    goal: str
    start_url: str | None = None
    providers: tuple[str, ...] = ("openai",)
    workflow_profiles: tuple[str, ...] = ("fast", "governed")
    max_steps: int = 6
    expected_statuses: tuple[str, ...] = ("done",)
    expect_url_contains: tuple[str, ...] = ()
    expect_actions: tuple[str, ...] = ()
    min_step_count: int | None = None
    max_step_count: int | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> AgentEvalCase:
        return cls(
            id=str(payload["id"]),
            name=str(payload.get("name") or payload["id"]),
            goal=str(payload["goal"]),
            start_url=_optional_str(payload.get("start_url")),
            providers=_string_tuple(payload.get("providers") or ("openai",)),
            workflow_profiles=_string_tuple(payload.get("workflow_profiles") or ("fast", "governed")),
            max_steps=int(payload.get("max_steps", 6)),
            expected_statuses=_string_tuple(payload.get("expected_statuses") or ("done",)),
            expect_url_contains=_string_tuple(payload.get("expect_url_contains") or ()),
            expect_actions=_string_tuple(payload.get("expect_actions") or ()),
            min_step_count=_optional_int(payload.get("min_step_count")),
            max_step_count=_optional_int(payload.get("max_step_count")),
        )


@dataclass(frozen=True)
class AgentEvalSpec:
    case: AgentEvalCase
    provider: str
    workflow_profile: str

    @property
    def result_name(self) -> str:
        return f"{self.case.id}__{self.provider}__{self.workflow_profile}.json"

    def request_payload(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "goal": self.case.goal,
            "max_steps": self.case.max_steps,
            "workflow_profile": self.workflow_profile,
        }


@dataclass(frozen=True)
class EvalCriterion:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class AgentEvalScore:
    case_id: str
    provider: str
    workflow_profile: str
    success: bool
    score: float
    criteria: tuple[EvalCriterion, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "provider": self.provider,
            "workflow_profile": self.workflow_profile,
            "success": self.success,
            "score": round(self.score, 4),
            "criteria": [criterion.__dict__ for criterion in self.criteria],
        }


def load_cases(path: str | Path) -> list[AgentEvalCase]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    raw_cases = payload.get("cases", payload) if isinstance(payload, dict) else payload
    if not isinstance(raw_cases, list):
        raise ValueError("eval cases must be a list or an object with a cases list")
    return [AgentEvalCase.from_dict(item) for item in raw_cases]


def build_matrix(
    cases: list[AgentEvalCase],
    *,
    providers: tuple[str, ...] | None = None,
    workflow_profiles: tuple[str, ...] | None = None,
) -> list[AgentEvalSpec]:
    matrix: list[AgentEvalSpec] = []
    for case in cases:
        case_providers = providers or case.providers
        case_profiles = workflow_profiles or case.workflow_profiles
        for provider in case_providers:
            for profile in case_profiles:
                matrix.append(AgentEvalSpec(case=case, provider=provider, workflow_profile=profile))
    return matrix


def score_result(spec: AgentEvalSpec, result: dict[str, Any]) -> AgentEvalScore:
    criteria: list[EvalCriterion] = []
    status = str(result.get("status") or "")
    if spec.case.expected_statuses:
        criteria.append(
            EvalCriterion(
                name="status",
                passed=status in spec.case.expected_statuses,
                detail=f"expected {', '.join(spec.case.expected_statuses)}; got {status or 'missing'}",
            )
        )

    url = _current_url(result)
    if spec.case.expect_url_contains:
        criteria.append(
            EvalCriterion(
                name="url",
                passed=any(fragment in url for fragment in spec.case.expect_url_contains),
                detail=f"expected one of {', '.join(spec.case.expect_url_contains)}; got {url or 'missing'}",
            )
        )

    actions = _action_names(result)
    for action in spec.case.expect_actions:
        criteria.append(
            EvalCriterion(
                name=f"action:{action}",
                passed=action in actions,
                detail=f"observed actions: {', '.join(actions) if actions else 'none'}",
            )
        )

    step_count = len(_steps(result))
    if spec.case.min_step_count is not None:
        criteria.append(
            EvalCriterion(
                name="min_steps",
                passed=step_count >= spec.case.min_step_count,
                detail=f"expected >= {spec.case.min_step_count}; got {step_count}",
            )
        )
    if spec.case.max_step_count is not None:
        criteria.append(
            EvalCriterion(
                name="max_steps",
                passed=step_count <= spec.case.max_step_count,
                detail=f"expected <= {spec.case.max_step_count}; got {step_count}",
            )
        )

    criteria.append(
        EvalCriterion(
            name="no_error",
            passed=status != "error" and not result.get("error"),
            detail=str(result.get("error") or "ok"),
        )
    )
    passed = sum(1 for criterion in criteria if criterion.passed)
    score = passed / len(criteria) if criteria else 0.0
    return AgentEvalScore(
        case_id=spec.case.id,
        provider=spec.provider,
        workflow_profile=spec.workflow_profile,
        success=passed == len(criteria),
        score=score,
        criteria=tuple(criteria),
    )


def summarize_scores(scores: list[AgentEvalScore]) -> dict[str, Any]:
    groups: dict[str, list[AgentEvalScore]] = {}
    for score in scores:
        key = f"{score.provider}/{score.workflow_profile}"
        groups.setdefault(key, []).append(score)
    return {
        key: {
            "runs": len(items),
            "successes": sum(1 for item in items if item.success),
            "average_score": round(sum(item.score for item in items) / len(items), 4),
        }
        for key, items in sorted(groups.items())
    }


def plan_payload(specs: list[AgentEvalSpec]) -> dict[str, Any]:
    return {
        "cases": len({spec.case.id for spec in specs}),
        "runs": [
            {
                "case_id": spec.case.id,
                "name": spec.case.name,
                "provider": spec.provider,
                "workflow_profile": spec.workflow_profile,
                "max_steps": spec.case.max_steps,
                "result_file": spec.result_name,
            }
            for spec in specs
        ],
    }


def render_markdown_report(
    specs: list[AgentEvalSpec],
    *,
    scores: list[AgentEvalScore] | None = None,
) -> str:
    lines = ["# Auto Browser Agent Eval Report", ""]
    if scores is None:
        lines.extend(
            [
                "## Planned Matrix",
                "",
                "| Case | Provider | Profile | Max Steps | Result File |",
                "| --- | --- | --- | ---: | --- |",
            ]
        )
        for spec in specs:
            lines.append(
                f"| {spec.case.id} | {spec.provider} | {spec.workflow_profile} | "
                f"{spec.case.max_steps} | {spec.result_name} |"
            )
        lines.append("")
        lines.append(f"Planned runs: {len(specs)}")
        return "\n".join(lines) + "\n"

    summary = summarize_scores(scores)
    lines.extend(
        [
            "## Summary",
            "",
            "| Provider/Profile | Runs | Successes | Average Score |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for key, item in summary.items():
        lines.append(f"| {key} | {item['runs']} | {item['successes']} | {item['average_score']:.4f} |")
    lines.extend(
        [
            "",
            "## Runs",
            "",
            "| Case | Provider | Profile | Status | Score | Failed Criteria |",
            "| --- | --- | --- | --- | ---: | --- |",
        ]
    )
    for score in scores:
        failed = [criterion.name for criterion in score.criteria if not criterion.passed]
        status = "PASS" if score.success else "FAIL"
        lines.append(
            f"| {score.case_id} | {score.provider} | {score.workflow_profile} | "
            f"{status} | {score.score:.4f} | {', '.join(failed) if failed else '-'} |"
        )
    return "\n".join(lines) + "\n"


class ControllerEvalClient:
    def __init__(
        self,
        base_url: str,
        *,
        bearer_token: str | None = None,
        operator_id: str | None = None,
        operator_name: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"Content-Type": "application/json"}
        if bearer_token:
            self.headers["Authorization"] = f"Bearer {bearer_token}"
        if operator_id:
            self.headers["X-Operator-Id"] = operator_id
        if operator_name:
            self.headers["X-Operator-Name"] = operator_name

    def run_spec(self, spec: AgentEvalSpec) -> dict[str, Any]:
        session_payload: dict[str, Any] = {"name": f"eval-{spec.case.id}"}
        if spec.case.start_url:
            session_payload["start_url"] = spec.case.start_url
        session = self._post("/sessions", session_payload)
        session_id = session.get("id") or session.get("session_id")
        if not session_id:
            raise RuntimeError("controller did not return a session id")
        return self._post(f"/sessions/{session_id}/agent/run", spec.request_payload())

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers=self.headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"controller request failed: {exc.code} {detail}") from exc


def score_from_result_dir(specs: list[AgentEvalSpec], result_dir: str | Path) -> list[AgentEvalScore]:
    root = Path(result_dir)
    scores: list[AgentEvalScore] = []
    for spec in specs:
        result_path = root / spec.result_name
        if not result_path.exists():
            missing = {"status": "error", "error": f"missing result file: {result_path}"}
            scores.append(score_result(spec, missing))
            continue
        scores.append(score_result(spec, json.loads(result_path.read_text(encoding="utf-8"))))
    return scores


def _steps(result: dict[str, Any]) -> list[dict[str, Any]]:
    steps = result.get("steps")
    return steps if isinstance(steps, list) else []


def _action_names(result: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for step in _steps(result):
        if not isinstance(step, dict):
            continue
        decision = step.get("decision")
        if isinstance(decision, dict) and decision.get("action"):
            names.add(str(decision["action"]))
    return names


def _current_url(result: dict[str, Any]) -> str:
    final_session = result.get("final_session")
    if isinstance(final_session, dict):
        for key in ("current_url", "url", "start_url"):
            if final_session.get(key):
                return str(final_session[key])
    for step in reversed(_steps(result)):
        if not isinstance(step, dict):
            continue
        execution = step.get("execution")
        after = execution.get("after") if isinstance(execution, dict) else None
        if isinstance(after, dict) and after.get("url"):
            return str(after["url"])
        observation = step.get("observation")
        if isinstance(observation, dict) and observation.get("url"):
            return str(observation["url"])
    return ""


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def _optional_str(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
