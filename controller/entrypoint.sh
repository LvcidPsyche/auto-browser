#!/usr/bin/env bash
# The controller never stays root unless CONTROLLER_RUN_AS=root (not recommended).
#
# As root this only (1) asks app/run_as.py which user to run as (the owner of /data when that is neither root nor
# the browser's uid 10001, so a host user keeps its files and its 0600 data/api-token; otherwise the image's controller
# user, uid 10002) and lets it hand that user what earlier releases left in the controller's own /data subtrees;
# (2) makes sure that uid and gid have passwd/group entries (ssh refuses a uid without one); (3) adds the group of a
# mounted Docker socket, for docker_ephemeral. Then it execs the command as that user through setpriv (util-linux,
# already in the base image) with no capabilities in any set and no_new_privs.
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  # Started as a non-root user already (docker run --user, a Kubernetes runAsUser): nothing to drop.
  export CONTROLLER_RUN_AS_REASON="${CONTROLLER_RUN_AS_REASON:-started as uid $(id -u) by the container runtime}"
  exec "$@"
fi

fail() {
  echo "entrypoint: ERROR: $*" >&2
  exit 70
}

# Switching users needs CAP_SETUID (bit 7) and CAP_SETGID (bit 6). A container started with cap_drop ALL has
# neither: it still starts, as a uid 0 that holds no capabilities, with no_new_privs, and readiness warns.
cap_eff="$(awk '/^CapEff:/ {print $2}' /proc/self/status)"
if (( (16#$cap_eff & ((1 << 7) | (1 << 6))) != ((1 << 7) | (1 << 6)) )); then
  export CONTROLLER_RUN_AS_REASON="no CAP_SETUID/CAP_SETGID (cap_drop ALL?), so uid 0 with no capabilities"
  echo "entrypoint: WARNING: this container cannot switch users (no CAP_SETUID/CAP_SETGID): running as uid 0 with" \
    "no capabilities and no_new_privs. To run as the controller user instead, start it as that user: compose" \
    "user: \"10002:10002\" (or docker run --user 10002:10002), with ./data owned by 10002; or add cap_add SETUID," \
    "SETGID and CHOWN." >&2
  exec setpriv --inh-caps=-all --ambient-caps=-all --no-new-privs "$@"
fi

# run_as prints exactly three lines; logs, and any ERROR, go to stderr. A bad CONTROLLER_RUN_AS exits non-zero
# here, and set -e stops the container with that message. Parsed strictly, never eval'd: the uid and gid must be
# digits (both empty means stay root) and the reason is taken verbatim as text.
output="$(python -m app.run_as)"
mapfile -t lines <<<"$output"
[[ ${#lines[@]} -eq 3 ]] || fail "app.run_as printed ${#lines[@]} lines, not 3"
[[ "${lines[0]}" =~ ^RUN_UID=([0-9]*)$ ]] || fail "unexpected first line from app.run_as"
RUN_UID="${BASH_REMATCH[1]}"
[[ "${lines[1]}" =~ ^RUN_GID=([0-9]*)$ ]] || fail "unexpected second line from app.run_as"
RUN_GID="${BASH_REMATCH[1]}"
[[ "${lines[2]}" == RUN_REASON=* ]] || fail "unexpected third line from app.run_as"
RUN_REASON="${lines[2]#RUN_REASON=}"
if { [[ -z "$RUN_UID" ]] && [[ -n "$RUN_GID" ]]; } || { [[ -n "$RUN_UID" ]] && [[ -z "$RUN_GID" ]]; }; then
  fail "app.run_as gave a uid without a gid, or the reverse"
fi
export CONTROLLER_RUN_AS_REASON="$RUN_REASON"

if [[ -z "$RUN_UID" ]]; then
  echo "entrypoint: WARNING: CONTROLLER_RUN_AS=root: running as root (not recommended)" >&2
  exec "$@"
fi
if [[ "$RUN_UID" -eq 0 || "$RUN_GID" -eq 0 ]]; then
  fail "refusing to drop to uid $RUN_UID gid $RUN_GID"
fi

if ! getent group "$RUN_GID" >/dev/null; then
  groupadd --gid "$RUN_GID" "auto-browser-$RUN_GID"
fi
if ! getent passwd "$RUN_UID" >/dev/null; then
  useradd --uid "$RUN_UID" --gid "$RUN_GID" --no-create-home --home-dir /home/controller \
    --shell /usr/sbin/nologin "auto-browser-$RUN_UID"
fi

# docker_ephemeral talks to the daemon through a mounted socket (docker-compose.isolation.yml). Its group is the
# docker group on a Linux host; on Docker Desktop the socket shows as root:root, so the controller joins group 0
# there: still not uid 0, and with no capabilities (docs/deployment.md lists what group 0 can write in the image).
groups=()
docker_host="${ISOLATED_DOCKER_HOST:-${DOCKER_HOST:-unix:///var/run/docker.sock}}"
if [[ "$docker_host" == unix://* ]]; then
  socket="${docker_host#unix://}"
  if [[ -S "$socket" ]]; then
    socket_gid="$(stat -c %g "$socket")"
    groups+=("$socket_gid")
    if [[ "$socket_gid" == 0 ]]; then
      echo "entrypoint: $socket belongs to group 0, as on Docker Desktop: the controller joins group 0 to use it" >&2
    fi
  fi
fi

# No capabilities in any set, and no_new_privs even without compose's no-new-privileges. The bounding set can only
# be cleared while CAP_SETPCAP (bit 8) is in it; a container started with cap_drop ALL has nothing to clear there.
caps=(--inh-caps=-all --ambient-caps=-all --no-new-privs)
cap_bnd="$(awk '/^CapBnd:/ {print $2}' /proc/self/status)"
if (( (16#$cap_bnd & (1 << 8)) != 0 )); then
  caps+=(--bounding-set=-all)
fi

export HOME=/home/controller
echo "entrypoint: running as uid $RUN_UID gid $RUN_GID${groups[*]:+ groups ${groups[*]}} ($RUN_REASON)" >&2
if [[ ${#groups[@]} -gt 0 ]]; then
  group_list="$(IFS=,; echo "${groups[*]}")"
  exec setpriv --reuid="$RUN_UID" --regid="$RUN_GID" --groups="$group_list" "${caps[@]}" "$@"
fi
exec setpriv --reuid="$RUN_UID" --regid="$RUN_GID" --clear-groups "${caps[@]}" "$@"
