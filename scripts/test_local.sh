#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

source "${ROOT_DIR}/scripts/python_env.sh"
PYTHON_BIN="$(require_python311_bin)"
export PYTHONPATH="${ROOT_DIR}/controller${PYTHONPATH:+:${PYTHONPATH}}"

if ! "${PYTHON_BIN}" - <<'PY' >/dev/null 2>&1
import importlib.util

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
raise SystemExit(0 if not missing else 1)
PY
then
  cat >&2 <<EOF
Missing Python dependencies for host-side controller tests.

Install them with:
  ${PYTHON_BIN} -m pip install -e ./controller[dev]
EOF
  exit 1
fi

exec "${PYTHON_BIN}" -m unittest discover -s "${ROOT_DIR}/controller/tests" -v
