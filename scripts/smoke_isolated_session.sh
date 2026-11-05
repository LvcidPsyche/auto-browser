#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

source "${ROOT_DIR}/scripts/load_env.sh"
source "${ROOT_DIR}/scripts/smoke_lib.sh"
# This run's own compose project and session-container prefix (see smoke_lib.sh).
smoke_setup

COMPOSE=(docker compose -p "${SMOKE_PROJECT}" -f docker-compose.yml -f docker-compose.isolation.yml)
# Two sessions at once, for the cross-session reach probe below.
export MAX_SESSIONS=2
API_PORT_VALUE="${API_PORT:-8000}"
API_BASE_URL="http://127.0.0.1:${API_PORT_VALUE}"

cleanup() {
  smoke_cleanup
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

smoke_preflight isolated
trap cleanup EXIT

"${COMPOSE[@]}" up -d --build browser-node controller
wait_for "controller readiness" "curl -fsS ${API_BASE_URL}/readyz" 120 2

SESSION_JSON="$(bearer_curl -fsS "${API_BASE_URL}/sessions" -X POST -H 'content-type: application/json' -d '{"name":"isolated-smoke","start_url":"https://example.com"}')"
read -r SESSION_ID CONTAINER_NAME NOVNC_PORT SESSION_NETWORK <<<"$(python3 -I - <<'PY' "${SESSION_JSON}"
import json
import sys

payload = json.loads(sys.argv[1])
assert payload["isolation"]["mode"] == "docker_ephemeral", payload
assert payload["isolation"]["shared_browser_process"] is False, payload
assert payload["isolation"]["shared_takeover_surface"] is False, payload
assert payload["remote_access"]["status"] == "local_only", payload
runtime = payload["isolation"]["runtime"]
expected_takeover = f"http://127.0.0.1:{runtime['novnc_port']}/vnc.html?autoconnect=true&resize=scale"
# A handoff response: the URL carries this session's own noVNC password.
import re
assert re.fullmatch(re.escape(expected_takeover) + r"#password=[A-Za-z0-9]{8}", payload["takeover_url"]), payload
assert payload["remote_access"]["takeover_url"] == payload["takeover_url"], payload
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

# Chromium's own sandbox, measured inside a browser container: the state its server.mjs wrote, a live /proc check
# (renderers in their own user namespace under one more seccomp filter) and chrome://sandbox. Printed, not asserted:
# BROWSER_SANDBOX=auto falls back where the host refuses unprivileged user namespaces, and says why.
sandbox_report() {
  docker exec -u browser "$1" node /opt/browser-node/sandbox-report.mjs "$2"
}
sandbox_report "${CONTAINER_NAME}" "session A"

OBSERVE_JSON="$(bearer_curl -fsS "${API_BASE_URL}/sessions/${SESSION_ID}/observe")"
python3 -I - <<'PY' "${OBSERVE_JSON}" "${CONTAINER_NAME}" "${NOVNC_PORT}"
import json
import sys

payload = json.loads(sys.argv[1])
container_name = sys.argv[2]
novnc_port = int(sys.argv[3])
assert payload["session"]["isolation"]["mode"] == "docker_ephemeral", payload
assert payload["session"]["isolation"]["runtime"]["container_name"] == container_name, payload
assert payload["remote_access"]["status"] == "local_only", payload
# Not a handoff response: observations reach the model, so no password anywhere in them.
assert payload["takeover_url"] == f"http://127.0.0.1:{novnc_port}/vnc.html?autoconnect=true&resize=scale", payload
assert "#password=" not in json.dumps(payload), "observe carries the noVNC password"
assert payload["url"] == "https://example.com/", payload
print("isolated observe ok (no password in the observation)")
PY

REMOTE_ACCESS_JSON="$(bearer_curl -fsS "${API_BASE_URL}/remote-access?session_id=${SESSION_ID}")"
python3 -I - <<'PY' "${REMOTE_ACCESS_JSON}" "${NOVNC_PORT}"
import json
import sys

payload = json.loads(sys.argv[1])
novnc_port = int(sys.argv[2])
assert payload["status"] == "local_only", payload
expected = f"http://127.0.0.1:{novnc_port}/vnc.html?autoconnect=true&resize=scale"
assert payload["takeover_url"].startswith(expected + "#password="), payload
print("isolated remote-access endpoint ok (takeover URL carries the session password)")
PY

# NOVNC_PORT is the session's published noVNC port, as the API reported it above.
# Every isolated container has its own noVNC password, whatever VNC_PASSWORD is.
python3 -I "${ROOT_DIR}/scripts/check_vnc_exposure.py" "${CONTAINER_NAME}" "${NOVNC_PORT}" --expect-password

# Cross-session probe: a second session, B, on its own network. From A's
# browser container, B's Playwright server and the shared browser-node must be
# unreachable, by name and by address. The controller joins each network but
# refuses every request from one with 403.
B_JSON="$(bearer_curl -fsS "${API_BASE_URL}/sessions" -X POST -H 'content-type: application/json' -d '{"name":"isolated-smoke-b","start_url":"https://example.com"}')"
read -r B_SESSION_ID B_CONTAINER_NAME B_NETWORK <<<"$(python3 -I - <<'PY' "${B_JSON}"
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
sandbox_report "${B_CONTAINER_NAME}" "session B"
sandbox_report "${BROWSER_NODE_ID}" "browser-node"

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
must_fail_from_a "B:6080 by name" curl -fsS --max-time 3 "http://${B_CONTAINER_NAME}:6080/"
# Docker lets a published port (noVNC 6080, even on 127.0.0.1) be reached at
# the container's address from other bridges, so B's noVNC answers A at
# B_IP:6080. What guards it is B's own VNC password: speak RFB from A the way
# native code there could (forging Host past noVNC's Origin guard) and require
# VNC authentication as the only security type.
rfb_security_types_from_a() {
  docker exec -i "${CONTAINER_NAME}" python3 -I - "$1" <<'PY'
import base64, os, socket, struct, sys

sock = socket.create_connection((sys.argv[1], 6080), timeout=5)
key = base64.b64encode(os.urandom(16)).decode()
sock.sendall(
    (
        "GET /websockify HTTP/1.1\r\nHost: 127.0.0.1:6080\r\nOrigin: http://127.0.0.1:6080\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Protocol: binary\r\n\r\n"
    ).encode()
)
raw = b""
while b"\r\n\r\n" not in raw:
    chunk = sock.recv(4096)
    if not chunk:
        sys.exit("noVNC closed the connection during the upgrade")
    raw += chunk
head, _, raw = raw.partition(b"\r\n\r\n")
status = head.split(b"\r\n", 1)[0].decode("latin-1")
if " 101 " not in status + " ":
    print(f"upgrade refused: {status}")
    sys.exit(0)
data = b""


def take(size):
    global raw, data
    while len(data) < size:
        while len(raw) < 2:
            raw += sock.recv(65536)
        length = raw[1] & 0x7F
        start = 2 + {126: 2, 127: 8}.get(length, 0)
        while len(raw) < start:
            raw += sock.recv(65536)
        if length == 126:
            length = struct.unpack(">H", raw[2:4])[0]
        elif length == 127:
            length = struct.unpack(">Q", raw[2:10])[0]
        while len(raw) < start + length:
            raw += sock.recv(65536)
        data += raw[start : start + length]
        raw = raw[start + length :]
    out, data = data[:size], data[size:]
    return out


version = take(12)
mask = os.urandom(4)
reply = b"RFB 003.008\n"
sock.sendall(bytes([0x82, 0x80 | len(reply)]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(reply)))
types = list(take(take(1)[0]))
print(f"{version.decode().strip()} security types {types}")
PY
}
B_RFB="$(rfb_security_types_from_a "${B_IP}" 2>&1 || true)"
echo "A -> B noVNC at ${B_IP}:6080 with a forged Host: ${B_RFB}"
case "${B_RFB}" in
  "upgrade refused"*|*"timed out"*|*"Connection refused"*) echo "probe ok: A cannot open B's noVNC" ;;
  *"security types [2]") echo "probe ok: B's noVNC offers only VNC authentication (its own password)" ;;
  *)
    echo >&2 "cross-session probe FAILED: B's noVNC does not require VNC authentication from A: ${B_RFB}"
    exit 1
    ;;
