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
  load_repo_env "${ROOT_DIR}/.env"
  export COMPOSE_PROJECT_NAME="${SMOKE_PROJECT}"
  # The controller names its session containers <prefix>-<id> and their networks <prefix>-<id>-net.
  SMOKE_SESSION_PREFIX="${SMOKE_PROJECT}-session"
  export ISOLATED_BROWSER_CONTAINER_PREFIX="${SMOKE_SESSION_PREFIX}"
}

# Refuses (exit 1) unless the smoke can run without touching anything that is not its own.
# Run it before any side effect: before the EXIT trap, any rm, or any compose call.
#   isolated  the smoke's controller will reap every managed container and network on the
#             daemon, so none may exist (shared mode skips this: it starts no isolated sessions)
# Both modes need a reachable daemon, and no running stack of this checkout in another
# project (it shares ./data with the smoke).
smoke_preflight() {
  local mode="${1:-isolated}"
  local listed
  if ! listed="$(docker ps -a --format '{{.Names}}' 2>&1)" || ! listed="$(docker network ls --format '{{.Name}}' 2>&1)"; then
    echo "smoke preflight: cannot list the Docker daemon's containers and networks, so I cannot tell whether the smoke is safe to run; refusing to start." >&2
    echo "$listed" | head -3 >&2
    exit 1
  fi

  if [[ "$mode" == "isolated" ]]; then
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
  fi

  local dir rows row project name others=""
  for dir in "${ROOT_DIR}" "$(pwd -W 2>/dev/null || true)"; do
    [[ -n "$dir" ]] || continue
    rows="$(docker ps --filter "label=${SMOKE_COMPOSE_DIR_LABEL}=${dir}" \
      --format "{{.Label \"${SMOKE_COMPOSE_PROJECT_LABEL}\"}}|{{.Names}}")" || exit 1
    while IFS= read -r row; do
      [[ -n "$row" ]] || continue
      project="${row%%|*}"
      name="${row#*|}"
      [[ "$project" == "$SMOKE_PROJECT" ]] && continue
      others+="  ${name} (compose project ${project})"$'\n'
    done <<<"$rows"
  done
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
