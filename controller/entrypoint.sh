#!/usr/bin/env bash
# The controller never stays root unless CONTROLLER_RUN_AS=root (not recommended).
#
# As root this only (1) asks app/run_as.py which user to run as (the owner of /data when that is not root, so a
# host user keeps its files and its 0600 data/api-token; otherwise the image's controller user, uid 10001) and lets it
# hand that user the root-owned entries of the controller's own /data subtrees; (2) makes sure that uid has a passwd
# entry (ssh refuses a uid without one); (3) adds the group of a mounted Docker socket, for docker_ephemeral. Then it
# execs the command as that user through setpriv (util-linux, already in the base image).
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  # Started as a non-root user already (docker run --user, a Kubernetes runAsUser): nothing to drop.
  export CONTROLLER_RUN_AS_REASON="${CONTROLLER_RUN_AS_REASON:-started as uid $(id -u) by the container runtime}"
  exec "$@"
fi

# Prints RUN_UID, RUN_GID and RUN_REASON (shell-quoted); logs, and any ERROR, go to stderr. A bad
# CONTROLLER_RUN_AS exits non-zero here, and set -e stops the container with that message.
assignments="$(python -m app.run_as)"
eval "$assignments"
export CONTROLLER_RUN_AS_REASON="$RUN_REASON"

if [[ -z "$RUN_UID" ]]; then
  echo "entrypoint: WARNING: CONTROLLER_RUN_AS=root: running as root (not recommended)" >&2
  exec "$@"
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
# there: still not uid 0, and with no capabilities.
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

export HOME=/home/controller
echo "entrypoint: running as uid $RUN_UID gid $RUN_GID${groups[*]:+ groups ${groups[*]}} ($RUN_REASON)" >&2
if [[ ${#groups[@]} -gt 0 ]]; then
  group_list="$(IFS=,; echo "${groups[*]}")"
  exec setpriv --reuid="$RUN_UID" --regid="$RUN_GID" --groups="$group_list" "$@"
fi
exec setpriv --reuid="$RUN_UID" --regid="$RUN_GID" --clear-groups "$@"
