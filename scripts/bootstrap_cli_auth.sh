#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

source "$ROOT_DIR/scripts/load_env.sh"
load_repo_env "$ROOT_DIR/.env"

usage() {
  cat <<'USAGE'
Usage:
  ./scripts/bootstrap_cli_auth.sh [codex|claude|gemini|openai|all ...]

What it does:
  Opens the requested provider CLI interactively inside the controller image with
  HOME=$CLI_HOME (default /data/cli-home).

  This helper only supports writable CLI_HOME paths under /data/... because the
  default compose stack mounts ./data at /data inside the controller container.
  If you use the host-subscriptions override with a host-style CLI_HOME such as
  /home/youruser, sign in on the host first instead of using this helper.

Examples:
  ./scripts/bootstrap_cli_auth.sh codex
  ./scripts/bootstrap_cli_auth.sh claude gemini
  ./scripts/bootstrap_cli_auth.sh all
USAGE
}

providers=()
for raw in "$@"; do
  case "${raw}" in
    openai|codex)
      providers+=(codex)
      ;;
    claude)
      providers+=(claude)
      ;;
    gemini)
      providers+=(gemini)
      ;;
    all)
      providers+=(codex claude gemini)
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo >&2 "Unknown provider: ${raw}"
      usage
      exit 1
      ;;
  esac
done

if [[ ! -t 0 || ! -t 1 ]]; then
  echo >&2 "bootstrap_cli_auth.sh must run in an interactive terminal"
  exit 1
fi

if [[ $# -eq 0 ]]; then
  set -- all
  providers+=(codex claude gemini)
fi

# De-duplicate while preserving order.
unique_providers=()
for provider in "${providers[@]}"; do
  skip=false
  for seen in "${unique_providers[@]:-}"; do
    if [[ "${seen}" == "${provider}" ]]; then
      skip=true
      break
    fi
  done
  if [[ "${skip}" == false ]]; then
    unique_providers+=("${provider}")
  fi
done

# The provider CLIs are in the controller image only when it was built with
# INSTALL_AGENT_CLIS=true. Rebuilding here would not stick: the next plain
# `docker compose up --build` would drop them again. The setting belongs in .env.
if [[ "${INSTALL_AGENT_CLIS:-false}" != "true" ]]; then
  echo >&2 "The controller image only includes the codex/claude/gemini CLIs when built with INSTALL_AGENT_CLIS=true."
  echo >&2 "Add INSTALL_AGENT_CLIS=true to .env, run: docker compose build controller"
  echo >&2 "then run this script again."
  exit 1
fi

container_cli_home="${CLI_HOME:-/data/cli-home}"
if [[ "$container_cli_home" != /data/* ]]; then
  echo >&2 "bootstrap_cli_auth.sh only supports CLI_HOME paths under /data/."
  echo >&2 "Current CLI_HOME=${container_cli_home}"
  echo >&2 "Use CLI_HOME=/data/cli-home for in-container bootstrap, or sign in on the host and use docker-compose.host-subscriptions.yml."
  exit 1
fi
host_cli_home="$(resolve_repo_host_path "$ROOT_DIR" "$container_cli_home")"
mkdir -p "$host_cli_home"

for provider in "${unique_providers[@]}"; do
  echo
  echo "=== Bootstrapping ${provider} auth inside the controller container ==="
  echo "Using CLI_HOME=${container_cli_home}"
  echo "Complete login in the interactive CLI, then exit back to this shell."
  docker compose run --rm \
    -e "CLI_HOME=${container_cli_home}" \
    -e "HOME=${container_cli_home}" \
    --entrypoint bash \
    controller \
    -lc "export NO_COLOR=1; exec ${provider}"
done

echo
echo "Done. Current auth cache contents:"
find "$host_cli_home" -maxdepth 2 \( -name '.codex' -o -name '.claude' -o -name '.claude.json' -o -name '.gemini' \) -print | sed 's#^#- #' || true
