#!/bin/sh
# What a command of Codex's model can reach beneath the controller's CLI sandbox, in the controller image built with
# INSTALL_AGENT_CLIS=true (the agent-cli-image workflow runs it there, as root, with this directory at /ci).
#
# Codex runs as the controller runs it: as uid 10002 with no new privileges and no capabilities, under the launcher
# with the grants cli_sandbox.launch_command gives (the CLI home, /tmp, /var/tmp, /dev), in a call directory inside the
# home, against codex_responses_mock.py, which asks it to run one probe. Each CODEX_SANDBOX_MODE setting must reach
# what docs/deployment.md ("Codex's own sandbox") says and no more: read-only reads but writes nothing and connects
# nowhere; workspace-write also writes /tmp and its working directory; danger-full-access and the bypass flag run the
# command with no sandbox of Codex's (the control: what read-only refuses there, Codex's sandbox refused). No setting
# reaches the rest of /data. Then Codex's own sandbox runs a command under the launcher, and an MCP server planted in
# the home's config.toml is disabled by /etc/codex/requirements.toml.
set -u
CI_DIR=$(dirname "$0")
H=/data/cli-home
CALL=$H/call
PORT=18080
AS_CLI="setpriv --reuid 10002 --regid 10002 --clear-groups --no-new-privs --inh-caps=-all"
LAUNCHER="python3 -I -S /app/app/cli_sandbox_launcher.py --protect /data --rw $H --rw /tmp --rw /var/tmp --rw /dev --"
LABELS='read /data/cli-home/.codex/auth.json
read /data/other/planted.txt
read /etc/hostname
write /data/cli-home/written.txt
write /data/cli-home/.codex/config.toml.new
write /tmp/written.txt
write /data/other/written.txt
write ./written-cwd.txt
connect'
PROBE='r() { if cat "$1" >/dev/null 2>&1; then echo "read $1: YES"; else echo "read $1: no"; fi; }
w() { if (echo x > "$1") 2>/dev/null; then echo "write $1: YES"; else echo "write $1: no"; fi; }
r /data/cli-home/.codex/auth.json; r /data/other/planted.txt; r /etc/hostname
w /data/cli-home/written.txt; w /data/cli-home/.codex/config.toml.new; w /tmp/written.txt; w /data/other/written.txt
w ./written-cwd.txt
if python3 -c "import socket; socket.create_connection((\"127.0.0.1\", '"$PORT"'), 3)" 2>/dev/null
then echo "connect: YES"; else echo "connect: no"; fi'

fail() { echo "FAIL: $*"; exit 1; }

# The probe's lines for one answer (YES or no) per label, in LABELS' order.
want() { printf '%s\n' "$LABELS" | while IFS= read -r label; do printf '%s: %s\n' "$label" "$1"; shift; done; }

# What the probe printed when Codex ran it under one setting (its flags, split on purpose).
probe_under() {
  log=/tmp/mock.log
  : > "$log"
  python3 "$CI_DIR/codex_responses_mock.py" "$PORT" "$log" "$PROBE" &
  mock=$!
  i=0
  until python3 -c "import socket; socket.create_connection(('127.0.0.1', $PORT), 1)" 2>/dev/null; do
    i=$((i + 1)); [ $i -lt 50 ] || fail "the mock did not start"; sleep 0.1
  done
  rm -f "$H/written.txt" "$H/.codex/config.toml.new" /tmp/written.txt /data/other/written.txt "$CALL/written-cwd.txt"
  # shellcheck disable=SC2086 # the launcher's command line and the setting's flags are split on purpose
  (cd "$CALL" && HOME=$H CODEX_HOME=$H/.codex timeout 120 $AS_CLI $LAUNCHER \
    codex exec --skip-git-repo-check --ephemeral --cd "$CALL" $1 probe) > /tmp/exec.out 2>&1 < /dev/null
  kill "$mock" 2>/dev/null
  wait "$mock" 2>/dev/null
  grep -E '^(read|write|connect)' "$log"
}

# One setting, then the answers the docs give for it.
check() {
  setting=$1
  shift
  got=$(probe_under "$setting")
  expected=$(want "$@")
  echo "=== $setting"
  echo "$got"
  if [ "$got" != "$expected" ]; then
    printf -- '--- expected\n%s\n--- codex exec\n' "$expected"
    tail -n 15 /tmp/exec.out
    fail "$setting: a command of the model's did not reach what the docs say, or reached more"
  fi
}

mkdir -p "$H/.codex" "$CALL" /data/other
printf '{"OPENAI_API_KEY": "SIGNIN-SECRET"}\n' > "$H/.codex/auth.json"
echo DATA-SECRET > /data/other/planted.txt
printf '%s\n' 'model = "mock-model"' 'model_provider = "mock"' '[model_providers.mock]' 'name = "mock"' \
  "base_url = \"http://127.0.0.1:$PORT/v1\"" 'wire_api = "responses"' > "$H/.codex/config.toml"
chown -R 10002:10002 /data
codex --version || fail "no codex"

#                                     auth  /data /etc  home  conf  /tmp  /data cwd  connect
check "-s read-only"                  YES   no    YES   no    no    no    no    no   no
check "-s workspace-write"            YES   no    YES   no    no    YES   no    YES  no
check "-s danger-full-access"         YES   no    YES   YES   YES   YES   no    YES  YES
check "--dangerously-bypass-approvals-and-sandbox" \
                                      YES   no    YES   YES   YES   YES   no    YES  YES

echo "=== codex sandbox linux, under the launcher"
# shellcheck disable=SC2086
out=$(cd "$CALL" && HOME=$H $AS_CLI $LAUNCHER codex sandbox linux -- sh -c "echo nested-ok" 2>&1 < /dev/null)
status=$?
echo "$out"
[ "$status" -eq 0 ] && [ "$out" = nested-ok ] || fail "Codex's sandbox under the launcher: exit $status"

echo "=== an MCP server planted in the home's config.toml"
printf '%s\n' '[mcp_servers.planted]' 'command = "/bin/true"' >> "$H/.codex/config.toml"
listed=$(HOME=$H CODEX_HOME=$H/.codex $AS_CLI codex mcp list 2>&1 < /dev/null)
echo "$listed"
echo "$listed" | grep planted | grep -q "disabled: requirements" || fail "the planted MCP server is not disabled"
echo "=== all held"
