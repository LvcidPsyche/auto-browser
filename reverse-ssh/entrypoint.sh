#!/bin/sh
set -eu

require_env() {
  name="$1"
  eval "value=\${$name:-}"
  if [ -z "$value" ]; then
    echo >&2 "missing required environment variable: $name"
    exit 1
  fi
}

require_env REVERSE_SSH_HOST
require_env REVERSE_SSH_USER
require_env REVERSE_SSH_KEY_PATH

: "${REVERSE_SSH_PORT:=22}"
: "${REVERSE_SSH_REMOTE_BIND_ADDRESS:=127.0.0.1}"
: "${REVERSE_SSH_REMOTE_API_PORT:=18000}"
: "${REVERSE_SSH_REMOTE_NOVNC_PORT:=16080}"
: "${REVERSE_SSH_LOCAL_API_HOST:=controller}"
: "${REVERSE_SSH_LOCAL_API_PORT:=8000}"
: "${REVERSE_SSH_LOCAL_NOVNC_HOST:=browser-node}"
: "${REVERSE_SSH_LOCAL_NOVNC_PORT:=6080}"
: "${REVERSE_SSH_SERVER_ALIVE_INTERVAL:=30}"
: "${REVERSE_SSH_SERVER_ALIVE_COUNT_MAX:=3}"
: "${REVERSE_SSH_STRICT_HOST_KEY_CHECKING:=yes}"
: "${REVERSE_SSH_KNOWN_HOSTS_PATH:=/ssh/known_hosts}"
: "${REVERSE_SSH_INFO_PATH:=/data/tunnels/reverse-ssh.json}"
: "${REVERSE_SSH_INFO_INTERVAL_SECONDS:=15}"
: "${REVERSE_SSH_STALE_AFTER_SECONDS:=}"
: "${REVERSE_SSH_ALLOW_NONLOCAL_BIND:=false}"
: "${REVERSE_SSH_ACCESS_MODE:=private}"
: "${REVERSE_SSH_PUBLIC_HOST:=${REVERSE_SSH_HOST}}"
: "${REVERSE_SSH_PUBLIC_SCHEME:=http}"
: "${REVERSE_SSH_PUBLIC_API_URL:=${REVERSE_SSH_PUBLIC_SCHEME}://${REVERSE_SSH_PUBLIC_HOST}:${REVERSE_SSH_REMOTE_API_PORT}}"
: "${REVERSE_SSH_PUBLIC_TAKEOVER_URL:=${REVERSE_SSH_PUBLIC_SCHEME}://${REVERSE_SSH_PUBLIC_HOST}:${REVERSE_SSH_REMOTE_NOVNC_PORT}/vnc.html?autoconnect=true&resize=scale}"

# The numeric fields below are written into reverse-ssh.json unquoted.
require_int() {
  eval "value=\${$1}"
  case "$value" in
    ''|*[!0-9]*)
      echo >&2 "$1 must be a non-negative integer, got: $value"
      exit 1
      ;;
  esac
}

require_int REVERSE_SSH_INFO_INTERVAL_SECONDS

if [ -z "${REVERSE_SSH_STALE_AFTER_SECONDS}" ]; then
  REVERSE_SSH_STALE_AFTER_SECONDS=$((REVERSE_SSH_INFO_INTERVAL_SECONDS * 3))
fi

require_int REVERSE_SSH_STALE_AFTER_SECONDS
require_int REVERSE_SSH_PORT
require_int REVERSE_SSH_REMOTE_API_PORT
require_int REVERSE_SSH_REMOTE_NOVNC_PORT

if [ ! -r "${REVERSE_SSH_KEY_PATH}" ]; then
  echo >&2 "reverse SSH key is missing or unreadable: ${REVERSE_SSH_KEY_PATH}"
  exit 1
fi

case "${REVERSE_SSH_ACCESS_MODE}" in
  private|tailscale|cloudflare-access|unsafe-public)
    ;;
  *)
    echo >&2 "invalid REVERSE_SSH_ACCESS_MODE=${REVERSE_SSH_ACCESS_MODE}; expected one of: private, tailscale, cloudflare-access, unsafe-public"
    exit 1
    ;;
esac

case "${REVERSE_SSH_REMOTE_BIND_ADDRESS}" in
  127.0.0.1|localhost|::1)
    ;;
  *)
    if [ "${REVERSE_SSH_ALLOW_NONLOCAL_BIND}" != "true" ]; then
      echo >&2 "refusing non-local reverse bind address ${REVERSE_SSH_REMOTE_BIND_ADDRESS}; set REVERSE_SSH_ALLOW_NONLOCAL_BIND=true only if you mean to expose the forwarded ports more broadly"
      exit 1
    fi
    if [ "${REVERSE_SSH_ACCESS_MODE}" != "unsafe-public" ]; then
      echo >&2 "refusing non-local reverse bind address ${REVERSE_SSH_REMOTE_BIND_ADDRESS} while REVERSE_SSH_ACCESS_MODE=${REVERSE_SSH_ACCESS_MODE}; keep the bind local and front it with a bastion, Tailscale, or Cloudflare Access. Use REVERSE_SSH_ACCESS_MODE=unsafe-public only for deliberate wide exposure."
      exit 1
    fi
    ;;
esac

