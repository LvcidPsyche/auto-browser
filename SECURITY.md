# Security Policy

## Supported scope

Security reports are welcome for:
- auth-state handling
- secret exposure in logs/artifacts
- approval bypasses
- API auth issues
- isolation boundary failures
- takeover URL exposure
- unsafe file handling

## Out of scope

The following are not considered valid security goals for this project:
- anti-bot bypass
- CAPTCHA solving
- stealth / undetectable automation
- deceptive fingerprinting

## Reporting

Report security issues privately via GitHub's private vulnerability reporting:

**[Open a private security advisory](https://github.com/LvcidPsyche/auto-browser/security/advisories/new)**

Do not open a public issue for security problems. If you cannot use GitHub
advisories, open a regular issue saying only "security report — need a private
channel" with no details, and the maintainer will follow up.

Include:
- impact
- affected version/commit
- repro steps
- logs, screenshots, or PoC if available

## Published advisories

Issues found by the maintainers are disclosed the same way a reported one would
be, and the audits that found them are published in full:

- [GHSA-xmh3-cw7j-9gp5](https://github.com/LvcidPsyche/auto-browser/security/advisories/GHSA-xmh3-cw7j-9gp5)
  — `Runtime.evaluate` reachable through the "safe" raw-CDP allowlist, plus a
  Codespaces overlay that published the API unauthenticated (fixed in 1.6.1).
  Reported privately by [@rafaelfiguereod-stack](https://github.com/rafaelfiguereod-stack)
- [GHSA-32ph-8hp6-7qgj](https://github.com/LvcidPsyche/auto-browser/security/advisories/GHSA-32ph-8hp6-7qgj)
  — safety controls that reported success while not functioning (fixed in 1.5.1
  and 1.5.3). Write-up: [`docs/audits/2026-08-execution-audit.md`](./docs/audits/2026-08-execution-audit.md)
- [`docs/session-isolation-audit.md`](./docs/session-isolation-audit.md) — response
  to an external session-poisoning claim

## Handling goals

The project aims to:
- acknowledge reports quickly
- confirm severity and scope
- ship the smallest safe fix
- document user-facing mitigation steps when needed

"Quickly" was once seven weeks. GHSA-xmh3-cw7j-9gp5 was reported privately on
2026-06-17 and sat in triage until 2026-08-08, because a private report shows up
in none of the views a maintainer opens day to day. So acknowledgement is no
longer left to attention: `scripts/check_open_advisories.py` fails the release
audit while any report is still waiting in triage, which puts the check on the
one process that is guaranteed to run before users are affected by anything.
