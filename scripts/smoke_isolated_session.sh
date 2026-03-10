#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.isolation.yml)
API_PORT_VALUE="${API_PORT:-8000}"
API_BASE_URL="http://127.0.0.1:${API_PORT_VALUE}"

cleanup() {
  docker ps -aq --filter label=auto-browser.managed=true | xargs -r docker rm -f >/dev/null 2>&1 || true
  "${COMPOSE[@]}" down --remove-orphans >/dev/null 2>&1 || true
}

wait_for() {
  local description="$1"
  local command="$2"
  local attempts="${3:-60}"
  local sleep_seconds="${4:-1}"
  local i
  for ((i=1; i<=attempts; i++)); do
    if bash -lc "${command}" >/dev/null 2>&1; then
      return 0
    fi
    sleep "${sleep_seconds}"
  done
  echo >&2 "timed out waiting for ${description}"
  return 1
}

trap cleanup EXIT

"${COMPOSE[@]}" up -d --build browser-node controller
wait_for "controller readiness" "curl -fsS ${API_BASE_URL}/readyz" 120 2

SESSION_JSON="$(curl -fsS "${API_BASE_URL}/sessions" -X POST -H 'content-type: application/json' -d '{"name":"isolated-smoke","start_url":"https://example.com"}')"
read -r SESSION_ID CONTAINER_NAME NOVNC_PORT <<<"$(python3 - <<'PY' "${SESSION_JSON}"
import json
import sys

payload = json.loads(sys.argv[1])
assert payload["isolation"]["mode"] == "docker_ephemeral", payload
assert payload["isolation"]["shared_browser_process"] is False, payload
assert payload["isolation"]["shared_takeover_surface"] is False, payload
assert payload["remote_access"]["status"] == "local_only", payload
runtime = payload["isolation"]["runtime"]
expected_takeover = f"http://127.0.0.1:{runtime['novnc_port']}/vnc.html?autoconnect=true&resize=scale"
assert payload["takeover_url"] == expected_takeover, payload
print(payload["id"], runtime["container_name"], runtime["novnc_port"])
PY
)"

wait_for \
  "isolated browser container" \
  "docker ps --format '{{.Names}}' | grep -Fx '${CONTAINER_NAME}'" \
  30 \
  1

OBSERVE_JSON="$(curl -fsS "${API_BASE_URL}/sessions/${SESSION_ID}/observe")"
python3 - <<'PY' "${OBSERVE_JSON}" "${CONTAINER_NAME}" "${NOVNC_PORT}"
import json
import sys

payload = json.loads(sys.argv[1])
container_name = sys.argv[2]
novnc_port = int(sys.argv[3])
assert payload["session"]["isolation"]["mode"] == "docker_ephemeral", payload
assert payload["session"]["isolation"]["runtime"]["container_name"] == container_name, payload
assert payload["remote_access"]["status"] == "local_only", payload
assert payload["takeover_url"] == f"http://127.0.0.1:{novnc_port}/vnc.html?autoconnect=true&resize=scale", payload
assert payload["url"] == "https://example.com/", payload
print("isolated observe ok")
PY

REMOTE_ACCESS_JSON="$(curl -fsS "${API_BASE_URL}/remote-access?session_id=${SESSION_ID}")"
python3 - <<'PY' "${REMOTE_ACCESS_JSON}" "${NOVNC_PORT}"
import json
import sys

payload = json.loads(sys.argv[1])
novnc_port = int(sys.argv[2])
assert payload["status"] == "local_only", payload
assert payload["takeover_url"] == f"http://127.0.0.1:{novnc_port}/vnc.html?autoconnect=true&resize=scale", payload
print("isolated remote-access endpoint ok")
PY

CLOSE_JSON="$(curl -fsS "${API_BASE_URL}/sessions/${SESSION_ID}" -X DELETE)"
python3 - <<'PY' "${CLOSE_JSON}"
import json
import sys
payload = json.loads(sys.argv[1])
assert payload["closed"] is True, payload
assert payload["session"]["status"] == "closed", payload
print("isolated close ok")
PY

wait_for \
  "isolated browser container removal" \
  "! docker ps -a --format '{{.Names}}' | grep -Fx '${CONTAINER_NAME}'" \
  30 \
  1

echo "isolated session smoke test passed"
