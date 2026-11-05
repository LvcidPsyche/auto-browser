#!/usr/bin/env bash

# Helpers for the compose smoke scripts (smoke_*.sh) only; source scripts/load_env.sh first.
#
# A smoke run must not touch the operator's own stack. Two things make that hard:
#  - The isolated smokes start a controller in docker_ephemeral mode. At startup it
#    force-removes EVERY container labelled auto-browser.managed=true on the daemon and
#    deletes ./data/browser-sessions/* (SessionIsolationProvisioner: "one controller per
#    Docker host"), and a second compose project from the same checkout shares ./data.
#  - Compose project names say nothing about who owns a session container.
# So a smoke (1) runs in its own compose project, (2) refuses to start, before any side
# effect, when another stack could be hit (smoke_preflight), and (3) gives its controller a
# container-name prefix, so that cleanup removes only what carries that prefix.
#
# Callers set ROOT_DIR, then: smoke_setup; (define COMPOSE, an array naming this run's
# project with -p); smoke_preflight isolated|shared; trap on EXIT -> smoke_cleanup.

SMOKE_MANAGED_LABEL="auto-browser.managed"
SMOKE_MODE_LABEL="auto-browser.mode"
SMOKE_SESSION_ID_LABEL="auto-browser.session_id"
SMOKE_COMPOSE_DIR_LABEL="com.docker.compose.project.working_dir"
SMOKE_COMPOSE_PROJECT_LABEL="com.docker.compose.project"

# Chooses this run's compose project and loads .env. SMOKE_COMPOSE_PROJECT (read before
# .env, which cannot set it) names the project, default auto-browser-smoke: stable, so a
# rerun after a killed run takes over the leftovers. An ambient or .env COMPOSE_PROJECT_NAME
# usually names the operator's own stack, so it is replaced by this project.
smoke_setup() {
  SMOKE_PROJECT="${SMOKE_COMPOSE_PROJECT:-auto-browser-smoke}"
  if [[ ! "$SMOKE_PROJECT" =~ ^[a-z0-9][a-z0-9_-]*$ ]]; then
    echo "SMOKE_COMPOSE_PROJECT must be lowercase letters, digits, - and _ (compose's rule for project names)." >&2
    exit 1
  fi
  # The project a plain `docker compose` here would use: the folder's name, as compose normalizes it. A smoke in
  # that project would take over, and finally `compose down`, the operator's own stack.
  local default_project
  default_project="$(basename "${ROOT_DIR}" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
  if [[ "$SMOKE_PROJECT" == "$default_project" ]]; then
    echo "smoke: the compose project '${SMOKE_PROJECT}' is this checkout's own default project, so the smoke would run in (and tear down) your stack." >&2
    echo "Choose another name with SMOKE_COMPOSE_PROJECT, or rename the checkout folder." >&2
    exit 1
  fi
  # .env may set API_PORT and NOVNC_PORT, and a value in the environment must win over it: that is how the smoke runs
  # beside a stack of another checkout.
  local ambient_api_port ambient_novnc_port
  local have_api_port="${API_PORT+set}" have_novnc_port="${NOVNC_PORT+set}"
  ambient_api_port="${API_PORT-}"
  ambient_novnc_port="${NOVNC_PORT-}"
  load_repo_env "${ROOT_DIR}/.env"
  [[ -z "$have_api_port" ]] || export API_PORT="${ambient_api_port}"
  [[ -z "$have_novnc_port" ]] || export NOVNC_PORT="${ambient_novnc_port}"
  # The smoke's controller reaps every managed container on the daemon it is pointed at. docker-compose.isolation.yml
  # passes these two through to it, and preflight only looks at this script's own daemon, so a value from .env would
  # send the reaper to a daemon nobody checked: the controller uses its mounted local socket instead.
  export ISOLATED_DOCKER_HOST=
  export ISOLATED_HOST_DATA_ROOT=
  export COMPOSE_PROJECT_NAME="${SMOKE_PROJECT}"
  # The controller names its session containers <prefix>-<id> and their networks <prefix>-<id>-net.
  SMOKE_SESSION_PREFIX="${SMOKE_PROJECT}-session"
  export ISOLATED_BROWSER_CONTAINER_PREFIX="${SMOKE_SESSION_PREFIX}"
}

