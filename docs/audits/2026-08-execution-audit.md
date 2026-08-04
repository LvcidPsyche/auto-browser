# Execution audit — August 2026

**Status:** complete. All findings fixed and released across v1.5.1–v1.6.0.
**Method:** adversarial audit of this repository, by its maintainers. No user
reported any of it.

This follows the precedent of [`session-isolation-audit.md`](../session-isolation-audit.md):
findings first, reproductions included, limits stated plainly, and the parts
that were wrong labelled wrong.

---

## Summary

Several safety controls were asserted in code and documentation but never
verified end to end. **Three of them silently did nothing while reporting
success.** Every one was green in CI.

The most serious: **screenshot PII redaction had never redacted anything.**

```
ink pixels in the secret region, ORIGINAL : 307
after "redaction"                         : 307   <- unchanged
hits reported by the scrubber             : 1     <- caller believes it worked
```

The function returned a hit, the caller wrote a `pii_redaction / ok` audit
event, and the unredacted screenshot was stored under `/data/artifacts/`, served
over `/artifacts/`, and handed to the model.

## The single root cause

**This codebase verified decisions and mocked effects.**

Every defect below is a cross-boundary representation mismatch, tested only
against a mock of the far side:

| boundary | mismatch |
|---|---|
| Python dict → dict | OCR nested geometry under `bbox`; the scrubber read it flat |
| env string → registry | a typo in a pattern list emptied it instead of failing |
| Python string → JS parser | shell-escape artifacts (`\!`) made a script unparseable |
| module → package | a relative import resolved to a module that does not exist |
| regex → real-world format | an AWS key pattern one character too short to ever match |
| claim → crypto | a chain described as tamper-evident was unsigned |

The *decision* layer held up well — governed write blocking and upload approval
were exercised, the isolation boundary had been audited. The *effect* layer —
did the pixels change, did the regex match a real key, did the JS parse, did the
module import — is where all of it lived.

## Findings

### Privacy controls (fixed in v1.5.1)

1. **Screenshot PII redaction was a silent no-op.** `ocr.py` emits geometry
   nested under `block["bbox"]`; `pii_scrub.py` read flat `x`/`y`/`width`/
   `height`, so every `.get(..., 0)` fell to its default and every redaction
   rectangle computed to `(0,0,0,0)`. The existing test fed flat keys — a shape
   production never emits — which is exactly why nothing caught it.
2. **A typo in `PII_SCRUB_PATTERNS` disabled all scrubbing, fail-open.** Unknown
   names were "silently dropped" by intersecting with the known set, so
   `PII_SCRUB_PATTERNS="emial,phon"` produced an empty set — and an empty but
   non-`None` set makes `scrub_text` skip every pattern. `summary()` still
   reported `enabled: True`.
3. **The AWS access-key pattern could never match.** 19 characters where real key
   ids are 20; the trailing lookahead rejected every one.
4. **Most screenshots were never redacted at all.** `PII_SCRUB_SCREENSHOT=true`
   covered only `normal`/`rich` observes. The `fast` preset, manual captures, and
   the before/after snapshot taken on *every action* wrote unredacted PNGs.
5. **Redaction was starved by a token budget.** `image_to_data` returns one block
   per *word*, and the list was truncated at 20 — so only the first ~20 words of
   a page could ever be redacted. A navigation bar exhausted the budget.

### Credential handling (fixed in v1.5.2–v1.5.3)

6. **`REQUIRE_AUTH_STATE_ENCRYPTION=true` loaded plaintext auth state.** Enforced
   only at construction — "is a key configured?" — and never on the read path, so
   plaintext cookies went straight into a live browser context while the
   deployment believed encryption was mandatory.
7. **Auth-state encryption was classified by filename, not content**, so the
   classification was wrong in both directions.
8. **Rate limiting could not throttle bearer-token brute force.** The bucket key
   derived from the `authorization` header, so every guessed token got its own
   fresh counter.
9. **Auth-profile export and import left no audit record**, while
   `save_storage_state` — which merely *writes* the same material — was fully
   instrumented.

### Correctness and lifecycle (fixed in v1.5.1–v1.5.3)

10. **`browser.export_script` had never worked over MCP** — a relative import
    resolved to a nonexistent module; every call raised `ModuleNotFoundError`,
    collapsed into an opaque "Tool execution failed". No test touched it.
11. **The stealth init script was never valid JavaScript.** No stealth patch had
    ever applied. (Low impact: stealth is default-off and an explicit non-goal.
    The defect *class* is the point.)
