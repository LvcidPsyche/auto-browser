# Agent Evals

`scripts/agent_eval.py` runs a repeatable matrix of browser-agent cases across providers and workflow profiles.
It has four modes:

- Plan mode: prints the provider/profile matrix from `evals/agent_cases.json`.
- Mock mode: scores deterministic local results with `--mock`.
- Scoring mode: reads saved result JSON files and scores success criteria without starting a browser.
- Execute mode: runs the cases against a live controller with `--execute --base-url`.
- Fixture mode: validates local HTML fixtures that mirror the release-critical workflows without network access.

Examples:

```powershell
python scripts\agent_eval.py --json
python scripts\agent_eval.py --mock
python scripts\agent_eval.py --report-file .\eval-report.md
python scripts\agent_eval.py --results-dir .\eval-results
python scripts\agent_eval.py --execute --base-url http://127.0.0.1:8000 --operator-id local-eval
python scripts\fixture_eval.py
```

Saved result files are named `<case_id>__<provider>__<workflow_profile>.json`. The harness scores result status,
final URL, observed actions, step counts, and provider errors, then aggregates each provider/profile pair.
Cases can also set `expected_status_by_profile` so a single case can prove that `fast` and `governed`
diverge, for example `fast=max_steps_reached` while `governed=approval_required`.
CI validates the eval matrix in plan mode so malformed cases cannot slip into a release branch.

`make eval` runs mock mode locally, and `make fixture-eval` validates the static fixture coverage for auth-profile reuse,
popup/download recovery, governed write blocking, upload approval, resume-after-failure, and multi-tab recovery.
Use live execute mode only when provider credentials and a running controller are available.