if [ "${REVERSE_SSH_ACCESS_MODE}" = "cloudflare-access" ] && [ "${REVERSE_SSH_PUBLIC_SCHEME}" != "https" ]; then
  echo >&2 "Cloudflare Access mode expects REVERSE_SSH_PUBLIC_SCHEME=https"
  exit 1
fi

case "${REVERSE_SSH_STRICT_HOST_KEY_CHECKING}" in
  yes|accept-new)
    if [ ! -f "${REVERSE_SSH_KNOWN_HOSTS_PATH}" ]; then
      echo >&2 "known_hosts file is required when strict host key checking is enabled: ${REVERSE_SSH_KNOWN_HOSTS_PATH}"
      exit 1
    fi
    ;;
esac

mkdir -p "$(dirname "${REVERSE_SSH_INFO_PATH}")"

now_iso8601() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

# Escape a value for use inside a JSON string: control characters are dropped
# (none is valid in a host or URL), then backslash and double quote are escaped.
json_str() {
  printf '%s' "$1" | tr -d '\000-\037' | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

# Written to a temp file beside the target, then renamed over it: the controller
# polls this file and must never read a half-written one. mktemp creates the file
# 0600; the controller may run as another user, so restore the mode a plain
# redirect gave.
write_metadata() {
  state="$1"
  tmp="$(mktemp "${REVERSE_SSH_INFO_PATH}.XXXXXX")"
  cat >"${tmp}" <<EOF
{
  "status": "$(json_str "${state}")",
  "updated_at": "$(now_iso8601)",
  "info_interval_seconds": ${REVERSE_SSH_INFO_INTERVAL_SECONDS},
  "stale_after_seconds": ${REVERSE_SSH_STALE_AFTER_SECONDS},
  "ssh_host": "$(json_str "${REVERSE_SSH_HOST}")",
  "ssh_port": ${REVERSE_SSH_PORT},
  "ssh_user": "$(json_str "${REVERSE_SSH_USER}")",
  "access_mode": "$(json_str "${REVERSE_SSH_ACCESS_MODE}")",
  "remote_bind_address": "$(json_str "${REVERSE_SSH_REMOTE_BIND_ADDRESS}")",
  "remote_api_port": ${REVERSE_SSH_REMOTE_API_PORT},
  "remote_novnc_port": ${REVERSE_SSH_REMOTE_NOVNC_PORT},
  "public_api_url": "$(json_str "${REVERSE_SSH_PUBLIC_API_URL}")",
  "public_takeover_url": "$(json_str "${REVERSE_SSH_PUBLIC_TAKEOVER_URL}")"
}
EOF
  chmod 644 "${tmp}"
  mv "${tmp}" "${REVERSE_SSH_INFO_PATH}"
}

heartbeat() {
  while kill -0 "${AUTOSSH_PID}" >/dev/null 2>&1; do
    if ps -o pid=,ppid=,comm= | awk -v autossh_pid="${AUTOSSH_PID}" '
      $2 == autossh_pid && $3 == "ssh" { found = 1 }
      END { exit found ? 0 : 1 }
    '; then
      write_metadata "active"
    else
      write_metadata "degraded"
    fi
    sleep "${REVERSE_SSH_INFO_INTERVAL_SECONDS}"
  done
}

export AUTOSSH_GATETIME=0
write_metadata "starting"

autossh \
  -M 0 \
  -N \
  -T \
  -p "${REVERSE_SSH_PORT}" \
  -o "BatchMode=yes" \
  -o "ExitOnForwardFailure=yes" \
  -o "ServerAliveInterval=${REVERSE_SSH_SERVER_ALIVE_INTERVAL}" \
  -o "ServerAliveCountMax=${REVERSE_SSH_SERVER_ALIVE_COUNT_MAX}" \
  -o "StrictHostKeyChecking=${REVERSE_SSH_STRICT_HOST_KEY_CHECKING}" \
  -o "UserKnownHostsFile=${REVERSE_SSH_KNOWN_HOSTS_PATH}" \
  -i "${REVERSE_SSH_KEY_PATH}" \
  -R "${REVERSE_SSH_REMOTE_BIND_ADDRESS}:${REVERSE_SSH_REMOTE_API_PORT}:${REVERSE_SSH_LOCAL_API_HOST}:${REVERSE_SSH_LOCAL_API_PORT}" \
  -R "${REVERSE_SSH_REMOTE_BIND_ADDRESS}:${REVERSE_SSH_REMOTE_NOVNC_PORT}:${REVERSE_SSH_LOCAL_NOVNC_HOST}:${REVERSE_SSH_LOCAL_NOVNC_PORT}" \
  "${REVERSE_SSH_USER}@${REVERSE_SSH_HOST}" &

AUTOSSH_PID=$!
write_metadata "starting"
heartbeat &
HEARTBEAT_PID=$!

terminate() {
  if kill -0 "${AUTOSSH_PID}" >/dev/null 2>&1; then
    kill "${AUTOSSH_PID}" >/dev/null 2>&1 || true
  fi
}

trap terminate INT TERM

wait "${AUTOSSH_PID}"
AUTOSSH_STATUS=$?
kill "${HEARTBEAT_PID}" >/dev/null 2>&1 || true
wait "${HEARTBEAT_PID}" >/dev/null 2>&1 || true
write_metadata "inactive"
exit "${AUTOSSH_STATUS}"
