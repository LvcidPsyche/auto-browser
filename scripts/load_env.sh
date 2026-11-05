#!/usr/bin/env bash

# Reads KEY=VALUE lines from a .env file and exports them. The file is parsed, never
# sourced: a value like $(cmd) or `cmd` is kept as text and not run, and nothing is
# expanded. Blank lines and # comments are skipped, as is an `export ` prefix. Everything
# after the first = is the value, kept intact. Quoting follows docker compose's .env
# reader where that is safe:
#   'single'  literal, no escapes
#   "double"  literal except \" \\ \n and \r (any other backslash stays as written)
#   unquoted  ends at a whitespace-preceded # comment; `KEY=   # note` is empty
# Text after a closing quote (other than a # comment), an unterminated quote and any line
# that is not KEY=VALUE are skipped with a warning naming the line number, never its text.
# A UTF-8 BOM before the first key is ignored. Keys that change how the shell or this
# loader behaves are refused with a warning naming the key (see __lre_refused).
#
# Locals use a __lre_ prefix so a .env key cannot overwrite the parser's own state.

# Whether a .env key is on the fixed denylist: shell startup, expansion and behaviour hooks, the dynamic
# loader, the interpreters' startup hooks (PYTHON*, PERL5*, NODE_OPTIONS, BROWSER, which webbrowser.open() runs),
# HOME (which login shells read profiles from), and the smoke scripts' own names. SMOKE_* is not refused as a
# whole: doctor.sh reads SMOKE_URL, SMOKE_PROVIDER and SMOKE_GOAL from .env.
__lre_denied_name() {
  case "$1" in
    BASH_ENV | ENV | BASH_FUNC_* | SHELLOPTS | BASHOPTS | IFS | PATH | PS4 | PROMPT_COMMAND | CDPATH | GLOBIGNORE) return 0 ;;
    POSIXLY_CORRECT | BASH_COMPAT | EXECIGNORE | FUNCNEST | TMOUT | LC_ALL | LC_CTYPE | HOME) return 0 ;;
    PYTHON* | BROWSER | PERL5OPT | PERL5LIB | NODE_OPTIONS) return 0 ;;
    LD_* | DYLD_* | COMSPEC | PATHEXT) return 0 ;;
    SMOKE_PROJECT | SMOKE_COMPOSE_* | SMOKE_SESSION_* | SMOKE_*_LABEL | ROOT_DIR | COMPOSE | __lre_*) return 0 ;;
  esac
  return 1
}

# Whether a .env key must not be exported: a denylisted name, or a variable the calling script (or bash itself)
# set and did not export, which is the script's own. On Git Bash and Cygwin the denylist ignores case, because
# Windows reads environment names case-insensitively (`Path=` would reach a child as PATH).
__lre_refused() {
  __lre_denied_name "$1" && return 0
  case "${OSTYPE:-}" in
    msys* | cygwin*) __lre_denied_name "$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]')" && return 0 ;;
  esac
  # `declare -p` (not `[[ -v ]]`, which needs bash 4.2) fails for a name that is not set.
  local __lre_decl
  __lre_decl="$(declare -p "$1" 2>/dev/null)" || return 1
  case "$__lre_decl" in
    "declare -x"* | "declare -"[a-zA-Z]x*) return 1 ;;
    *) return 0 ;;
  esac
}

# Parses a quoted value. $1 is the text starting at the opening quote. Sets __lre_out to the
# value; returns 1 for an unterminated quote or text after the closing one.
__lre_parse_quoted() {
  local __lre_text="$1" __lre_quote="${1:0:1}" __lre_i __lre_c __lre_n __lre_tail
  __lre_out=""
  for ((__lre_i = 1; __lre_i < ${#__lre_text}; __lre_i++)); do
    __lre_c="${__lre_text:__lre_i:1}"
    if [[ "$__lre_c" == "$__lre_quote" ]]; then
      __lre_tail="${__lre_text:__lre_i+1}"
      __lre_tail="${__lre_tail#"${__lre_tail%%[![:space:]]*}"}"
      [[ -z "$__lre_tail" || "$__lre_tail" == "#"* ]]
      return
    fi
    if [[ "$__lre_quote" == '"' && "$__lre_c" == "\\" ]]; then
      __lre_n="${__lre_text:__lre_i+1:1}"
      case "$__lre_n" in
        '"' | "\\") __lre_out+="$__lre_n"; ((__lre_i++)) ;;
        n) __lre_out+=$'\n'; ((__lre_i++)) ;;
        r) __lre_out+=$'\r'; ((__lre_i++)) ;;
        *) __lre_out+="$__lre_c" ;;
      esac
      continue
    fi
    __lre_out+="$__lre_c"
  done
  return 1
}

