#!/usr/bin/env bash
set -euo pipefail

export DISPLAY=:99
WIDTH="${BROWSER_WIDTH:-1600}"
HEIGHT="${BROWSER_HEIGHT:-900}"
WS_ENDPOINT_FILE="${BROWSER_WS_ENDPOINT_FILE:-/data/profile/browser-ws-endpoint.txt}"
PLAYWRIGHT_SERVER_PORT="${PLAYWRIGHT_SERVER_PORT:-9223}"
PLAYWRIGHT_SERVER_HOST="${PLAYWRIGHT_SERVER_HOST:-0.0.0.0}"
PLAYWRIGHT_SERVER_ADVERTISED_HOST="${PLAYWRIGHT_SERVER_ADVERTISED_HOST:-browser-node}"
export BROWSER_WIDTH="$WIDTH" \
  BROWSER_HEIGHT="$HEIGHT" \
  BROWSER_WS_ENDPOINT_FILE="$WS_ENDPOINT_FILE" \
  PLAYWRIGHT_SERVER_PORT \
  PLAYWRIGHT_SERVER_HOST \
  PLAYWRIGHT_SERVER_ADVERTISED_HOST

mkdir -p /data/profile /data/downloads /tmp/runtime
rm -f "$WS_ENDPOINT_FILE"
DISPLAY_NUM="${DISPLAY#:}"
rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}"

Xvfb "$DISPLAY" -screen 0 "${WIDTH}x${HEIGHT}x24" -ac +extension RANDR >/tmp/xvfb.log 2>&1 &
fluxbox >/tmp/fluxbox.log 2>&1 &
x11vnc -display "$DISPLAY" -forever -shared -rfbport 5900 -nopw -xkb >/tmp/x11vnc.log 2>&1 &
/usr/share/novnc/utils/novnc_proxy --vnc localhost:5900 --listen 6080 >/tmp/novnc.log 2>&1 &

cleanup() {
  if [[ -n "${PLAYWRIGHT_SERVER_PID:-}" ]] && kill -0 "$PLAYWRIGHT_SERVER_PID" >/dev/null 2>&1; then
    kill "$PLAYWRIGHT_SERVER_PID" >/dev/null 2>&1 || true
    wait "$PLAYWRIGHT_SERVER_PID" >/dev/null 2>&1 || true
  fi
}

trap cleanup EXIT INT TERM

node /opt/browser-node/server.mjs >/tmp/playwright-server.log 2>&1 &
PLAYWRIGHT_SERVER_PID=$!

wait "$PLAYWRIGHT_SERVER_PID"
