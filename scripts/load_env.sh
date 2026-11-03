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

# The controller's bearer token: API_BEARER_TOKEN when set, else the token the
# compose controller saved in /data/api-token (<repo>/data/api-token on the host).
# Not API_TOKEN_FILE: these scripts drive the compose stack, which pins that path,
# so a different API_TOKEN_FILE in .env or the shell names some other controller's file.
# Prints nothing when there is none, as for a
# controller run with API_ALLOW_TOKENLESS_LOOPBACK=true. Callers read it on every
# call: a controller the script itself starts writes the file only once it is up.
api_token() {
  if [[ -n "${API_BEARER_TOKEN:-}" ]]; then
    printf '%s' "${API_BEARER_TOKEN}"
    return 0
  fi
  local token_file
  token_file="$(resolve_repo_host_path "${ROOT_DIR}" /data/api-token)"
  if [[ -r "$token_file" ]]; then
    tr -d '[:space:]' < "$token_file"
  elif [[ -e "$token_file" ]]; then
    echo "Cannot read the API token in $token_file; rerun with sudo, or read it with:" >&2
    echo "  docker compose exec -T controller cat /data/api-token" >&2
  fi
}

# curl that sends the controller's bearer token. Without it every API call is a
# 401 and the smoke scripts could only pass against an open controller.
bearer_curl() {
  local token
  token="$(api_token)"
  if [[ -n "$token" ]]; then
    curl -H "Authorization: Bearer ${token}" "$@"
  else
    curl "$@"
  fi
}
