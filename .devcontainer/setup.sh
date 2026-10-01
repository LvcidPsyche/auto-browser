#!/usr/bin/env bash
set -euo pipefail

echo "Installing Python dev dependencies..."
pip install --quiet -e ./controller[dev]

# The Codespaces overlay publishes the API on 0.0.0.0, so it refuses to start
# without API_BEARER_TOKEN — and nothing created one, so a fresh Codespace never
# came up. Generate one into .env (gitignored) the first time, along with the
# forwarded noVNC host name that takeover has to accept.
if [[ -n "${CODESPACE_NAME:-}" ]]; then
  touch .env
  # Appending to a file without a final newline would join two settings.
  if [[ -s .env && -n "$(tail -c1 .env)" ]]; then
    echo >> .env
  fi
  if ! grep -q '^API_BEARER_TOKEN=.' .env; then
    printf 'API_BEARER_TOKEN=%s\n' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" >> .env
    echo "Generated API_BEARER_TOKEN in .env; the API and the dashboard ask for it."
  fi
  if [[ -n "${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-}" ]] && ! grep -q '^NOVNC_ALLOWED_HOSTS=.' .env; then
    printf 'NOVNC_ALLOWED_HOSTS=%s-6080.%s\n' "$CODESPACE_NAME" "$GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN" >> .env
  fi
fi

echo "Pulling Docker images..."
docker compose pull --quiet 2>/dev/null || true

echo "Setup complete. auto-browser will start automatically."
