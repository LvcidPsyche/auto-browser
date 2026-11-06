#!/usr/bin/env bash
# Print a session's full takeover link, for the person who takes over.
#
#   scripts/takeover_link.sh <session_id>
#
# An isolated session's link carries its own noVNC password in the fragment, so the
# model that drives the session never receives it: only this script, the dashboard
# and GET /sessions/{session_id}/takeover-link return it. That endpoint also demands
# the takeover token (TAKEOVER_TOKEN, else data/takeover-token), which agents are not
# given. The link goes to stdout only; the API token and the takeover token are sent
# on stdin through takeover_curl and never echoed, and the response reaches Python on
# stdin, never in argv. AUTO_BROWSER_BASE_URL overrides the controller's address
# (default http://127.0.0.1:${API_PORT:-8000}); API_PORT, API_BEARER_TOKEN and
# TAKEOVER_TOKEN are read from .env too.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

source "${ROOT_DIR}/scripts/load_env.sh"
load_repo_env "${ROOT_DIR}/.env"

if [[ $# -ne 1 || -z "${1}" ]]; then
  echo "usage: scripts/takeover_link.sh <session_id>" >&2
  exit 2
fi
SESSION_ID="${1}"
# A session id starts with a letter or digit, then letters, digits, '.', '_' or '-': anything else would change
# the request path ('.' and '..' are normalised out of it by curl).
if [[ ! "${SESSION_ID}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
  echo "not a session id: ${SESSION_ID}" >&2
  exit 2
fi

API_BASE_URL="${AUTO_BROWSER_BASE_URL:-http://127.0.0.1:${API_PORT:-8000}}"
takeover_curl -fsS "${API_BASE_URL%/}/sessions/${SESSION_ID}/takeover-link" | python3 -I -c '
import json
import sys

try:
    link = json.load(sys.stdin).get("takeover_url")
except (ValueError, AttributeError):
    sys.exit("the controller did not answer with a takeover link")
if not isinstance(link, str) or not link:
    sys.exit("the controller returned no takeover link for this session")
print(link)
'
