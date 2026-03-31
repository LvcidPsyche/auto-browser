#!/usr/bin/env bash

resolve_python310_bin() {
  local candidate
  local -a candidates=()
  if [[ -n "${AUTO_BROWSER_PYTHON_BIN:-}" ]]; then
    candidates+=("${AUTO_BROWSER_PYTHON_BIN}")
  fi
  candidates+=(python3 python3.11)

  for candidate in "${candidates[@]}"; do
    [[ -n "${candidate}" ]] || continue
    if ! command -v "${candidate}" >/dev/null 2>&1; then
      continue
    fi
    if "${candidate}" - <<'PY' >/dev/null 2>&1
import sys

raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
    then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done

  return 1
}

require_python310_bin() {
  local python_bin=""
  local detected_version=""

  if python_bin="$(resolve_python310_bin)"; then
    printf '%s\n' "${python_bin}"
    return 0
  fi

  if command -v python3 >/dev/null 2>&1; then
    detected_version="$(python3 - <<'PY'
import sys

print(sys.version.split()[0])
PY
)"
  else
    detected_version="python3 not found"
  fi

  cat >&2 <<EOF
Python 3.10+ is required for local auto-browser developer scripts.
Detected: ${detected_version}

Install Python 3.10+ or set AUTO_BROWSER_PYTHON_BIN to a compatible interpreter.
If you only need the containerized path, use \`make test\`.
EOF
  exit 1
}
