#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

SSH_DIR="${ROOT_DIR}/data/ssh"
TUNNEL_DIR="${ROOT_DIR}/data/tunnels"
SMOKE_KEY="${SSH_DIR}/smoke_id_ed25519"
SMOKE_PUB="${SMOKE_KEY}.pub"
SMOKE_AUTHORIZED_KEYS="${SSH_DIR}/smoke_authorized_keys"
SMOKE_KNOWN_HOSTS="${SSH_DIR}/smoke_known_hosts"
REMOTE_INFO="${TUNNEL_DIR}/reverse-ssh.json"
API_PORT_VALUE="${API_PORT:-8000}"
API_BASE_URL="http://127.0.0.1:${API_PORT_VALUE}"

export ISOLATED_TUNNEL_ENABLED=true
export ISOLATED_TUNNEL_HOST=test-bastion
export ISOLATED_TUNNEL_PORT=2222
export ISOLATED_TUNNEL_USER=tunnel
export ISOLATED_TUNNEL_KEY_PATH=/data/ssh/smoke_id_ed25519
export ISOLATED_TUNNEL_KNOWN_HOSTS_PATH=/data/ssh/smoke_known_hosts
export ISOLATED_TUNNEL_REMOTE_PORT_START=16181
export ISOLATED_TUNNEL_REMOTE_PORT_END=16189
export ISOLATED_TUNNEL_INFO_INTERVAL_SECONDS=5
export ISOLATED_TUNNEL_STARTUP_GRACE_SECONDS=1
export ISOLATED_TUNNEL_ACCESS_MODE=private
export ISOLATED_TUNNEL_PUBLIC_HOST=test-bastion
export ISOLATED_TUNNEL_PUBLIC_SCHEME=http
export ISOLATED_TAKEOVER_HOST=127.0.0.1

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.isolation.yml -f docker-compose.smoke.yml)

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

mkdir -p "${SSH_DIR}" "${TUNNEL_DIR}/sessions"
rm -f \
  "${SMOKE_KEY}" \
  "${SMOKE_PUB}" \
  "${SMOKE_AUTHORIZED_KEYS}" \
  "${SMOKE_KNOWN_HOSTS}" \
  "${REMOTE_INFO}" \
  "${TUNNEL_DIR}/sessions"/*.json \
  "${TUNNEL_DIR}/sessions"/*.log \
  >/dev/null 2>&1 || true

ssh-keygen -q -t ed25519 -N "" -f "${SMOKE_KEY}"
cp "${SMOKE_PUB}" "${SMOKE_AUTHORIZED_KEYS}"
chmod 600 "${SMOKE_KEY}" "${SMOKE_AUTHORIZED_KEYS}"
chmod 644 "${SMOKE_PUB}"

"${COMPOSE[@]}" up -d --build test-bastion
wait_for \
  "test bastion sshd" \
  "${COMPOSE[*]} exec -T test-bastion sh -lc 'nc -z 127.0.0.1 2222'" \
  40 \
  1

HOST_KEY="$("${COMPOSE[@]}" exec -T test-bastion sh -lc 'cat /etc/ssh/ssh_host_ed25519_key.pub')"
{
  printf '[test-bastion]:2222 %s\n' "${HOST_KEY}"
  printf 'test-bastion %s\n' "${HOST_KEY}"
} > "${SMOKE_KNOWN_HOSTS}"
chmod 644 "${SMOKE_KNOWN_HOSTS}"

"${COMPOSE[@]}" build browser-node controller
"${COMPOSE[@]}" up -d --no-recreate browser-node controller
wait_for "controller readiness" "curl -fsS ${API_BASE_URL}/readyz" 120 2

SESSION_JSON="$(curl -fsS "${API_BASE_URL}/sessions" -X POST -H 'content-type: application/json' -d '{"name":"isolated-tunnel-smoke","start_url":"https://example.com"}')"
read -r SESSION_ID CONTAINER_NAME REMOTE_PORT REMOTE_URL <<<"$(python3 - <<'PY' "${SESSION_JSON}"
import json
import sys

payload = json.loads(sys.argv[1])
assert payload["isolation"]["mode"] == "docker_ephemeral", payload
assert payload["remote_access"]["status"] == "active", payload
assert payload["remote_access"]["session_tunnel"]["active"] is True, payload
print(
    payload["id"],
    payload["isolation"]["runtime"]["container_name"],
    payload["remote_access"]["session_tunnel"]["remote_port"],
    payload["remote_access"]["takeover_url"],
)
PY
)"

wait_for \
  "isolated browser container" \
  "docker ps --format '{{.Names}}' | grep -Fx '${CONTAINER_NAME}'" \
  30 \
  1

wait_for \
  "reverse tunnel on bastion" \
  "${COMPOSE[*]} exec -T test-bastion sh -lc 'curl -fsS http://127.0.0.1:${REMOTE_PORT}/vnc.html >/dev/null'" \
  60 \
  2

OBSERVE_JSON="$(curl -fsS "${API_BASE_URL}/sessions/${SESSION_ID}/observe")"
python3 - <<'PY' "${OBSERVE_JSON}" "${REMOTE_URL}"
import json
import sys

payload = json.loads(sys.argv[1])
remote_url = sys.argv[2]
assert payload["remote_access"]["status"] == "active", payload
assert payload["remote_access"]["session_tunnel"]["active"] is True, payload
assert payload["takeover_url"] == remote_url, payload
print("isolated tunnel observe ok")
PY

REMOTE_ACCESS_JSON="$(curl -fsS "${API_BASE_URL}/remote-access?session_id=${SESSION_ID}")"
python3 - <<'PY' "${REMOTE_ACCESS_JSON}" "${REMOTE_URL}"
import json
import sys

payload = json.loads(sys.argv[1])
remote_url = sys.argv[2]
assert payload["status"] == "active", payload
assert payload["session_tunnel"]["active"] is True, payload
assert payload["takeover_url"] == remote_url, payload
print("isolated tunnel remote-access endpoint ok")
PY

CLOSE_JSON="$(curl -fsS "${API_BASE_URL}/sessions/${SESSION_ID}" -X DELETE)"
python3 - <<'PY' "${CLOSE_JSON}"
import json
import sys

payload = json.loads(sys.argv[1])
assert payload["closed"] is True, payload
assert payload["session"]["status"] == "closed", payload
assert payload["session"]["remote_access"]["session_tunnel"]["status"] == "inactive", payload
print("isolated tunnel close ok")
PY

wait_for \
  "isolated browser container removal" \
  "! docker ps -a --format '{{.Names}}' | grep -Fx '${CONTAINER_NAME}'" \
  30 \
  1

wait_for \
  "reverse tunnel teardown" \
  "! ${COMPOSE[*]} exec -T test-bastion sh -lc 'curl -fsS http://127.0.0.1:${REMOTE_PORT}/vnc.html >/dev/null'" \
  30 \
  1

echo "isolated session tunnel smoke test passed"
