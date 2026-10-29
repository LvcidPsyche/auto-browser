# Convergence Harness

The convergence harness is the Agent Skill Induction path for auto-browser. A run takes a `TaskContract`, records trace evidence, verifies completion, retries within budget, and emits a staged skill candidate when the run converges.

The harness intentionally stops at staging. Promotion into the production skill corpus remains a governed review step.

## Operator Runbook

1. Start the controller with the full MCP profile when an external MCP client should drive the harness:

```powershell
$env:MCP_TOOL_PROFILE="full"
$env:HARNESS_ROOT=".autonomous/harness"
docker compose up --build
```

2. Write or choose a contract. A minimal local example lives at [`evals/contracts/example_read.json`](../evals/contracts/example_read.json).

3. Run a deterministic local smoke before spending model/browser budget:

```powershell
python -m controller.harness.run --contract evals/contracts/example_read.json --mock-final-url https://example.com --mock-final-text "Example Domain" --json
```

4. Inspect the generated run record under `.autonomous/harness/runs/<run_id>/`.

5. Inspect the staged candidate under `.autonomous/harness/skills/staging/<skill_id>/`.

6. Review `provenance.json`, `envelope.json`, `contract.json`, and `trace.json` before promotion. Promotion is intentionally outside the Stage 0 harness path.

## Local Run

From the repository root:

```powershell
python -m controller.harness.run --contract evals/contracts/example_read.json --mock-final-url https://example.com --mock-final-text "Example Domain" --json
```

Generated artifacts are written under `.autonomous/harness` by default. Controller runtime uses `HARNESS_ROOT`, defaulting to `/data/harness`.

Useful env knobs:

| Variable | Purpose |
| --- | --- |
| `HARNESS_ROOT` | Runtime directory for runs, traces, and staged candidates |
| `HARNESS_VERIFIER` | `programmatic`, `uv`, or `ensemble` |
| `HARNESS_UV_COMMAND` | Pinned command used by `UniversalVerifierAdapter` |
| `HARNESS_EXPLORER_MODEL` | Model recorded for induction/exploration cost provenance |
| `HARNESS_VERIFIER_MODEL` | Model recorded for verifier cost provenance |
| `HARNESS_EXECUTOR_MODEL` | Minimum executor tier recorded on staged skills |

## MCP Tools

The full MCP profile exposes the harness itself:

- `harness.start_convergence`
- `harness.get_status`
- `harness.get_trace`
- `harness.list_runs`
- `harness.list_candidates`
- `harness.get_candidate`
- `harness.graduate`

`harness.graduate` returns the staged candidate metadata only. It does not promote or register the skill into production automatically.

For live browser sessions, call `harness.start_convergence` with `workflow_profile=governed` and a valid approval. Mock-only harness runs can be used for local deterministic smoke tests without attaching to a live session.

Minimal MCP call:

```json
{
  "name": "harness.start_convergence",
  "arguments": {
    "contract": {
      "id": "example-read",
      "goal": "Open the example page and confirm it loaded.",
      "postconditions": [
        {"kind": "url_contains", "value": "example.com"}
      ],
      "budget": {"max_steps": 3, "max_attempts": 1}
    },
    "mock_final_observation": {
      "url": "https://example.com",
      "text": "Example Domain"
    }
  }
}
```

Review staged candidates through MCP before shelling into the harness root:

```json
{"name": "harness.list_candidates", "arguments": {}}
```

```json
{"name": "harness.get_candidate", "arguments": {"skill_id": "example-read-abc12345"}}
```

## Verification

Stage 0 ships with a deterministic `ProgrammaticVerifier` and a dependency-gated `UniversalVerifierAdapter`. The UV adapter returns `unverified` until a pinned Stagehand/Universal Verifier command is configured through `HARNESS_UV_COMMAND`.

Stage 1 is expected to add the CUAVerifierBench regression suite and verifier ensemble voting on top of this adapter boundary.

## Mesh Boundary

When mesh identity is enabled, staged skill candidates are wrapped in the existing Ed25519 `SignedEnvelope` format. The signed payload is credential-free by design: no auth profiles, cookies, storage state, uploads, or browser profile material are embedded.

On a controller with a mesh identity, where candidates are signed, the registry checks a staged candidate on every read. The envelope signs the `contract_hash` and `trace_hash` recorded in `candidate.json`, the hashes of `SKILL.md`, `helper.py` and `test_skill.py`, and a hash of everything else in `candidate.json` except the `files` map and the envelope itself; a candidate that differs from it is refused. The files are hashed in the directory `candidate.json` was read from, and that directory must be named for the candidate's `skill_id` and must stay inside the staging directory once links are resolved, so a copied `candidate.json` is refused. The `files` paths in what `harness.get_candidate` and `harness.list_candidates` return are built from that directory. `contract.json` and `trace.json` are never compared with the recorded hashes, and `provenance.json` and `envelope.json` are not hashed, so a reviewer cannot rely on any of the four.

This gives future mesh replication a provenance format without widening the trust boundary in Stage 0.

Signed candidates are verified immediately after signing. If the mesh envelope cannot be created or verified, staging fails closed instead of writing an ambiguous artifact.

## Stage 0 Limits

- `max_attempts` and `max_wall_seconds` are enforced today. `max_model_calls` and `max_usd` remain provenance fields until real provider cost callbacks land in Stage 1.
- The default inducer emits a replay helper and provenance package; direct API-path discovery is deferred to Stage 1.
- UV integration is adapter-ready but requires a pinned `HARNESS_UV_COMMAND` before it contributes decisive verifier votes.
