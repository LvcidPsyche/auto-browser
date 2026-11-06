#!/usr/bin/env bash

# Reads KEY=VALUE lines from a .env file and exports them. The file is parsed, never
# sourced: a value like $(cmd) or `cmd` is kept as text and not run, and nothing is
# expanded. Blank lines and # comments are skipped, as is an `export ` prefix. Everything
# after the first = is the value, kept intact. Quoting follows docker compose's .env
# reader where that is safe:
#   'single'  literal; only a backslash before the quote is an escape (see __lre_parse_quoted)
#   "double"  escapes as compose-go's expandEscapes: \" \\ \$ \a \b \f \n \r \t \v (any other backslash stays as
#             written; a value holding \0 is skipped by choice, so compose reads that key itself)
#   unquoted  ends at a whitespace-preceded # comment; `KEY=   # note` is empty
# Text after a closing quote (other than a # comment), a quote that spans several lines (the lines up to the
# closing quote are skipped too), a double-quoted \0 (skipped by choice: compose reads that key itself), and any
# line that is not KEY=VALUE are skipped with a warning naming the line number, never its text.
# A UTF-8 BOM before the first key is ignored. Keys that change how the shell or this
# loader behaves are refused with a warning naming the key (see __lre_refused).
#
# Locals use a __lre_ prefix so a .env key cannot overwrite the parser's own state.

# Whether a .env key is on the fixed denylist: shell startup, expansion and behaviour hooks, the dynamic
# loader, the interpreters' startup hooks (PYTHON*, PERL5*, PERLLIB, NODE_OPTIONS, BROWSER, which webbrowser.open()
# runs), HOME and XDG_CONFIG_HOME (which login shells and curl read profiles and a .curlrc from), CURL_HOME, the
# locale (LANG, LC_*), the proxy variables curl reads (HTTP_PROXY, HTTPS_PROXY, ALL_PROXY, NO_PROXY and their
# lower-case forms), and the smoke scripts' own names. SMOKE_* is not refused as a
# whole: doctor.sh reads SMOKE_URL, SMOKE_PROVIDER and SMOKE_GOAL from .env.
__lre_denied_name() {
  case "$1" in
    BASH_ENV | ENV | BASH_FUNC_* | SHELLOPTS | BASHOPTS | IFS | PATH | PS4 | PROMPT_COMMAND | CDPATH | GLOBIGNORE) return 0 ;;
    POSIXLY_CORRECT | BASH_COMPAT | EXECIGNORE | FUNCNEST | TMOUT | LANG | LC_* | HOME) return 0 ;;
    PYTHON* | BROWSER | PERL5* | PERLLIB | NODE_OPTIONS) return 0 ;;
    CURL_HOME | XDG_CONFIG_HOME | HTTP_PROXY | HTTPS_PROXY | ALL_PROXY | NO_PROXY) return 0 ;;
    http_proxy | https_proxy | all_proxy | no_proxy) return 0 ;;
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
  # No fork for the common key: a subshell costs about 130 ms on Git Bash, and a copied .env.example has 176 keys.
  case "${OSTYPE:-}" in
    msys* | cygwin*)
      if [[ "$1" == *[a-z]* ]]; then
        local __lre_nocase=0 __lre_hit=1
        shopt -q nocasematch && __lre_nocase=1
        shopt -s nocasematch
        __lre_denied_name "$1" && __lre_hit=0
        ((__lre_nocase)) || shopt -u nocasematch
        ((__lre_hit)) || return 0
      fi
      ;;
  esac
  # `declare -p` (not `[[ -v ]]`, which needs bash 4.2, nor `${!1+set}`, which reads a `local x` with no value as
  # unset) succeeds for any declared name.
  declare -p "$1" >/dev/null 2>&1 || return 1
  local __lre_decl
  __lre_decl="$(declare -p "$1" 2>/dev/null)" || return 1
  case "$__lre_decl" in
    "declare -x"* | "declare -"[a-zA-Z]x*) return 1 ;;
    *) return 0 ;;
  esac
}

