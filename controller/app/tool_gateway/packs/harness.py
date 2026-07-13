from __future__ import annotations

from ...tool_inputs import (
    EmptyInput,
    HarnessGetStatusInput,
    HarnessGetTraceInput,
    HarnessGraduateInput,
    HarnessListRunsInput,
    HarnessSkillIdInput,
    HarnessStartConvergenceInput,
)
from ..registry import ToolSpec


def register(registry, gateway):
    for spec in [
        ToolSpec(
            name="harness.start_convergence",
            description=(
                "Start an Agent Skill Induction convergence run from a task contract. "
                "Converged runs emit staged skill candidates only; promotion remains governed."
            ),
            input_model=HarnessStartConvergenceInput,
            handler=gateway._harness_start_convergence,
            profiles=("full",),
            governed_kind="write",
        ),
        ToolSpec(
            name="harness.get_status",
            description="Read one convergence run record and current status.",
            input_model=HarnessGetStatusInput,
            handler=gateway._harness_get_status,
            profiles=("curated", "full"),
        ),
        ToolSpec(
            name="harness.get_trace",
            description="Read the latest or selected trace for one convergence run.",
            input_model=HarnessGetTraceInput,
            handler=gateway._harness_get_trace,
            profiles=("curated", "full"),
        ),
        ToolSpec(
            name="harness.list_runs",
            description=(
                "List recent Agent Skill Induction convergence runs, newest first, with "
                "each run's ID, contract, provider, attempt count, and current status. "
                "Optionally filter by status (created, running, converged, unconverged, "
                "failed, over_budget). Feed run IDs into harness.get_status or "
                "harness.get_trace."
            ),
            input_model=HarnessListRunsInput,
            handler=gateway._harness_list_runs,
            profiles=("curated", "full"),
        ),
        ToolSpec(
            name="harness.list_candidates",
            description="List staged skill candidates emitted by converged harness runs.",
            input_model=EmptyInput,
            handler=gateway._harness_list_candidates,
            profiles=("full",),
        ),
        ToolSpec(
            name="harness.get_candidate",
            description="Read one staged skill candidate by skill ID.",
            input_model=HarnessSkillIdInput,
            handler=gateway._harness_get_candidate,
            profiles=("full",),
        ),
        ToolSpec(
            name="harness.check_drift",
            description="Re-run verifier checks for one staged skill candidate and write drift.json.",
            input_model=HarnessSkillIdInput,
            handler=gateway._harness_check_drift,
            profiles=("full",),
        ),
        ToolSpec(
            name="harness.check_all_drifts",
            description="Run drift checks for all staged skill candidates.",
            input_model=EmptyInput,
            handler=gateway._harness_check_all_drifts,
            profiles=("full",),
        ),
        ToolSpec(
            name="harness.graduate",
            description=(
                "Return the staged candidate for a converged run. "
                "This does not promote it into production skills."
            ),
            input_model=HarnessGraduateInput,
            handler=gateway._harness_graduate,
            profiles=("full",),
        ),
    ]:
        registry.register(spec)
