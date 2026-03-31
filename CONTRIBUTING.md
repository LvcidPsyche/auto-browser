# Contributing

Thanks for helping make Auto Browser better.

Please also read `CODE_OF_CONDUCT.md`.

## What this project is

Auto Browser is an open-source, supervised browser control plane for authorized workflows.

It is for:
- operator-assisted browser automation
- reusable auth/session workflows
- human takeover when pages get brittle
- MCP / API-driven browser control

It is not for:
- anti-bot bypass
- CAPTCHA solving
- stealth/evasion work
- unauthorized scraping or account access

## Development setup

```bash
cp .env.example .env
./scripts/compose_local.sh up --build
```

Useful commands:

```bash
make lint
make help
make doctor
make release-audit
make test
make test-local
```

Host-side tests require Python 3.11+. Install the editable controller package with:

```bash
python3.11 -m pip install -e ./controller[dev]
```

## Before opening a PR

Please:
- keep diffs small
- prefer obvious code over clever code
- add or update tests when behavior changes
- update docs for new endpoints, env vars, or workflows

Minimum local checks:

```bash
make release-audit
```

## Areas that are especially welcome

- browser reliability and recovery
- auth profile workflows
- MCP tooling and interoperability
- docs, examples, and deployment guides
- isolated session ergonomics
- approval/audit UX

## Areas that are out of scope

We will close or decline contributions that add:
- stealth / anti-detection features
- CAPTCHA solving integrations
- deceptive fingerprint spoofing
- features aimed at bypassing site protections

## Issue quality bar

Good issues include:
- exact steps
- expected vs actual behavior
- logs or screenshots
- environment details
- whether the issue reproduces with `make doctor`

## Security

If you find a security issue, do not open a public exploit issue first.
See `SECURITY.md`.
