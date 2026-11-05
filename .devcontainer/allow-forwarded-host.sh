#!/usr/bin/env bash
# Adds this Codespace's forwarded controller host to a CONTROLLER_ALLOWED_HOSTS that .env already sets.
#
# A CONTROLLER_ALLOWED_HOSTS in .env (the one in .env.example is loopback only) replaces the overlay's wildcard
# default, so the forwarded name has to be added to it, or the controller answers "Invalid host header". An unset or
# empty one is left to that default. Run from postCreateCommand (via setup.sh) and again from postStartCommand: .env
# is gitignored, so a `cp .env.example .env` after the container was created is only seen by the second run.
set -euo pipefail

[[ -n "${CODESPACE_NAME:-}" && -f .env ]] || exit 0

controller_host="${CODESPACE_NAME}-8000.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"

# The copy of .env lives beside it (not in /tmp, and it holds every key in .env) and is moved into place, so an
# interrupted write cannot leave a truncated .env. It is removed on any exit.
hosts_tmp="$(mktemp .env.XXXXXX)"
trap 'rm -f "$hosts_tmp"' EXIT

# awk exits 0 when it edited the line, 3 when there was nothing to edit, 4 when the line is one it will not edit.
status=0
awk -v host="$controller_host" '
  {
    line = $0
    crlf = sub(/\r$/, "", line)
  }
  line ~ /^CONTROLLER_ALLOWED_HOSTS=./ {
    value = substr(line, index(line, "=") + 1)
    if (value ~ /^["\047]/ || value ~ /[ \t]#/) { skipped = 1 }
    else {
      n = split(value, names, ",")
      present = 0
      for (i = 1; i <= n; i++) if (names[i] == host) present = 1
      if (!present) { $0 = line "," host (crlf ? "\r" : ""); changed = 1 }
    }
  }
  { print }
  END { exit skipped ? 4 : (changed ? 0 : 3) }
' .env > "$hosts_tmp" || status=$?

case "$status" in
  0)
    chmod --reference=.env "$hosts_tmp"
    mv -f "$hosts_tmp" .env
    echo "Added this Codespace's forwarded host to CONTROLLER_ALLOWED_HOSTS in .env."
    ;;
  3) ;;
  4)
    echo "CONTROLLER_ALLOWED_HOSTS in .env is quoted or has a trailing comment, so it was not edited."
    echo "Add ${controller_host} to it yourself, unquoted and without the comment."
    ;;
  *) exit "$status" ;;
esac
