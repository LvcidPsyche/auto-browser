#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

source "${ROOT_DIR}/scripts/load_env.sh"
load_repo_env "${ROOT_DIR}/.env"

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.isolation.yml)
# Two sessions at once, for the cross-session reach probe below.
export MAX_SESSIONS=2
API_PORT_VALUE="${API_PORT:-8000}"
API_BASE_URL="http://127.0.0.1:${API_PORT_VALUE}"

cleanup() {
  docker ps -aq --filter label=auto-browser.managed=true | xargs -r docker rm -f >/dev/null 2>&1 || true
  "${COMPOSE[@]}" down --remove-orphans >/dev/null 2>&1 || true
  # Only after compose down: the controller must be gone, or it still holds
  # an endpoint on each session network.
  docker network ls -q --filter label=auto-browser.managed=true | xargs -r docker network rm >/dev/null 2>&1 || true
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

SESSION_JSON="$(bearer_curl -fsS "${API_BASE_URL}/sessions" -X POST -H 'content-type: application/json' -d '{"name":"isolated-smoke","start_url":"https://example.com"}')"
read -r SESSION_ID CONTAINER_NAME NOVNC_PORT SESSION_NETWORK <<<"$(python3 - <<'PY' "${SESSION_JSON}"
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
# Its own network (per_session, the default), not the compose network.
assert runtime["network"] == f"{runtime['container_name']}-net", payload
print(payload["id"], runtime["container_name"], runtime["novnc_port"], runtime["network"])
PY
)"

wait_for \
  "isolated browser container" \
  "docker ps --format '{{.Names}}' | grep -Fx '${CONTAINER_NAME}'" \
  30 \
  1

# The session network holds exactly the controller and this browser.
NETWORK_MEMBERS="$(docker network inspect "${SESSION_NETWORK}" -f '{{len .Containers}}')"
if [[ "${NETWORK_MEMBERS}" != "2" ]]; then
  echo >&2 "session network ${SESSION_NETWORK} has ${NETWORK_MEMBERS} containers, expected 2"
  docker network inspect "${SESSION_NETWORK}" >&2
  exit 1
fi
echo "session network ok: ${SESSION_NETWORK} holds 2 containers"

OBSERVE_JSON="$(bearer_curl -fsS "${API_BASE_URL}/sessions/${SESSION_ID}/observe")"
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

REMOTE_ACCESS_JSON="$(bearer_curl -fsS "${API_BASE_URL}/remote-access?session_id=${SESSION_ID}")"
python3 - <<'PY' "${REMOTE_ACCESS_JSON}" "${NOVNC_PORT}"
import json
import sys

payload = json.loads(sys.argv[1])
novnc_port = int(sys.argv[2])
assert payload["status"] == "local_only", payload
assert payload["takeover_url"] == f"http://127.0.0.1:{novnc_port}/vnc.html?autoconnect=true&resize=scale", payload
print("isolated remote-access endpoint ok")
PY

# NOVNC_PORT is the session's published noVNC port, as the API reported it above.
python3 "${ROOT_DIR}/scripts/check_vnc_exposure.py" "${CONTAINER_NAME}" "${NOVNC_PORT}" ${VNC_PASSWORD:+--expect-password}

# Cross-session probe: a second session, B, on its own network. From A's
# browser container, B's Playwright server and the shared browser-node must be
# unreachable, by name and by address. The controller stays reachable from
# every session (it joins each network), which is expected for now.
B_JSON="$(bearer_curl -fsS "${API_BASE_URL}/sessions" -X POST -H 'content-type: application/json' -d '{"name":"isolated-smoke-b","start_url":"https://example.com"}')"
read -r B_SESSION_ID B_CONTAINER_NAME B_NETWORK <<<"$(python3 - <<'PY' "${B_JSON}"
import json
import sys

payload = json.loads(sys.argv[1])
runtime = payload["isolation"]["runtime"]
print(payload["id"], runtime["container_name"], runtime["network"])
PY
)"
if [[ "${B_NETWORK}" == "${SESSION_NETWORK}" ]]; then
  echo >&2 "sessions A and B share network ${B_NETWORK}"
  exit 1
fi
container_ip() {
  docker inspect "$1" -f "{{(index .NetworkSettings.Networks \"$2\").IPAddress}}"
}
B_IP="$(container_ip "${B_CONTAINER_NAME}" "${B_NETWORK}")"
BROWSER_NODE_ID="$("${COMPOSE[@]}" ps -q browser-node)"
BROWSER_NODE_IP="$(docker inspect "${BROWSER_NODE_ID}" -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' | awk '{print $1}')"
CONTROLLER_ID="$("${COMPOSE[@]}" ps -q controller)"
CONTROLLER_IP_ON_A="$(container_ip "${CONTROLLER_ID}" "${SESSION_NETWORK}")"
: "${B_IP:?no address for session B}" "${BROWSER_NODE_IP:?no address for browser-node}"
: "${CONTROLLER_IP_ON_A:?the controller is not on the network of session A}"

must_fail_from_a() {
  local description="$1"
  shift
  if docker exec "${CONTAINER_NAME}" "$@" >/dev/null 2>&1; then
    echo >&2 "cross-session probe FAILED: A reached ${description} ($*)"
    exit 1
  fi
  echo "probe ok: A cannot reach ${description}"
}
# Controls, so a missing tool cannot pass the probes below: A resolves its own
# name, and curl in A reaches A's own Playwright port.
docker exec "${CONTAINER_NAME}" getent hosts "${CONTAINER_NAME}" >/dev/null
docker exec "${CONTAINER_NAME}" curl -sS --max-time 3 -o /dev/null "http://127.0.0.1:9223/"
must_fail_from_a "B:9223/json by name" curl -fsS --max-time 3 "http://${B_CONTAINER_NAME}:9223/json"
must_fail_from_a "B:9223/json at ${B_IP}" curl -fsS --max-time 3 "http://${B_IP}:9223/json"
must_fail_from_a "B:6080 at ${B_IP}" curl -fsS --max-time 3 "http://${B_IP}:6080/"
must_fail_from_a "browser-node by name" getent hosts browser-node
# No -f: any HTTP answer from the Playwright server would be a reach.
must_fail_from_a "browser-node:9223 at ${BROWSER_NODE_IP}" curl -sS --max-time 3 -o /dev/null "http://${BROWSER_NODE_IP}:9223/"
CONTROLLER_STATUS="$(docker exec "${CONTAINER_NAME}" curl -sS --max-time 3 -o /dev/null -w '%{http_code}' "http://${CONTROLLER_IP_ON_A}:8000/readyz" || true)"
if [[ -z "${CONTROLLER_STATUS}" || "${CONTROLLER_STATUS}" == "000" ]]; then
  echo >&2 "expected the controller to answer from A at ${CONTROLLER_IP_ON_A}:8000, got '${CONTROLLER_STATUS}'"
  exit 1
fi
echo "probe ok: A reaches the controller at ${CONTROLLER_IP_ON_A}:8000 (HTTP ${CONTROLLER_STATUS}; expected for now)"

bearer_curl -fsS "${API_BASE_URL}/sessions/${B_SESSION_ID}" -X DELETE >/dev/null
wait_for "session B network removal" "! docker network inspect '${B_NETWORK}'" 30 1

CLOSE_JSON="$(bearer_curl -fsS "${API_BASE_URL}/sessions/${SESSION_ID}" -X DELETE)"
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

wait_for "session network removal" "! docker network inspect '${SESSION_NETWORK}'" 30 1
echo "session network removed after close"

echo "isolated session smoke test passed"