load_repo_env() {
  local __lre_file="${1:-.env}"
  if [[ ! -f "$__lre_file" ]]; then
    return 0
  fi

  local __lre_pair_re='^([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=(.*)$'
  local __lre_line __lre_key __lre_raw __lre_value __lre_out __lre_number=0
  while IFS= read -r __lre_line || [[ -n "$__lre_line" ]]; do
    ((__lre_number++)) || true
    if ((__lre_number == 1)); then
      __lre_line="${__lre_line#$'\xEF\xBB\xBF'}"
    fi
    __lre_line="${__lre_line%$'\r'}"
    __lre_line="${__lre_line#"${__lre_line%%[![:space:]]*}"}"
    [[ -z "$__lre_line" || "$__lre_line" == "#"* ]] && continue
    __lre_line="${__lre_line#export[[:space:]]}"
    __lre_line="${__lre_line#"${__lre_line%%[![:space:]]*}"}"
    if [[ ! "$__lre_line" =~ $__lre_pair_re ]]; then
      echo "load_repo_env: ${__lre_file}:${__lre_number}: not a KEY=VALUE line, ignored" >&2
      continue
    fi
    __lre_key="${BASH_REMATCH[1]}"
    __lre_raw="${BASH_REMATCH[2]}"
    __lre_value="${__lre_raw#"${__lre_raw%%[![:space:]]*}"}"
    if [[ "$__lre_value" == [\"\']* ]]; then
      if ! __lre_parse_quoted "$__lre_value"; then
        echo "load_repo_env: ${__lre_file}:${__lre_number}: unterminated quote or text after the closing quote, ignored" >&2
        continue
      fi
      __lre_value="$__lre_out"
    elif [[ "$__lre_raw" != "$__lre_value" && "$__lre_value" == "#"* ]]; then
      __lre_value=""
    else
      __lre_value="${__lre_value%%[[:space:]]\#*}"
      __lre_value="${__lre_value%"${__lre_value##*[![:space:]]}"}"
    fi
    if __lre_refused "$__lre_key"; then
      echo "load_repo_env: ${__lre_file}:${__lre_number}: ${__lre_key} is not loaded from .env (it changes how the shell or the scripts behave), ignored" >&2
      continue
    fi
    export "$__lre_key=$__lre_value"
  done < "$__lre_file"
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

# The controller's bearer header as a curl config line, for `curl --config -` on
# stdin (pipe it in: curl_auth_config | curl --config - ...). A command line is
# readable by every local user (ps, /proc/<pid>/cmdline), so the token never goes
# in argv, and a config on stdin is never written to disk. It takes no argument
# and turns xtrace off for itself (it runs in the pipeline's subshell, so the
# caller's trace is untouched): `bash -x` would otherwise print the token as a
# function argument or a printf operand. Prints nothing without a token.
curl_auth_config() {
  { set +x; } 2>/dev/null
  local token
  token="$(api_token)"
  if [[ "$token" == *$'\n'* || "$token" == *$'\r'* ]]; then
    # No HTTP header can carry it, and here it would split the config line: send nothing.
    echo "The API token contains a line break; fix API_BEARER_TOKEN (or data/api-token)." >&2
    return 1
  fi
  if [[ -n "$token" ]]; then
    token="${token//\\/\\\\}"
    printf 'header = "Authorization: Bearer %s"\n' "${token//\"/\\\"}"
  fi
}

# curl that sends the controller's bearer token. Without it every API call is a
# 401 and the smoke scripts could only pass against an open controller.
bearer_curl() {
  curl_auth_config | curl --config - "$@"
}
