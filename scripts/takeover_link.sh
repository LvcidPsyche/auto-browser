#!/usr/bin/env bash
# Print a session's full takeover link, for the person who takes over.
#
#   scripts/takeover_link.sh <session_id>
#
# An isolated session's link carries its own noVNC password in the fragment, so the
# model that drives the session never receives it: only this script, the dashboard
# and GET /sessions/{session_id}/takeover-link return it. The link goes to stdout
# only; the API token is sent on stdin through bearer_curl and never echoed.
# AUTO_BROWSER_BASE_URL overrides the controller's address (default
# http://127.0.0.1:${API_PORT:-8000}).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

source "${ROOT_DIR}/scripts/load_env.sh"

if [[ $# -ne 1 || -z "${1}" ]]; then
  echo "usage: scripts/takeover_link.sh <session_id>" >&2
  exit 2
fi
SESSION_ID="${1}"
# A session id is letters, digits, '.', '_' or '-': anything else would change the request path.
if [[ ! "${SESSION_ID}" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "not a session id: ${SESSION_ID}" >&2
  exit 2
fi

API_BASE_URL="${AUTO_BROWSER_BASE_URL:-http://127.0.0.1:${API_PORT:-8000}}"
RESPONSE="$(bearer_curl -fsS "${API_BASE_URL%/}/sessions/${SESSION_ID}/takeover-link")"

python3 -I - "${RESPONSE}" <<'PY'
import json
import sys

link = json.loads(sys.argv[1]).get("takeover_url")
if not isinstance(link, str) or not link:
    sys.exit("the controller returned no takeover link for this session")
print(link)
PY
