# Verifier lane adapters

First adapter pass (issue #40) mapping Auto Browser run artifacts into the
external evidence lanes under `benchmarks/cuaverifier/` and
`benchmarks/online_mind2web/`.

`verifier_adapter.py` is pure and dict-based: it consumes a serialized
[`AgentRunResult`](../../controller/app/models.py) and emits one record per lane.
It **never scores** — `verifier_result` is always `None` and every record is
`"scored": false`. Both lanes stay `tracked-not-scored` until their upstream
revision and deterministic subset are pinned (see each lane's manifest
`required_before_scoring`).

## Field mapping

Source is `AgentRunResult` (`provider`, `model`, `goal`, `workflow_profile`,
`status`, `steps[]`, `final_session`). `final_session` is the session summary
(`current_url`, `auth_profile`, `artifact_dir`, `trace_path`, ...).

| Lane field | Source |
|------------|--------|
| `provider` | `run.provider` |
| `workflow_profile` | `run.workflow_profile` |
| `auth_profile` | `run.final_session.auth_profile` |
| `final_url` | `run.final_session.current_url` |
| `action_sequence` | per step: `step.decision.{action,element_id,url,risk_category}` + `step.status` |
| `task_goal` (cuaverifier) | `run.goal` |
| `status` (cuaverifier) | `run.status` |
| `evidence` (cuaverifier) | `final_session.{artifact_dir,trace_path}` + screenshots (pending) |
| `verifier_result` | **not produced by Auto Browser** — filled by the external verifier once pinned |

## Known source gaps

`missing_source_fields()` surfaces runs that can't feed the lanes yet (no
provider, no final URL, or no steps). Screenshot references are not yet threaded
from saved session artifacts into `evidence.screenshots`; that lands when the
lane is pinned and CI enforcement is enabled.

## Usage

```bash
python benchmarks/adapters/verifier_adapter.py path/to/agent_run_result.json
```

Or programmatically:

```python
from verifier_adapter import adapt
records = adapt(run_result_dict)   # {"online_mind2web": {...}, "cuaverifier": {...}}
```
