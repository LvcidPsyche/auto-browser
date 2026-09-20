#!/usr/bin/env bash
# Read-only check. It intentionally never prints bearer values.
set -euo pipefail
AUTO_ROOT=/opt/auto-browser
HERMES_ROOT=/opt/stone/hermes-stack
NETWORK=hermes-browser-private
bad=0
pass(){ echo "PASS $1"; }
fail(){ echo "FAIL $1" >&2; bad=$((bad+1)); }

if python3 - "$AUTO_ROOT/.env" <<'PY'
import re, sys
t=open(sys.argv[1], encoding='utf-8').read()
m=re.search(r'(?m)^BROKER_AGENT_TOKENS=(.*)$', t)
x=[] if not m else [v for v in m.group(1).split(',') if v.startswith('hermes:')]
raise SystemExit(0 if len(x)==1 and len(x[0].split(':',1)[1])>=32 else 1)
PY
then
  pass "one strong named Hermes bearer exists"
else
  fail "Hermes bearer absent, duplicate, or weak"
fi

broker="$(docker compose --env-file "$AUTO_ROOT/.env" -f "$AUTO_ROOT/docker-compose.yml" -f "$AUTO_ROOT/deploy/hetzner.compose.yml" ps -q approval-broker 2>/dev/null || true)"
hermes="$(docker compose -f "$HERMES_ROOT/docker-compose.yml" ps -q hermes 2>/dev/null || true)"
[[ -n "$broker" ]] && pass "approval broker is running" || fail "approval broker is not running"
[[ -n "$hermes" ]] && pass "Hermes is running" || fail "Hermes is not running"
docker network inspect "$NETWORK" --format '{{.Internal}}' 2>/dev/null | grep -qx true && pass "dedicated network is internal" || fail "dedicated internal network missing"

members="$(docker network inspect "$NETWORK" --format '{{json .Containers}}' 2>/dev/null || true)"
if [[ -n "$broker" && -n "$hermes" && "$members" == *"${broker:0:12}"* && "$members" == *"${hermes:0:12}"* ]]; then
  pass "broker and Hermes share dedicated network"
else
  fail "shared network membership missing"
fi

if [[ -n "$broker" ]]; then
  bind="$(docker port "$broker" 18001/tcp 2>/dev/null || true)"
  [[ -z "$bind" || "$bind" == 127.0.0.1:* || "$bind" == '[::1]:'* ]] && pass "broker has no public raw port" || fail "broker port is public"
fi

if [[ -n "$hermes" ]]; then
  docker exec "$hermes" getent hosts approval-broker >/dev/null 2>&1 && pass "Hermes resolves broker" || fail "Hermes cannot resolve broker"
  cfg_url="$(docker exec -u hermes -e HERMES_HOME=/opt/data "$hermes" /opt/hermes/.venv/bin/hermes config get mcp_servers.auto_browser.url 2>/dev/null || true)"
  cfg_enabled="$(docker exec -u hermes -e HERMES_HOME=/opt/data "$hermes" /opt/hermes/.venv/bin/hermes config get mcp_servers.auto_browser.enabled 2>/dev/null || true)"
  [[ "$cfg_url" == 'http://approval-broker:18001/mcp' && "$cfg_enabled" == true ]] && pass "Hermes MCP server is configured" || fail "Hermes MCP server configuration missing"
fi
[[ "$bad" -eq 0 ]]
