#!/usr/bin/env bash
set -euo pipefail
umask 077

[[ "${1:-}" == "--apply" ]] || { echo "Use --apply" >&2; exit 64; }
AUTO_ROOT=/opt/auto-browser
HERMES_ROOT=/opt/stone/hermes-stack
NETWORK=hermes-browser-private

[[ -f "$AUTO_ROOT/.env" && -f "$HERMES_ROOT/docker-compose.yml" ]] || exit 1
docker compose -f "$HERMES_ROOT/docker-compose.yml" config --quiet
docker compose --env-file "$AUTO_ROOT/.env" -f "$AUTO_ROOT/docker-compose.yml" -f "$AUTO_ROOT/deploy/hetzner.compose.yml" config --quiet
docker network inspect "$NETWORK" >/dev/null
docker inspect hermes >/dev/null

# The token stays on the server and is never printed.
agent_token="$(python3 - "$AUTO_ROOT/.env" <<'PY'
import os, re, secrets, sys
from pathlib import Path
p = Path(sys.argv[1])
s = p.read_text()
m = re.search(r'(?m)^BROKER_AGENT_TOKENS=(.*)$', s)
if not m:
    raise SystemExit('BROKER_AGENT_TOKENS missing')
items = [x for x in m.group(1).split(',') if x]
hits = [x.split(':', 1)[1] for x in items if x.startswith('hermes:')]
if len(hits) > 1:
    raise SystemExit('duplicate Hermes token')
if hits:
    token = hits[0]
    if len(token) < 32:
        raise SystemExit('weak Hermes token')
else:
    token = secrets.token_urlsafe(36)
    items.append('hermes:' + token)
    next_text = s[:m.start(1)] + ','.join(items) + s[m.end(1):]
    temp = p.with_name('.env.hermes-tmp')
    temp.write_text(next_text)
    temp.chmod(0o600)
    os.replace(temp, p)
print(token, end='')
PY
)"

docker network connect "$NETWORK" hermes 2>/dev/null || true
cd "$AUTO_ROOT"
docker compose --env-file .env -f docker-compose.yml -f deploy/hetzner.compose.yml up -d --no-deps --force-recreate approval-broker >/dev/null

hermes_config() {
    docker exec -u hermes -e HERMES_HOME=/opt/data hermes /opt/hermes/.venv/bin/hermes config set --force "$@" >/dev/null
}
hermes_config mcp_servers.auto_browser.url http://approval-broker:18001/mcp
hermes_config mcp_servers.auto_browser.enabled true
hermes_config mcp_servers.auto_browser.trust full
hermes_config mcp_servers.auto_browser.tools.include '["browser.session_status","browser.request_access","browser.get_request","browser.complete","browser.observe","browser.click","browser.navigate","browser.press","browser.scroll","browser.type","browser.wait"]'
printf '%s\n' "$agent_token" | docker exec -i -u hermes -e HERMES_HOME=/opt/data hermes sh -c 'IFS= read -r token; /opt/hermes/.venv/bin/hermes config set --force mcp_servers.auto_browser.headers.Authorization "Bearer $token" >/dev/null'
unset agent_token
echo 'Hermes private MCP configured.'
