# WebArena

This lane tracks the first public WebArena-style proof slice for Auto Browser.

`stage0-manifest.json` defines the five task classes that must be wired to a pinned local WebArena environment before any competitive benchmark number is published. Until those contracts run with saved trace, action, screenshot, and model-decision evidence, this lane is **tracked but intentionally unscored**.

## Contracts

`stage0_contracts.py` turns the manifest into typed, validated `TaskContract`
objects and a concrete per-task evidence plan (`trace.zip`, `actions.json`,
`screenshots/`, `model_decisions.json`). The five contracts are:

| id | domain | task class |
|----|--------|------------|
| shopping-order-status-read | shopping | authenticated_read |
| reddit-thread-summarize | forum | read_only |
| gitlab-issue-triage-read | project_management | authenticated_read |
| cms-draft-review-governed | content | governed_write_block |
| maps-business-hours-read | maps | read_only |

## Running

```bash
# Validate the contracts (deterministic, browser-free — this runs in CI):
python benchmarks/webarena/run_stage0.py

# Materialize the evidence layout for each contract (tracked-only unless pinned):
python benchmarks/webarena/run_stage0.py --execute

# Live execution against a provisioned WebArena environment:
WEBARENA_BASE_URL=http://localhost:7770 \
  python benchmarks/webarena/run_stage0.py --execute
```

## Pinning (required before scoring)

The lane stays `tracked-not-scored` until every item in the manifest's
`required_before_scoring` list is done:

1. Pin `environment.revision` to a reviewed [WebArena](https://github.com/web-arena-x/webarena) commit SHA (currently `null`).
2. Provision the five seeded site containers and record their image digests.
3. Execute all five contracts producing full evidence.
4. Only then flip `competitive_score_allowed` to `true`.

`run_stage0.py --execute` will not fabricate a run: with no pinned revision and
no `WEBARENA_BASE_URL`, it writes the evidence scaffold + a `run.json` marked
`tracked-only` and stops.