esac
# The shared browser-node's noVNC is reachable the same way; only the
# operator's VNC_PASSWORD guards it, and no isolated container holds that.
BROWSER_NODE_RFB="$(rfb_security_types_from_a "${BROWSER_NODE_IP}" 2>&1 || true)"
echo "A -> browser-node noVNC at ${BROWSER_NODE_IP}:6080 with a forged Host: ${BROWSER_NODE_RFB}"
if [[ -n "${VNC_PASSWORD:-}" ]]; then
  case "${BROWSER_NODE_RFB}" in
    *"security types [2]"|"upgrade refused"*|*"timed out"*|*"Connection refused"*) echo "probe ok: browser-node needs VNC_PASSWORD" ;;
    *)
      echo >&2 "browser-node's noVNC does not require VNC authentication from A: ${BROWSER_NODE_RFB}"
      exit 1
      ;;
  esac
elif [[ "${BROWSER_NODE_RFB}" == *"security types"* && "${BROWSER_NODE_RFB}" != *"security types [2]" ]]; then
  echo "RESIDUAL: VNC_PASSWORD is unset, so code in a session container can take over the shared browser-node's desktop"
fi
must_fail_from_a "browser-node by name" getent hosts browser-node
# No -f: any HTTP answer from the Playwright server would be a reach.
must_fail_from_a "browser-node:9223 at ${BROWSER_NODE_IP}" curl -sS --max-time 3 -o /dev/null "http://${BROWSER_NODE_IP}:9223/"
# The controller is on A's network (it joined to reach A's browser), but it refuses
# every request from a session network with 403, before the Host check and auth.
for path in /healthz /readyz /sessions /mcp; do
  CONTROLLER_STATUS="$(docker exec "${CONTAINER_NAME}" curl -sS --max-time 3 -o /dev/null -w '%{http_code}' "http://${CONTROLLER_IP_ON_A}:8000${path}" || true)"
  if [[ "${CONTROLLER_STATUS}" != "403" ]]; then
    echo >&2 "expected 403 from the controller at ${CONTROLLER_IP_ON_A}:8000${path} for session A, got '${CONTROLLER_STATUS}'"
    exit 1
  fi
