#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

source "$ROOT_DIR/scripts/load_env.sh"
load_repo_env "$ROOT_DIR/.env"

require_bin() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

explain_socket_access_requirement() {
  cat >&2 <<'EOF'
Local socket access is blocked in this environment.
`make doctor` probes localhost ports and connects to Docker-published services,
so it must run in a shell that can open local sockets.

If you're running from a sandboxed agent session, rerun it with elevated
permissions or from your normal terminal.
EOF
}

ensure_local_socket_access() {
  local status=0
  if python3 - <<'PY'
import errno
import socket

try:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM):
        pass
except PermissionError:
    raise SystemExit(2)
except OSError as exc:
    if exc.errno in {errno.EPERM, errno.EACCES}:
        raise SystemExit(2)
    raise SystemExit(1)
PY
  then
    return 0
  else
    status=$?
  fi

  if [[ "$status" == "2" ]]; then
    explain_socket_access_requirement
    exit 1
  fi

  echo "Failed to verify local socket access required by doctor." >&2
  exit 1
}

port_is_free() {
  local status=0
  if python3 - "$1" <<'PY'
import errno
import socket
import sys

port = int(sys.argv[1])
try:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        result = sock.connect_ex(("127.0.0.1", port))
except PermissionError:
    raise SystemExit(2)
except OSError as exc:
    if exc.errno in {errno.EPERM, errno.EACCES}:
        raise SystemExit(2)
    raise
raise SystemExit(0 if result != 0 else 1)
PY
  then
    return 0
  else
    status=$?
  fi

  if [[ "$status" == "2" ]]; then
    return 2
  fi
  return 1
}

pick_port() {
  local requested="$1"
  local reusable_current="${2:-}"
  local status=0
  shift 2
  if [[ -n "$reusable_current" && "$requested" == "$reusable_current" ]]; then
    echo "$requested"
    return 0
  fi
  if port_is_free "$requested"; then
    echo "$requested"
    return 0
  else
    status=$?
  fi
  if [[ "$status" == "2" ]]; then
    explain_socket_access_requirement
    exit 1
  fi
  for candidate in "$@"; do
    if port_is_free "$candidate"; then
      echo "$candidate"
      return 0
    else
      status=$?
    fi
    if [[ "$status" == "2" ]]; then
      explain_socket_access_requirement
      exit 1
    fi
  done
  echo "No free port found for requested port $requested" >&2
  exit 1
}

wait_for_http() {
  local url="$1"
  local attempts="${2:-90}"
  local delay="${3:-1}"
  local count=0
  until curl -fsS "$url" >/dev/null 2>&1; do
    count=$((count + 1))
    if (( count >= attempts )); then
      echo "Timed out waiting for $url" >&2
      return 1
    fi
    sleep "$delay"
  done
}

unix_http_healthcheck() {
  python3 - "$1" <<'PY'
import socket
import sys

socket_path = sys.argv[1]
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
    sock.settimeout(2)
    sock.connect(socket_path)
    sock.sendall(b"GET /healthz HTTP/1.1\r\nHost: local\r\nConnection: close\r\n\r\n")
    chunks = []
    while True:
      data = sock.recv(4096)
      if not data:
        break
      chunks.append(data)
payload = b"".join(chunks)
if b"200 OK" not in payload:
    sys.stderr.write(payload.decode("utf-8", "replace"))
    raise SystemExit(1)
PY
}

require_bin docker
require_bin curl
require_bin jq
require_bin python3
ensure_local_socket_access

REQUESTED_API_PORT="${API_PORT:-8000}"
REQUESTED_NOVNC_PORT="${NOVNC_PORT:-6080}"
REQUESTED_VNC_PORT="${VNC_PORT:-5900}"

current_controller_port=""
current_browser_novnc_port=""
current_browser_vnc_port=""
# docker compose ps -q exits 0 even when no containers exist; check for non-empty output
if docker compose ps -q controller 2>/dev/null | grep -q .; then
  current_controller_port="$(docker compose port controller 8000 2>/dev/null | awk -F: 'NF {print $NF}' | tail -n1 || true)"
fi
if docker compose ps -q browser-node 2>/dev/null | grep -q .; then
  current_browser_novnc_port="$(docker compose port browser-node 6080 2>/dev/null | awk -F: 'NF {print $NF}' | tail -n1 || true)"
  current_browser_vnc_port="$(docker compose port browser-node 5900 2>/dev/null | awk -F: 'NF {print $NF}' | tail -n1 || true)"
fi

API_PORT="$(pick_port "$REQUESTED_API_PORT" "$current_controller_port" 8010 8011 8012 18000)"
NOVNC_PORT="$(pick_port "$REQUESTED_NOVNC_PORT" "$current_browser_novnc_port" 6081 6082 16080)"
VNC_PORT="$(pick_port "$REQUESTED_VNC_PORT" "$current_browser_vnc_port" 5901 5902 15900)"

if [[ "$API_PORT" != "$REQUESTED_API_PORT" ]]; then
  echo "Using API_PORT=$API_PORT because $REQUESTED_API_PORT is busy."
fi
if [[ "$NOVNC_PORT" != "$REQUESTED_NOVNC_PORT" ]]; then
  echo "Using NOVNC_PORT=$NOVNC_PORT because $REQUESTED_NOVNC_PORT is busy."
fi
if [[ "$VNC_PORT" != "$REQUESTED_VNC_PORT" ]]; then
  echo "Using VNC_PORT=$VNC_PORT because $REQUESTED_VNC_PORT is busy."
