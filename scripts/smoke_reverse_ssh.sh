#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.smoke.yml --profile reverse-ssh)
API_PORT_VALUE="${API_PORT:-8000}"
API_BASE_URL="http://127.0.0.1:${API_PORT_VALUE}"
SSH_DIR="${ROOT_DIR}/data/ssh"
TUNNEL_DIR="${ROOT_DIR}/data/tunnels"
SMOKE_KEY="${SSH_DIR}/smoke_id_ed25519"
SMOKE_PUB="${SMOKE_KEY}.pub"
SMOKE_AUTHORIZED_KEYS="${SSH_DIR}/smoke_authorized_keys"
SMOKE_KNOWN_HOSTS="${SSH_DIR}/smoke_known_hosts"
REMOTE_INFO="${TUNNEL_DIR}/reverse-ssh.json"

cleanup() {
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

mkdir -p "${SSH_DIR}" "${TUNNEL_DIR}"
rm -f "${SMOKE_KEY}" "${SMOKE_PUB}" "${SMOKE_AUTHORIZED_KEYS}" "${SMOKE_KNOWN_HOSTS}" "${REMOTE_INFO}"

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

"${COMPOSE[@]}" build browser-node controller reverse-ssh
"${COMPOSE[@]}" up -d --no-recreate browser-node controller reverse-ssh

wait_for "controller readiness" "curl -fsS ${API_BASE_URL}/readyz" 120 2
wait_for "remote access metadata" "[ -f '${REMOTE_INFO}' ]" 60 1
wait_for \
  "forwarded API through bastion" \
  "${COMPOSE[*]} exec -T test-bastion sh -lc 'curl -fsS http://127.0.0.1:18000/readyz'" \
  60 \
  2
wait_for \
  "forwarded noVNC through bastion" \
  "${COMPOSE[*]} exec -T test-bastion sh -lc 'curl -fsS http://127.0.0.1:16080/vnc.html'" \
  60 \
  2

REMOTE_ACCESS_JSON="$(curl -fsS "${API_BASE_URL}/remote-access")"
python3 - <<'PY' "${REMOTE_ACCESS_JSON}"
import json
import sys

payload = json.loads(sys.argv[1])
assert payload["active"] is True, payload
assert payload["status"] == "active", payload
assert payload["takeover_url"] == "http://test-bastion:16080/vnc.html?autoconnect=true&resize=scale", payload
assert payload["api_url"] == "http://test-bastion:18000", payload
print("remote-access endpoint ok")
PY

SESSION_JSON="$("${COMPOSE[@]}" exec -T test-bastion sh -lc "curl -fsS http://127.0.0.1:18000/sessions -X POST -H 'content-type: application/json' -d '{\"name\":\"smoke\",\"start_url\":\"https://example.com\"}'")"
SESSION_ID="$(python3 - <<'PY' "${SESSION_JSON}"
import json
import sys
print(json.loads(sys.argv[1])["id"])
PY
)"

OBSERVE_JSON="$("${COMPOSE[@]}" exec -T test-bastion sh -lc "curl -fsS http://127.0.0.1:18000/sessions/${SESSION_ID}/observe")"
python3 - <<'PY' "${OBSERVE_JSON}"
import json
import sys

payload = json.loads(sys.argv[1])
assert payload["session"]["remote_access"]["active"] is True, payload
assert payload["remote_access"]["status"] == "active", payload
assert payload["takeover_url"] == "http://test-bastion:16080/vnc.html?autoconnect=true&resize=scale", payload
assert payload["url"] == "https://example.com/", payload
print("observe through forwarded API ok")
PY

echo "reverse SSH smoke test passed"
