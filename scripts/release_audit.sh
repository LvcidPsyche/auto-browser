#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

require_bin() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

require_file() {
  if [[ ! -f "$1" ]]; then
    echo "Missing required release file: $1" >&2
    exit 1
  fi
}

for bin in git docker curl jq python3; do
  require_bin "$bin"
done

echo "Checking launch-critical files..."
for path in \
  README.md \
  LICENSE \
  CONTRIBUTING.md \
  SECURITY.md \
  ROADMAP.md \
  docs/launch.md \
  docs/mcp-clients.md \
  docs/assets/hero.svg \
  examples/README.md \
  examples/claude_desktop_config.json \
  scripts/compose_local.sh \
  scripts/doctor.sh \
  scripts/mcp_stdio_bridge.py; do
  require_file "$path"
done

echo "Validating compose configs..."
./scripts/compose_local.sh config >/tmp/auto-browser-compose.out
./scripts/compose_local.sh -f docker-compose.yml -f docker-compose.isolation.yml config >/tmp/auto-browser-compose-isolation.out

echo "Running controller tests..."
make test

echo "Running readiness smoke..."
DOCTOR_BUILD=1 make doctor

echo "Scanning tracked files for obvious secret-shaped tokens..."
if git grep -nE \
  'sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}' \
  -- . >/tmp/auto-browser-release-audit-secrets.out; then
  cat /tmp/auto-browser-release-audit-secrets.out >&2
  echo "Release audit failed: potential secret-shaped token found." >&2
  exit 1
fi

echo
echo "Release audit passed."
echo "Next manual step: attach a README GIF or screenshot before the public launch."