12. **Cron-triggered sessions were never closed.** With `MAX_SESSIONS` defaulting
    to 1, the first cron fire consumed the only slot permanently.
13. **The maintenance cleanup loop died permanently on its first exception**, and
    a corrupt cron store erased every cron job.
14. **MCP version negotiation violated a spec MUST**, and `-32002` meant two
    different things.

### The gap that hid much of it

**The Docker CI job ran 566 of 637 tests.** `controller-tests` used
`unittest discover`, which only collects `unittest.TestCase` subclasses — so
`test_1_0.py` (61 tests: mesh Ed25519, peer registry, delegation policy, stealth,
network inspector, CDP, workflow engine), `test_pii_scrub.py` (8), and
`test_readiness.py` (4) were invisible to it.

The one required check that validates the **shipped container** ran zero
PII-scrubber and zero mesh-crypto tests, and any new pytest-style file would have
been dropped from it with no warning.

## Scope of exposure — stated honestly

auto-browser is **local-first and single-tenant**. For most operators the
affected artifacts never left their own machine, and there is no remote attacker
path in any of these findings — no privilege escalation, no unauthenticated
trigger, no reported exploitation.

The real exposure surfaces are the three places evidence leaves the box:

- **shared session links** (`/share/{token}`)
- **auth-profile exports**
- **remote witness sinks**

If you used any of those with `PII_SCRUB_SCREENSHOT` enabled and believed
screenshots were redacted, they were not. If you set
`REQUIRE_AUTH_STATE_ENCRYPTION=true` and had plaintext profiles predating it,
those kept loading.

**A correction worth making explicitly:** the lead that opened finding 6 was
recorded as "an encrypted file renamed without `.enc` is handed to Playwright as
plaintext." That is **wrong** — the file contains ciphertext, so nothing
plaintext leaked; the envelope was passed through verbatim, which is a silent
auth failure. The genuinely serious defect was the one sitting beside it. A
lead's stated mechanism is a hypothesis, not a finding.

## What changed so the class cannot recur

Fixing the instances was the smaller half. These gates target the *class*:

| gate | what it makes impossible |
|---|---|
| Pixel-diff redaction test, driven through the real `_extract_sync` | the two modules drifting apart again |
| Canonical sample for all 16 PII patterns + completeness test | a pattern shipping with no behavioural assertion (12 had none) |
| Fail-closed pattern parsing | a typo silently disabling scrubbing |
| Relative-import walk across all 129 app modules | a lazy in-function import resolving nowhere |
| `node --check` on all 15 shipped JS constants | JS that Python tooling can never validate |
| Docker CI job switched to pytest | the container check silently skipping 71 tests |
| Python-floor invariant parsed from the CI matrix | a package claiming a version CI does not run |
| Compose-vs-config model parity test | Docker users silently running different models |

**637 → 873 tests.**

Every fix was confirmed **red before green**: the change was reverted, the test
watched to fail, then restored. Where a claim came from an automated reviewer, it
was reproduced by execution before being acted on — reading the code only
confirms you read it the same way; counting pixels is what made finding 1 a fact.

## Two tests that were protecting bugs

Worth recording, because it is the most uncomfortable part:

- `test_scheduler_registration_and_store_error_paths` asserted `_load() == {}`
  for a corrupt cron store — pinning as correct the exact behaviour that erased
  every cron job on the next save.
- `test_screenshot_redaction_and_invalid_image_fallback` asserts that when
  Pillow cannot decode an image the **original un-redacted** bytes are returned.
  That is a deliberate availability trade-off, but the test name does not say so.
  Flagged, not silently changed.

## What remains

- **Tail truncation of a witness chain** is undetectable without an external
  anchor. Signing (v1.6.0) does not fix this and does not claim to; `verify()`
  reports the head so a holder of a previously published head can check.
- **Key compromise** still allows rewriting a signed history. Signing raises the
  bar from "anyone with the file" to "anyone with the key".
- **Multi-worker deployments** would fork the receipt chain. Documented in
  `witness.py`; out of scope until multi-worker is actually supported.

## Timeline

| date | event |
|---|---|
| 2026-08-03 | Audit run; findings reproduced by execution |
| 2026-08-04 | v1.5.1 — privacy controls, broken MCP surface, CI runner |
| 2026-08-04 | v1.5.2 — lifecycle, credential surface, published packages |
| 2026-08-04 | v1.5.3 — auth-state fail-closed, remaining operational leaks |
| 2026-08-04 | v1.6.0 — witness chain signing, verifiable evidence bundles |
