#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

source "${ROOT_DIR}/scripts/python_env.sh"

# Accept an interpreter only if it is 3.10+ AND has the controller's deps —
# probing both at once lets the resolver skip a bare system Python and keep
# looking (e.g. the Windows `py` launcher) instead of failing on the first hit.
CONTROLLER_DEPS_PROBE='import importlib.util
import sys

if sys.version_info < (3, 10):
    raise SystemExit(1)
required = [
    "apscheduler",
    "cryptography",
    "docker",
    "fastapi",
    "httpx",
    "playwright",
    "prometheus_client",
    "pydantic_settings",
    "PIL",
    "pyotp",
    "pytesseract",
    "redis",
]
missing = [name for name in required if importlib.util.find_spec(name) is None]
raise SystemExit(0 if not missing else 1)'

if ! PYTHON_BIN="$(resolve_python310_bin "${CONTROLLER_DEPS_PROBE}")"; then
  cat >&2 <<EOF
No Python 3.10+ interpreter with the controller's dependencies was found.

Install them with:
  python3 -m pip install -e ./controller[dev]

or point AUTO_BROWSER_PYTHON_BIN at an interpreter that has them.
If you only need the containerized path, use \`make test\`.
EOF
  exit 1
fi
export PYTHONPATH="${ROOT_DIR}/controller${PYTHONPATH:+:${PYTHONPATH}}"

# Run from controller/, not the repo root: pydantic-settings loads .env from the
# current working directory, and a developer's root .env (bearer token, operator
# id, rate limits) turns on auth the tests don't send — ~138 route tests then
# fail with 400s that never happen in CI or Docker, which have no .env.
cd "${ROOT_DIR}/controller"
exec "${PYTHON_BIN}" -m unittest discover -s tests -v
