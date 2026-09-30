#!/usr/bin/env bash
set -euo pipefail

export DISPLAY=:99
WIDTH="${BROWSER_WIDTH:-1600}"
HEIGHT="${BROWSER_HEIGHT:-800}"
# Chromium's page viewport is WIDTH x HEIGHT, but the headed browser also needs
# room for its tabs, address bar, and the Fluxbox window frame.  Giving Xvfb the
# exact viewport height clips part of that real window in the VNC framebuffer.
# Keep the agent-facing viewport unchanged and add display-only headroom.
DISPLAY_WIDTH="${BROWSER_DISPLAY_WIDTH:-$WIDTH}"
DISPLAY_HEIGHT="${BROWSER_DISPLAY_HEIGHT:-$((HEIGHT + 100))}"
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

BROWSER_USER="${BROWSER_USER:-browser}"

# The endpoint file's own directory is created and chowned too: a fresh data
# volume has only a root-owned /data, so the unprivileged browser user could
# not create it (EACCES, crash loop on every new tenant).
WS_ENDPOINT_DIR="$(dirname "$WS_ENDPOINT_FILE")"
case "$WS_ENDPOINT_DIR" in
  /data/?*) ;;
  *) echo "BROWSER_WS_ENDPOINT_FILE must live in a subdirectory of /data: $WS_ENDPOINT_FILE" >&2; exit 1 ;;
esac
mkdir -p /data/profile /data/downloads /data/browser-profiles "$WS_ENDPOINT_DIR" /tmp/runtime
rm -f "$WS_ENDPOINT_FILE"

# Crash recovery for persistent profiles (stale Chromium SingletonLock etc.
# left by a killed container) is done by server.mjs, not here: only after it
# holds the volume's node lease, so a lock that belongs to another live
# browser-node sharing this volume is never removed.
DISPLAY_NUM="${DISPLAY#:}"
rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}"

# Everything below runs as $BROWSER_USER; root is used only for this setup.
# Data directories are bind mounts that earlier releases filled as root, so
# ownership is fixed on every start. Under rootless Docker or userns-remap the
# chown can be refused — say so and carry on rather than refusing to start.
run_as_browser() {
  if [[ "$(id -u)" -eq 0 ]] && id "$BROWSER_USER" >/dev/null 2>&1; then
    HOME="$(getent passwd "$BROWSER_USER" | cut -d: -f6)" \
      setpriv --reuid="$(id -u "$BROWSER_USER")" --regid="$(id -g "$BROWSER_USER")" --init-groups "$@"
  else
    "$@"
  fi
}
if [[ "$(id -u)" -eq 0 ]] && id "$BROWSER_USER" >/dev/null 2>&1; then
  chown -R "$BROWSER_USER:$BROWSER_USER" /data/profile /data/downloads /data/browser-profiles "$WS_ENDPOINT_DIR" /tmp/runtime \
    || echo "warning: could not chown browser data directories; continuing" >&2
  mkdir -p /tmp/.X11-unix
  chmod 1777 /tmp/.X11-unix
fi

# x11vnc serves the raw display, and noVNC fronts it. Without a password either
# port is full control of a browser holding stored logins, which is only safe
# while both stay bound to loopback. VNC_PASSWORD adds VNC authentication for
# deployments that publish them more widely (noVNC prompts for it). The VNC
# protocol uses only the first 8 characters. It goes through a 0600 file, not
# argv, and is unset so Chromium and the other processes never inherit it.
VNC_AUTH_ARGS=(-nopw)
if [[ -n "${VNC_PASSWORD:-}" ]]; then
  VNC_PASSWORD_FILE=/tmp/runtime/vnc-password
  (umask 077 && printf '%s\n' "$VNC_PASSWORD" >"$VNC_PASSWORD_FILE")
  if [[ "$(id -u)" -eq 0 ]] && id "$BROWSER_USER" >/dev/null 2>&1; then
    chown "$BROWSER_USER:$BROWSER_USER" "$VNC_PASSWORD_FILE"
  fi
  VNC_AUTH_ARGS=(-passwdfile "$VNC_PASSWORD_FILE")
fi
unset VNC_PASSWORD

run_as_browser Xvfb "$DISPLAY" -screen 0 "${DISPLAY_WIDTH}x${DISPLAY_HEIGHT}x24" -ac +extension RANDR >/tmp/xvfb.log 2>&1 &
run_as_browser fluxbox >/tmp/fluxbox.log 2>&1 &
# -add_keysyms teaches the X keymap Arabic and other non-Latin keysyms while
# preserving the VNC authentication and non-root process boundary.
start_x11vnc() {
  # x11vnc is deliberately separate from Chromium.  If it dies, agents can
  # still drive the browser through Playwright while the owner viewer waits
  # forever for a framebuffer.  Keep that recovery local to the viewer: do
  # not restart Chromium, the browser-node, or the active owner session.
  run_as_browser x11vnc -display "$DISPLAY" -forever -shared -rfbport 5900 "${VNC_AUTH_ARGS[@]}" -xkb -add_keysyms >>/tmp/x11vnc.log 2>&1 &
  X11VNC_PID=$!
}

start_x11vnc
run_as_browser /usr/share/novnc/utils/novnc_proxy --vnc localhost:5900 --listen 6080 >/tmp/novnc.log 2>&1 &

cleanup() {
  if [[ -n "${X11VNC_WATCHDOG_PID:-}" ]] && kill -0 "$X11VNC_WATCHDOG_PID" >/dev/null 2>&1; then
    kill "$X11VNC_WATCHDOG_PID" >/dev/null 2>&1 || true
    wait "$X11VNC_WATCHDOG_PID" >/dev/null 2>&1 || true
  fi
  if [[ -n "${PLAYWRIGHT_SERVER_PID:-}" ]] && kill -0 "$PLAYWRIGHT_SERVER_PID" >/dev/null 2>&1; then
    kill "$PLAYWRIGHT_SERVER_PID" >/dev/null 2>&1 || true
    wait "$PLAYWRIGHT_SERVER_PID" >/dev/null 2>&1 || true
  fi
}

trap cleanup EXIT INT TERM

run_as_browser node /opt/browser-node/server.mjs >/tmp/playwright-server.log 2>&1 &
PLAYWRIGHT_SERVER_PID=$!

# Do not make a transient VNC failure invisible behind a healthy Playwright
# process.  The viewer self-heals within two seconds and an agent's active
# browser work remains untouched.
(
  while kill -0 "$PLAYWRIGHT_SERVER_PID" >/dev/null 2>&1; do
    if ! kill -0 "$X11VNC_PID" >/dev/null 2>&1; then
      echo "x11vnc exited; restarting the display relay" >&2
      start_x11vnc
    fi
    sleep 2
  done
) &
X11VNC_WATCHDOG_PID=$!

wait "$PLAYWRIGHT_SERVER_PID"