# Refuses (exit 1) when the daemon has auto-browser session containers or networks. The isolated smokes' controller
# removes every one of them when it starts, so none may exist then: smoke_preflight runs this first, and the smokes
# run it again right before the controller starts, after the image build, which takes minutes in which another
# stack could create some. A window of seconds remains; the full fix is a label naming the controller that owns a
# session, which the controller does not set.
smoke_check_managed() {
  local containers networks
  containers="$(docker ps -a --filter "label=${SMOKE_MANAGED_LABEL}=true" --format '{{.Names}}')" || exit 1
  networks="$(docker network ls --filter "label=${SMOKE_MANAGED_LABEL}=true" --format '{{.Name}}')" || exit 1
  if [[ -n "$containers" || -n "$networks" ]]; then
    echo "smoke preflight: this Docker daemon already has auto-browser session containers or networks:" >&2
    [[ -z "$containers" ]] || sed 's/^/  container /' <<<"$containers" >&2
    [[ -z "$networks" ]] || sed 's/^/  network   /' <<<"$networks" >&2
    echo "The smoke's controller removes every one of them when it starts. If they belong to a live stack, stop the smoke;" >&2
    echo "if they are leftovers (for example of a killed smoke run), remove them and run it again:" >&2
    [[ -z "$containers" ]] || echo "  docker rm -f $(tr '\n' ' ' <<<"$containers")" >&2
    [[ -z "$networks" ]] || echo "  docker network rm $(tr '\n' ' ' <<<"$networks")" >&2
    exit 1
  fi
}