fi

TAKEOVER_URL="${TAKEOVER_URL:-http://127.0.0.1:${NOVNC_PORT}/vnc.html?autoconnect=true&resize=scale}"
SMOKE_URL="${SMOKE_URL:-https://example.com}"
SMOKE_PROVIDER="${SMOKE_PROVIDER:-openai}"
SMOKE_GOAL="${SMOKE_GOAL:-Inspect the page. If it already says Example Domain, return done with a short reason.}"
OPENAI_AUTH_MODE="${OPENAI_AUTH_MODE:-api}"
OPENAI_HOST_BRIDGE_SOCKET="${OPENAI_HOST_BRIDGE_SOCKET:-/data/host-bridge/codex.sock}"

if [[ -n "$current_controller_port" ]]; then
  active_sessions_json="$(curl -fsS "http://127.0.0.1:${current_controller_port}/sessions" 2>/dev/null || echo '[]')"
  active_count="$(echo "$active_sessions_json" | jq '[.[] | select(.status == "active")] | length')"
  if [[ "$active_count" != "0" ]]; then
    echo "Refusing readiness smoke because ${active_count} active session(s) already exist on API port ${current_controller_port}." >&2
    echo "Close them first so doctor does not interrupt live work." >&2
    exit 1
  fi
fi

if [[ "$OPENAI_AUTH_MODE" == "host_bridge" ]]; then
  host_socket_path="$(resolve_repo_host_path "$ROOT_DIR" "$OPENAI_HOST_BRIDGE_SOCKET")"
  if [[ ! -S "$host_socket_path" ]]; then
    echo "Host bridge socket missing at $host_socket_path" >&2
    echo "Start the bridge first, for example:" >&2
    echo "  systemctl --user start codex-host-bridge.service" >&2
    exit 1
  fi
  unix_http_healthcheck "$host_socket_path"
fi

echo "Bringing up controller + browser-node..."
compose_flags=(-d)
if [[ "${DOCTOR_BUILD:-0}" == "1" ]]; then
  compose_flags+=(--build)
fi
API_PORT="$API_PORT" NOVNC_PORT="$NOVNC_PORT" VNC_PORT="$VNC_PORT" TAKEOVER_URL="$TAKEOVER_URL" \
  docker compose up "${compose_flags[@]}" browser-node controller >/dev/null

wait_for_http "http://127.0.0.1:${API_PORT}/readyz"

echo "Provider readiness:"
providers_json="$(curl -fsS "http://127.0.0.1:${API_PORT}/agent/providers")"
echo "$providers_json" | jq .

smoke_provider_ready="$(echo "$providers_json" | jq -r --arg provider "$SMOKE_PROVIDER" '.[] | select(.provider == $provider) | .configured')"

session_id=""
cleanup() {
  if [[ -n "$session_id" ]]; then
    curl -fsS -X DELETE "http://127.0.0.1:${API_PORT}/sessions/${session_id}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

session_payload="$(jq -nc --arg start_url "$SMOKE_URL" --arg name "doctor-smoke" '{start_url:$start_url,name:$name}')"
session_json="$(curl -fsS "http://127.0.0.1:${API_PORT}/sessions" \
  -X POST \
  -H 'Content-Type: application/json' \
  -d "$session_payload")"
session_id="$(echo "$session_json" | jq -r '.id')"

echo "Smoke session:"
echo "$session_json" | jq '{id,status,current_url,title,takeover_url}'

echo "Observe smoke:"
curl -fsS "http://127.0.0.1:${API_PORT}/sessions/${session_id}/observe" \
  | jq '{title,url,interactable_count:(.interactables|length),screenshot_url}'

if [[ "$smoke_provider_ready" == "true" ]]; then
  echo "Agent smoke via provider=${SMOKE_PROVIDER}:"
  step_payload="$(jq -nc --arg provider "$SMOKE_PROVIDER" --arg goal "$SMOKE_GOAL" '{provider:$provider,goal:$goal,observation_limit:25}')"
  step_response_file="$(mktemp)"
  step_status=""
  for attempt in 1 2 3; do
    step_status="$(
      curl -sS \
        -o "$step_response_file" \
        -w '%{http_code}' \
        "http://127.0.0.1:${API_PORT}/sessions/${session_id}/agent/step" \
        -X POST \
        -H 'Content-Type: application/json' \
        -d "$step_payload"
    )"
    cat "$step_response_file" | jq '{provider,model,status,decision,error,error_code,usage,detail}'
    if [[ "${step_status}" -lt 400 ]]; then
      break
    fi
    if [[ "${step_status}" -lt 500 || "${attempt}" -eq 3 ]]; then
      echo "Agent smoke failed with HTTP ${step_status}." >&2
      rm -f "$step_response_file"
      exit 1
    fi
    echo "Agent smoke hit transient HTTP ${step_status}; retrying (${attempt}/3)..." >&2
    sleep 2
  done
  rm -f "$step_response_file"
else
  echo "Skipping agent smoke because provider=${SMOKE_PROVIDER} is not configured."
fi

echo
echo "Ready:"
echo "  API docs:     http://127.0.0.1:${API_PORT}/docs"
echo "  noVNC:        ${TAKEOVER_URL}"
echo "  Health:       http://127.0.0.1:${API_PORT}/healthz"
echo "  Ready:        http://127.0.0.1:${API_PORT}/readyz"