# Parses a quoted value, escape for escape as docker compose's reader (compose-go dotenv/parser.go) does. $1 is
# the text starting at the opening quote; it may hold newlines. Sets __lre_out to the value. Returns:
#   0  a value, followed by nothing or a # comment
#   1  text after the closing quote
#   2  no closing quote
# It also sets __lre_bad_escape to 1 (and to 0 first) when a double-quoted value holds a \0, which compose-go reads in
# a way not worth copying (it rewrites \0 to a backslash first and returns that text when the octal then fails to
# decode, so "a\012b" is a\12b there); the caller skips such a value.
# In both kinds of quote a backslash before the quote character is that character, and a backslash pair is
# \\ in double quotes and stays two backslashes in single quotes. Only double quotes expand: \a \b \f \n \r \t \v
# \$ (a literal $), \" and \\. A backslash before any other character is kept.
__lre_parse_quoted() {
  local __lre_text="$1" __lre_quote="${1:0:1}" __lre_i __lre_c __lre_n __lre_tail
  __lre_out=""
  __lre_bad_escape=0
  for ((__lre_i = 1; __lre_i < ${#__lre_text}; __lre_i++)); do
    __lre_c="${__lre_text:__lre_i:1}"
    if [[ "$__lre_c" == "$__lre_quote" ]]; then
      __lre_tail="${__lre_text:__lre_i+1}"
      __lre_tail="${__lre_tail#"${__lre_tail%%[![:space:]]*}"}"
      [[ -z "$__lre_tail" || "$__lre_tail" == "#"* ]]
      return
    fi
    if [[ "$__lre_c" == "\\" ]]; then
      __lre_n="${__lre_text:__lre_i+1:1}"
      if [[ "$__lre_n" == "$__lre_quote" ]]; then
        __lre_out+="$__lre_n"
        ((__lre_i++))
        continue
      fi
      if [[ "$__lre_n" == "\\" ]]; then
        if [[ "$__lre_quote" == '"' ]]; then __lre_out+="\\"; else __lre_out+="\\\\"; fi
        ((__lre_i++))
        continue
      fi
      if [[ "$__lre_quote" == '"' ]]; then
        case "$__lre_n" in
          n) __lre_out+=$'\n'; ((__lre_i++)); continue ;;
          r) __lre_out+=$'\r'; ((__lre_i++)); continue ;;
          t) __lre_out+=$'\t'; ((__lre_i++)); continue ;;
          a) __lre_out+=$'\a'; ((__lre_i++)); continue ;;
          b) __lre_out+=$'\b'; ((__lre_i++)); continue ;;
          f) __lre_out+=$'\f'; ((__lre_i++)); continue ;;
          v) __lre_out+=$'\v'; ((__lre_i++)); continue ;;
          '$') __lre_out+='$'; ((__lre_i++)); continue ;;
          # compose-go rewrites \0 before decoding and returns the rewritten text when decoding fails, so which
          # \0 forms decode is not worth guessing: every one is left to compose. The scan goes on to the closing
          # quote, so the caller skips the whole value, however many lines it spans.
          0) __lre_bad_escape=1 ;;
        esac
      fi
      __lre_out+="\\"
      continue
    fi
    __lre_out+="$__lre_c"
  done
  return 2
}

load_repo_env() {
  local __lre_file="${1:-.env}"
  if [[ ! -f "$__lre_file" ]]; then
    return 0
  fi

  # xtrace off while the values are read and exported (the .env holds the tokens), restored after: `bash -x` would
  # otherwise print every value.
  local __lre_xtrace=0
  if [[ $- == *x* ]]; then __lre_xtrace=1; fi
  { set +x; } 2>/dev/null
  local __lre_pair_re='^([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=(.*)$'
  local __lre_line __lre_key __lre_raw __lre_value __lre_out __lre_number=0 __lre_start __lre_rc __lre_next=""
  local __lre_bad_escape=0
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
      __lre_start=$__lre_number
      __lre_rc=0
      __lre_parse_quoted "$__lre_value" || __lre_rc=$?
      # No closing quote on this line: a multi-line value (a PEM, say). Its lines are skipped up to the one that
      # closes it, or the end of the file, so that base64 padding ("abc==") is never read as a KEY=VALUE line.
      while ((__lre_rc == 2)) && IFS= read -r __lre_next || [[ -n "$__lre_next" ]]; do
        ((__lre_number++)) || true
        __lre_value+=$'\n'"${__lre_next%$'\r'}"
        # A line without the opening quote character cannot close the value (an escape only protects a quote), so
        # the whole accumulated value is parsed again only when one arrives: re-parsing it for every added line made
        # a PEM-sized value quadratic.
        if [[ "$__lre_next" != *"${__lre_value:0:1}"* ]]; then
          __lre_next=""
          continue
        fi
        __lre_next=""
        __lre_rc=0
        __lre_parse_quoted "$__lre_value" || __lre_rc=$?
      done
      # A value with a \0 is skipped whole, only once its closing quote is found (or the file ends).
      if ((__lre_rc == 0 && __lre_bad_escape)); then __lre_rc=3; fi
      case "$__lre_rc" in
        0) ;;
        1) echo "load_repo_env: ${__lre_file}:${__lre_start}: text after the closing quote, ignored" >&2; continue ;;
        2) echo "load_repo_env: ${__lre_file}:${__lre_start}: no closing quote before the end of the file, the rest is ignored" >&2; continue ;;
        *) echo "load_repo_env: ${__lre_file}:${__lre_start}: the value has an escape the scripts cannot read as docker compose does, ignored" >&2; continue ;;
      esac
      if ((__lre_number > __lre_start)); then
        echo "load_repo_env: ${__lre_file}:${__lre_start}: a quoted value over several lines (to line ${__lre_number}), ignored" >&2
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
  if ((__lre_xtrace)); then set -x; fi
}

