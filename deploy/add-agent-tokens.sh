#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077

python3 - <<'PY'
import base64
import os
from pathlib import Path

path = Path('.env')
if not path.is_file():
    raise SystemExit('Protected .env is missing')
contents = path.read_text()
if 'BROKER_AGENT_TOKENS=' in contents:
    print('Named agent credentials already exist; nothing changed.')
    raise SystemExit(0)

def token() -> str:
    return base64.urlsafe_b64encode(os.urandom(36)).decode().rstrip('=')

entry = ','.join(f'{name}:{token()}' for name in (
    'assistant_one', 'assistant_two', 'assistant_three',
))
with path.open('a') as output:
    output.write(f'\nBROKER_AGENT_TOKENS={entry}\n')
os.chmod(path, 0o600)
print('Added three protected named agent credentials; values were not printed.')
PY

docker compose -f docker-compose.yml -f deploy/hetzner.compose.yml config --quiet
