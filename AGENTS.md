# AGENTS.md — auto-browser

For coding agents working in this repo. Humans: start with `README.md` and `CONTRIBUTING.md`.

## Commands

```
make help            # every target
make lint            # ruff across the repo (config: ruff.toml)
make test-local      # controller suite on the host (Python 3.11+ with ./controller[dev])
make test            # same suite in Docker
make release-audit   # launch-prep audit; run before any release PR
```

- Use a virtualenv for the host suite (`python -m pip install -e ./controller[dev]`) and point
  `AUTO_BROWSER_PYTHON_BIN` at it if several interpreters exist. The controller's FastAPI floor
  conflicts with other projects' pins, so don't share a global interpreter.
- `make test-local` runs from `controller/` on purpose: pydantic-settings reads `.env` from the
  working directory, and a developer's root `.env` turns on auth the tests don't send (~138
  false 400s). If you invoke the tests yourself, run them from `controller/` too.
- Test-suite defaults belong in `controller/tests/__init__.py`, not `conftest.py`: the
  controller image ships `tests/__init__.py`, so a conftest-only default passes on the host and
  fails in Docker.

## Invariants CI enforces

- **Version parity:** every version string in the repo must match (`scripts/check_version_parity.py`
  lists them). A release bumps all of them together with `python scripts/bump_version.py X.Y.Z`.
- **Playwright pin parity:** the controller (pip) and `browser-node` (npm) Playwright versions must
  be identical (`scripts/check_playwright_pins.py`).
- Security-sensitive changes (auth, isolation, witness receipts, PII scrubbing) follow `SECURITY.md`.

## Releases

A `v*` tag runs `.github/workflows/release.yml`, which publishes only three packages: `client/`,
`integrations/langchain/` and `packaging/auto-browser-mcp/`. Cut a release only when
`git diff <last-tag>..main -- client/ integrations/langchain/ packaging/auto-browser-mcp/` is
non-empty. `main` is protected: changes land through a PR with green CI, one merge at a time.
