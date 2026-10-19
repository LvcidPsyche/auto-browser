#!/usr/bin/env bash

load_repo_env() {
  local env_file="${1:-.env}"
  if [[ ! -f "$env_file" ]]; then
    return 0
  fi

  local previous_allexport
  previous_allexport="$(set -o | awk '$1 == "allexport" { print $2 }')"
  set -a
  # Trusted repo-local config file. Supports quoted values and embedded spaces.
  # shellcheck disable=SC1090
  source "$env_file"
  if [[ "$previous_allexport" != "on" ]]; then
    set +a
  fi
}

resolve_repo_host_path() {
  local root_dir="$1"
  local raw_path="$2"
  if [[ "$raw_path" == /data/* ]]; then
    echo "$root_dir/data/${raw_path#/data/}"
    return 0
  fi
  if [[ "$raw_path" == /* ]]; then
    echo "$raw_path"
    return 0
  fi
  echo "$root_dir/$raw_path"
}

# curl that sends the controller's bearer token when API_BEARER_TOKEN is set.
# Without it every API call to an authenticated controller is a 401 and the
# smoke scripts can only pass against an open one.
bearer_curl() {
  if [[ -n "${API_BEARER_TOKEN:-}" ]]; then
    curl -H "Authorization: Bearer ${API_BEARER_TOKEN}" "$@"
  else
    curl "$@"
  fi
}
