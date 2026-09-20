#!/usr/bin/env python3
"""Safe, non-secret verification of Hermes' private broker connection."""
import json
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

env_text = Path('/opt/auto-browser/.env').read_text()
match = re.search(r'(?m)^BROKER_AGENT_TOKENS=(.*)$', env_text)
assert match, 'No broker agent token list'
tokens = [x.split(':', 1)[1] for x in match.group(1).split(',') if x.startswith('hermes:')]
assert len(tokens) == 1 and len(tokens[0]) >= 32, 'Invalid Hermes token'

config = subprocess.run(
    ['docker', 'exec', '-u', 'hermes', '-e', 'HERMES_HOME=/opt/data', 'hermes',
     '/opt/hermes/.venv/bin/hermes', 'config', 'get',
     'mcp_servers.auto_browser.headers.Authorization'],
    capture_output=True, text=True, check=True,
).stdout.strip().strip('"\'')
assert config == 'Bearer ' + tokens[0], 'Hermes credential mismatch'
print('Hermes bearer: matched')

def call(payload):
    request = urllib.request.Request(
        'http://127.0.0.1:18001/mcp', data=json.dumps(payload).encode(),
        headers={'Authorization': 'Bearer ' + tokens[0],
                 'Content-Type': 'application/json', 'Accept': 'application/json'},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)

status, body = call({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                     'params': {'protocolVersion': '2025-03-26', 'capabilities': {},
                                'clientInfo': {'name': 'deployment-check', 'version': '1'}}})
assert status == 200 and body.get('result', {}).get('serverInfo', {}).get('name') == 'auto-browser-approval-broker'
print('MCP initialize: pass')
status, body = call({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}})
names = {x['name'] for x in body.get('result', {}).get('tools', [])}
assert status == 200 and {'browser.session_status', 'browser.request_access', 'browser.get_request', 'browser.navigate', 'browser.complete'} <= names
print('MCP tools: pass')
status, _ = call({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                  'params': {'name': 'browser.request_access',
                             'arguments': {'purpose': 'integration health check'}}})
assert status == 403, f'Expected closed owner session, received {status}'
print('No owner session: access correctly denied')
