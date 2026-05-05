# Agent Evals

`scripts/agent_eval.py` runs a repeatable matrix of browser-agent cases across providers and workflow profiles.
It has two modes:

- Plan mode: prints the provider/profile matrix from `evals/agent_cases.json`.
- Scoring mode: reads saved result JSON files and scores success criteria without starting a browser.
- Execute mode: runs the cases against a live controller with `--execute --base-url`.

Examples:

```powershell
python scripts\agent_eval.py --json
python scripts\agent_eval.py --report-file .\eval-report.md
python scripts\agent_eval.py --results-dir .\eval-results
python scripts\agent_eval.py --execute --base-url http://127.0.0.1:8000 --operator-id local-eval
```

Saved result files are named `<case_id>__<provider>__<workflow_profile>.json`. The harness scores result status,
final URL, observed actions, step counts, and provider errors, then aggregates each provider/profile pair.
CI validates the eval matrix in plan mode so malformed cases cannot slip into a release branch.