done
echo "probe ok: the controller refuses session A at ${CONTROLLER_IP_ON_A}:8000 with 403 (/healthz /readyz /sessions /mcp)"
# The overlay's sysctl is set when the controller starts; it has since joined A's
# and B's networks. It must still not forward between them.
CONTROLLER_FORWARD="$(docker exec "${CONTROLLER_ID}" cat /proc/sys/net/ipv4/ip_forward)"
if [[ "${CONTROLLER_FORWARD}" != "0" ]]; then
  echo >&2 "expected ip_forward=0 in the controller after joining the session networks, got '${CONTROLLER_FORWARD}'"
  exit 1
fi
echo "probe ok: the controller does not forward between session networks (ip_forward=0)"

bearer_curl -fsS "${API_BASE_URL}/sessions/${B_SESSION_ID}" -X DELETE >/dev/null
wait_for "session B network removal" "! docker network inspect '${B_NETWORK}'" 30 1

CLOSE_JSON="$(bearer_curl -fsS "${API_BASE_URL}/sessions/${SESSION_ID}" -X DELETE)"
python3 -I - <<'PY' "${CLOSE_JSON}"
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

# Its profile (cookies) and downloads belong to the browser user, which the controller is not: the controller has
# them emptied as that user before it removes the session dir. Nothing may be left, for A or for B.
for closed in "${SESSION_ID}" "${B_SESSION_ID}"; do
  if [[ -e "${ROOT_DIR}/data/browser-sessions/${closed}" ]]; then
    echo >&2 "data/browser-sessions/${closed} is still on disk after close:"
    ls -laR "${ROOT_DIR}/data/browser-sessions/${closed}" >&2 || true
    exit 1
  fi
done
echo "session dirs removed after close (profile and downloads included)"

echo "isolated session smoke test passed"
