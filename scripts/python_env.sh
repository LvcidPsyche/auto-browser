#!/usr/bin/env bash

# Default acceptance probe: Python 3.10+. Callers may pass a stricter probe
# (e.g. version + required packages) as $1 to the resolve/require functions;
# a candidate is accepted when the probe snippet exits 0.
AUTO_BROWSER_DEFAULT_PROBE='import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'

resolve_python310_bin() {
  local probe="${1:-${AUTO_BROWSER_DEFAULT_PROBE}}"
  local candidate
  local -a candidates=()
  if [[ -n "${AUTO_BROWSER_PYTHON_BIN:-}" ]]; then
    candidates+=("${AUTO_BROWSER_PYTHON_BIN}")
  fi
  candidates+=(python3 python3.13 python3.12 python3.11 python3.10)

  for candidate in "${candidates[@]}"; do
    [[ -n "${candidate}" ]] || continue
    if ! command -v "${candidate}" >/dev/null 2>&1; then
      continue
    fi
    if "${candidate}" -c "${probe}" >/dev/null 2>&1; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done

  # Windows Git Bash: interpreters often exist only via the `py` launcher
  # (no python3 on PATH). Resolve a launcher hit to its real executable so
  # callers get a plain quoted-safe path.
  if command -v py >/dev/null 2>&1; then
    local version_flag resolved
    for version_flag in -3.13 -3.12 -3.11 -3.10 -3; do
      resolved="$(py "${version_flag}" -c 'import sys; print(sys.executable)' 2>/dev/null)" || continue
      [[ -n "${resolved}" ]] || continue
      if "${resolved}" -c "${probe}" >/dev/null 2>&1; then
        printf '%s\n' "${resolved}"
        return 0
      fi
    done
  fi

  return 1
}

require_python310_bin() {
  local probe="${1:-}"
  local python_bin=""
  local detected_version=""

  if python_bin="$(resolve_python310_bin "${probe:-${AUTO_BROWSER_DEFAULT_PROBE}}")"; then
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
