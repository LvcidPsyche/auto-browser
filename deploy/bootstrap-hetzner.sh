#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
umask 077

if [[ -e .env ]]; then
  echo 'Refusing to replace existing .env' >&2
  exit 1
fi

python3 - <<'PY'
import base64
import os
from pathlib import Path

template = Path('deploy/hetzner.env.example').read_text()
secrets = {
    'CHANGE_ME_ROOT_TOKEN': base64.urlsafe_b64encode(os.urandom(36)).decode().rstrip('='),
    'CHANGE_ME_OWNER_TOKEN': base64.urlsafe_b64encode(os.urandom(36)).decode().rstrip('='),
    'CHANGE_ME_BROKER_OWNER_TOKEN': base64.urlsafe_b64encode(os.urandom(36)).decode().rstrip('='),
    'CHANGE_ME_AGENT_ONE_TOKEN': base64.urlsafe_b64encode(os.urandom(36)).decode().rstrip('='),
    'CHANGE_ME_AGENT_TWO_TOKEN': base64.urlsafe_b64encode(os.urandom(36)).decode().rstrip('='),
    'CHANGE_ME_AGENT_THREE_TOKEN': base64.urlsafe_b64encode(os.urandom(36)).decode().rstrip('='),
    'CHANGE_ME_SHARE_SECRET': base64.urlsafe_b64encode(os.urandom(36)).decode().rstrip('='),
    'CHANGE_ME_FERNET_KEY': base64.urlsafe_b64encode(os.urandom(32)).decode(),
}
for placeholder, secret in secrets.items():
    template = template.replace(placeholder, secret)
destination = Path('.env')
with destination.open('x') as output:
    output.write(template)
os.chmod(destination, 0o600)
print('Created protected .env; credentials were not printed.')
PY

mkdir -p data
chmod 700 data
docker compose -f docker-compose.yml -f deploy/hetzner.compose.yml config --quiet
echo 'Compose configuration validated.'
