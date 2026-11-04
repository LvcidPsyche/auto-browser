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
  # Started as a non-root user already (docker run --user, a Kubernetes runAsUser): no capabilities to drop, but
  # no_new_privs and empty inheritable and ambient sets all the same (a plain docker run sets no no-new-privileges).
  export CONTROLLER_RUN_AS_REASON="${CONTROLLER_RUN_AS_REASON:-started as uid $(id -u) by the container runtime}"
  exec setpriv --no-new-privs --inh-caps=-all --ambient-caps=-all "$@"
fi

fail() {
  echo "entrypoint: ERROR: $*" >&2
  exit 70
}

# The capability sets, read once and strictly: anything but 16 hex digits stops the container (exit 70) rather than
# letting a failed arithmetic test fall through to a drop that may not happen.
read_caps() {
  local value
  value="$(awk -v key="$1:" '$1 == key {print $2}' /proc/self/status)"
  [[ "$value" =~ ^[0-9a-fA-F]{16}$ ]] || fail "unreadable $1 in /proc/self/status: '$value'"
  echo "$value"
}
cap_eff="$(read_caps CapEff)"
cap_bnd="$(read_caps CapBnd)"
# The bounding set can only be cleared while CAP_SETPCAP (bit 8) is in the effective set.
has_setpcap=0
if (( (16#$cap_eff >> 8) & 1 )); then has_setpcap=1; fi

# CONTROLLER_RUN_AS is checked here too: the branch below never runs app.run_as, which checks it on the main path.
# As app.run_as reads it: leading and trailing whitespace trimmed, lowercased (Python's .strip().lower()).
run_as_setting="${CONTROLLER_RUN_AS:-}"
run_as_setting="${run_as_setting#"${run_as_setting%%[![:space:]]*}"}"
run_as_setting="${run_as_setting%"${run_as_setting##*[![:space:]]}"}"
run_as_setting="${run_as_setting,,}"
case "$run_as_setting" in
  "" | auto | root) ;;
  *) fail "CONTROLLER_RUN_AS must be auto or root, got '${CONTROLLER_RUN_AS}'" ;;
esac

# Switching users needs CAP_SETUID (bit 7) and CAP_SETGID (bit 6). Without them (cap_drop ALL, or a cap_drop of
# either) the container still starts as uid 0, with no_new_privs and every capability it can clear cleared; the
# WARNING and the readiness reason name whatever remains.
if (( ((16#$cap_eff >> 7) & 1) == 0 || ((16#$cap_eff >> 6) & 1) == 0 )); then
  drop=(--inh-caps=-all --ambient-caps=-all --no-new-privs)
  if (( has_setpcap )); then
    drop+=(--bounding-set=-all)
  fi
  # setpriv leaves uid 0 its permitted and effective sets: app.drop_caps capsets them (and inheritable) to 0 under
  # no_new_privs before the exec, so the command starts with no capability whatever the container was given. The
  # token check below runs through the same chain, so it sees exactly what the controller will.
  dropped=(setpriv "${drop[@]}" python -m app.drop_caps --)
    kept="no capabilities"
  export CONTROLLER_RUN_AS_REASON="no CAP_SETUID/CAP_SETGID, so uid 0 with $kept"
  echo "entrypoint: WARNING: this container cannot switch users (no CAP_SETUID/CAP_SETGID): running as uid 0 with" \
    "$kept and no_new_privs. To run as the controller user instead, start it as that user: compose" \
    "user: \"10002:10002\" (or docker run --user 10002:10002), with ./data owned by 10002; or add cap_add CHOWN," \
    "DAC_READ_SEARCH, SETUID and SETGID." >&2
  # Measured: on a /data another uid owns, uid 0 without CAP_DAC_OVERRIDE (bit 1) or CAP_DAC_READ_SEARCH (bit 2)
  # cannot read its 0600 files, and the controller exits at startup on the API token.
  data_root="${CONTROLLER_DATA_ROOT:-/data}"
  data_owner="$(stat -c %u "$data_root" 2>/dev/null || true)"
  if [[ "$data_owner" =~ ^[0-9]+$ ]] && (( data_owner != 0 )); then
    # Never browser-node's uid (10001): the controller's own user then. Nobody (65534, root-squashed NFS) is the uid
    # that can read such data, as app.run_as decides too.
    as_uid="$data_owner"
    chown_hint=""
    if (( data_owner == 10001 )); then
      as_uid=10002
      chown_hint=" (chown ./data to 10002 first)"
    fi
    way_out="start it as uid $as_uid$chown_hint (compose user: \"$as_uid:$as_uid\", or docker run --user $as_uid:$as_uid), or add the capabilities above"
    # The path is API_TOKEN_FILE from the environment (a .env file is the controller's, not this script's).
    token_file="${API_TOKEN_FILE:-$data_root/api-token}"
    tokenless="$(printf '%s' "${API_ALLOW_TOKENLESS_LOOPBACK:-}" | tr '[:upper:]' '[:lower:]')"
    if [[ -n "${API_BEARER_TOKEN:-}" || -n "${API_BEARER_TOKENS:-}" || "$tokenless" =~ ^(1|true|yes|on)$ ]]; then
      : # the controller never reads the file then
    elif ! "${dropped[@]}" test -x "$(dirname "$token_file")"; then
      fail "$data_root belongs to uid $data_owner, and uid 0 with $kept cannot reach the API token's directory" \
        "$(dirname "$token_file"), so the controller cannot start: $way_out."
    elif "${dropped[@]}" test -e "$token_file" && ! "${dropped[@]}" test -r "$token_file"; then
      fail "$data_root belongs to uid $data_owner, and uid 0 with $kept cannot read the API token" \
        "$token_file, so the controller cannot start: $way_out."
    fi
    # With no capability at all, uid 0 can neither read the owner's 0600 files nor write into a 0755 /data.
    echo "entrypoint: WARNING: $data_root belongs to uid $data_owner, and uid 0 with no capabilities cannot read" \
      "its private files or write into it: the controller will likely fail to start. To fix it, $way_out." >&2
  fi
  exec "${dropped[@]}" "$@"
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
# be cleared while CAP_SETPCAP (bit 8) is in the effective set.
caps=(--inh-caps=-all --ambient-caps=-all --no-new-privs)
if (( has_setpcap )); then
  caps+=(--bounding-set=-all)
fi

export HOME=/home/controller
echo "entrypoint: running as uid $RUN_UID gid $RUN_GID${groups[*]:+ groups ${groups[*]}} ($RUN_REASON)" >&2
if [[ ${#groups[@]} -gt 0 ]]; then
  group_list="$(IFS=,; echo "${groups[*]}")"
  exec setpriv --reuid="$RUN_UID" --regid="$RUN_GID" --groups="$group_list" "${caps[@]}" "$@"
fi
exec setpriv --reuid="$RUN_UID" --regid="$RUN_GID" --clear-groups "${caps[@]}" "$@"