# True when a container path lies under /data/ with no ".." component: /data/../etc matches /data/* and
# leaves /data (bootstrap_cli_auth.sh refuses any other CLI_HOME).
container_path_under_data() {
  [[ "$1" == /data/* && "/$1/" != */../* ]]
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

# The controller's bearer header as a curl config line, for `curl -q --config -` on
# stdin (pipe it in: curl_auth_config | curl -q --config - ...; the -q keeps a
# .curlrc out of a request carrying the token). A command line is
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

# The hosts curl reaches directly whatever HTTP_PROXY, HTTPS_PROXY or ALL_PROXY says: a request to a loopback
# controller carries the tokens, and through a proxy it would hand them to the proxy. curl's --noproxy takes a
# comma-separated list (an IPv6 address unbracketed) and replaces NO_PROXY for that curl, so the environment's own
# list (no_proxy, which curl reads first, else NO_PROXY) follows them. The scripts' base URL host (API_BASE_URL, else
# AUTO_BROWSER_BASE_URL) is listed too when it is another loopback address (127.0.0.2, say): named exactly, since
# older curls take no CIDR. curl reads `*` as every host only when it is the whole list, so an inherited `*` is
# printed alone.
loopback_noproxy() {
  local inherited="${no_proxy:-${NO_PROXY:-}}"
  local trimmed="${inherited//[[:space:]]/}"
  if [[ "$trimmed" == "*" ]]; then
    printf '%s' "*"
    return 0
  fi
  local hosts="localhost,127.0.0.1,::1"
  local host
  host="$(base_url_loopback_host "${API_BASE_URL:-${AUTO_BROWSER_BASE_URL:-}}")"
  if [[ -n "$host" && ",${hosts}," != *",${host},"* ]]; then
    hosts="${hosts},${host}"
  fi
  printf '%s' "${hosts}${inherited:+,${inherited}}"
}

# The host of a base URL when it is a loopback address (localhost, anything in 127.0.0.0/8, ::1), IPv6 unbracketed
# as --noproxy takes it; nothing otherwise.
base_url_loopback_host() {
  local rest="${1#*://}"
  rest="${rest%%/*}"
  rest="${rest##*@}"
  local host
  if [[ "$rest" == \[* ]]; then
    host="${rest#\[}"
    host="${host%%\]*}"
  else
    host="${rest%%:*}"
  fi
  host="$(printf '%s' "$host" | tr '[:upper:]' '[:lower:]')"
  if [[ "$host" == "localhost" || "$host" == "::1" || "$host" =~ ^127\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
    printf '%s' "$host"
  fi
}

# curl that sends the controller's bearer token. Without it every API call is a
# 401 and the smoke scripts could only pass against an open controller.
bearer_curl() {
  # -q first: curl would otherwise read a .curlrc (through CURL_HOME, XDG_CONFIG_HOME or HOME) that can add a proxy
  # or another header to a request carrying the token.
  curl_auth_config | curl -q --noproxy "$(loopback_noproxy)" --config - "$@"
}

# The takeover token, the person's second secret that GET /sessions/{session_id}/takeover-link demands:
# TAKEOVER_TOKEN when set, else the token the compose controller saved in /data/takeover-token
# (<repo>/data/takeover-token on the host). Not TAKEOVER_TOKEN_FILE: docker-compose.yml pins the controller's to
# /data/takeover-token (and passes TAKEOVER_TOKEN from the shell or .env), so a different TAKEOVER_TOKEN_FILE in
# .env or the shell names some other controller's file. Prints nothing when there is none.
takeover_token() {
  if [[ -n "${TAKEOVER_TOKEN:-}" ]]; then
    printf '%s' "${TAKEOVER_TOKEN}"
    return 0
  fi
  local token_file
  token_file="$(resolve_repo_host_path "${ROOT_DIR}" /data/takeover-token)"
  if [[ -r "$token_file" ]]; then
    tr -d '[:space:]' < "$token_file"
  elif [[ -e "$token_file" ]]; then
    echo "Cannot read the takeover token in $token_file; rerun with sudo, or read it with:" >&2
    echo "  docker compose exec -T controller cat /data/takeover-token" >&2
  fi
}

# A filter for the config curl_auth_config prints: it passes that config through and adds the X-Takeover-Token
# header, so both headers reach curl in one config on stdin and neither token goes in argv. xtrace is off for it,
# as for curl_auth_config.
with_takeover_header() {
  { set +x; } 2>/dev/null
  cat
  local token
  token="$(takeover_token)"
  if [[ "$token" == *$'\n'* || "$token" == *$'\r'* ]]; then
    echo "The takeover token contains a line break; fix TAKEOVER_TOKEN (or data/takeover-token)." >&2
    return 1
  fi
  if [[ -n "$token" ]]; then
    token="${token//\\/\\\\}"
    printf 'header = "X-Takeover-Token: %s"\n' "${token//\"/\\\"}"
  fi
}

# curl that sends the bearer token and the takeover token, for GET /sessions/{session_id}/takeover-link only.
takeover_curl() {
  curl_auth_config | with_takeover_header | curl -q --noproxy "$(loopback_noproxy)" --config - "$@"
}

# True when takeover_token finds a token. In a subshell with xtrace off, so a trace never shows it.
have_takeover_token() (
  { set +x; } 2>/dev/null
  [[ -n "$(takeover_token)" ]]
)