# One spelling for a directory, so that compose's label (a Windows path with backslashes, C:\Users\me\repo, when
# the stack was started by Docker Desktop's compose), Git Bash's /c/Users/me/repo and pwd -W's C:/Users/me/repo
# compare equal: forward slashes, no trailing slash, and a lowercase drive letter written c:/...
smoke_normalize_path() {
  local path="${1//\\//}"
  while [[ "$path" == */ && "$path" != "/" ]]; do path="${path%/}"; done
  case "$path" in
    [A-Za-z]:/* | [A-Za-z]:) path="$(printf '%s' "${path:0:1}" | tr '[:upper:]' '[:lower:]')${path:1}" ;;
    /[A-Za-z]/* | /[A-Za-z]) path="$(printf '%s' "${path:1:1}" | tr '[:upper:]' '[:lower:]'):${path:2}" ;;
  esac
  printf '%s' "$path"
}

# Refuses (exit 1) unless the smoke can run without touching anything that is not its own.
# Run it before any side effect: before the EXIT trap, any rm, or any compose call.
#   isolated  the smoke's controller will reap every managed container and network on the
#             daemon, so none may exist (shared mode skips this: it starts no isolated sessions)
# Both modes need a reachable daemon, no running stack of this checkout in another compose
# project (it shares ./data with the smoke), and no running container in the smoke's own
# project (a smoke is in progress, or a killed run left it).
smoke_preflight() {
  local mode="${1:-isolated}"
  local listed
  if ! listed="$(docker ps -a --format '{{.Names}}' 2>&1)" || ! listed="$(docker network ls --format '{{.Name}}' 2>&1)"; then
    echo "smoke preflight: cannot list the Docker daemon's containers and networks, so I cannot tell whether the smoke is safe to run; refusing to start." >&2
    echo "$listed" | head -3 >&2
    exit 1
  fi

  [[ "$mode" != "isolated" ]] || smoke_check_managed

  # Running containers with their compose project directory and project name. The label is listed rather than
  # filtered on: a filter is an exact string match, and the spelling of the path differs between hosts.
  local rows row dir project name candidate others="" own="" is_here
  local -a candidates=()
  rows="$(docker ps --format "{{.Label \"${SMOKE_COMPOSE_DIR_LABEL}\"}}|{{.Label \"${SMOKE_COMPOSE_PROJECT_LABEL}\"}}|{{.Names}}")" || exit 1
  candidates+=("$(smoke_normalize_path "${ROOT_DIR}")")
  candidates+=("$(smoke_normalize_path "$(cd "${ROOT_DIR}" && pwd -P)")")
  candidates+=("$(smoke_normalize_path "$(cd "${ROOT_DIR}" && pwd -W 2>/dev/null || true)")")
  if command -v cygpath >/dev/null 2>&1; then
    candidates+=("$(smoke_normalize_path "$(cygpath -m "${ROOT_DIR}" 2>/dev/null || true)")")
  fi
  while IFS= read -r row; do
    [[ -n "$row" ]] || continue
    dir="${row%%|*}"
    project="${row#*|}"
    name="${project#*|}"
    project="${project%%|*}"
    if [[ "$project" == "$SMOKE_PROJECT" ]]; then
      own+="  ${name}"$'\n'
      continue
    fi
    [[ -n "$dir" ]] || continue
    dir="$(smoke_normalize_path "$dir")"
    is_here=""
    for candidate in "${candidates[@]}"; do
      [[ -z "$candidate" || "$candidate" != "$dir" ]] || is_here=1
    done
    [[ -z "$is_here" ]] || others+="  ${name} (compose project ${project})"$'\n'
  done <<<"$rows"
  if [[ -n "$own" ]]; then
    echo "smoke preflight: containers of the smoke's own compose project (${SMOKE_PROJECT}) are running:" >&2
    printf '%s' "$own" >&2
    echo "Either a smoke run is in progress, or a killed run left this behind: docker compose -p ${SMOKE_PROJECT} down --remove-orphans" >&2
    echo "(run that only if no other smoke is running)." >&2
    exit 1
  fi
  if [[ -n "$others" ]]; then
    echo "smoke preflight: a stack from this checkout (${ROOT_DIR}) is running in another compose project:" >&2
    printf '%s' "$others" >&2
    echo "It shares ./data with the smoke, which resets files there. Stop it (docker compose down) or run the smoke from another checkout" >&2
    echo "with its own API_PORT and NOVNC_PORT." >&2
    exit 1
  fi
}

# Removes what this run created and nothing else: the containers named <prefix>-... that
# carry the managed and mode=docker_ephemeral labels, then the compose stack (the controller
# must be gone, or it still holds an endpoint on each session network), then the networks
# named <prefix>-...-net that carry the managed, mode and session_id labels (the controller's
# own reaper requires all three). A failed listing removes nothing. Needs COMPOSE and
# SMOKE_SESSION_PREFIX.
smoke_cleanup() {
  local name
  while IFS= read -r name; do
    [[ "$name" == "${SMOKE_SESSION_PREFIX}-"* ]] || continue
    docker rm -f "$name" >/dev/null 2>&1 || true
  done < <(docker ps -a --filter "name=^${SMOKE_SESSION_PREFIX}-" --filter "label=${SMOKE_MANAGED_LABEL}=true" \
    --filter "label=${SMOKE_MODE_LABEL}=docker_ephemeral" --format '{{.Names}}' 2>/dev/null || true)
  "${COMPOSE[@]}" down --remove-orphans >/dev/null 2>&1 || true
  while IFS= read -r name; do
    [[ "$name" == "${SMOKE_SESSION_PREFIX}-"*-net ]] || continue
    docker network rm "$name" >/dev/null 2>&1 || true
  done < <(docker network ls --filter "name=^${SMOKE_SESSION_PREFIX}-" --filter "label=${SMOKE_MANAGED_LABEL}=true" \
    --filter "label=${SMOKE_MODE_LABEL}=docker_ephemeral" --filter "label=${SMOKE_SESSION_ID_LABEL}" \
    --format '{{.Name}}' 2>/dev/null || true)
}
