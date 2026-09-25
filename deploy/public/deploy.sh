#!/usr/bin/env bash
# Start only the public control plane after proving the rendered Compose plan is safe.
set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly ENV_FILE="$SCRIPT_DIR/hetzner.env"
readonly COMPOSE_FILE="$SCRIPT_DIR/compose.yml"

if [[ ${EUID} -ne 0 ]]; then
  echo 'Run as root so Docker reads the protected host environment.' >&2
  exit 1
fi
[[ -f "$ENV_FILE" ]] || { echo 'Missing protected hetzner.env; run bootstrap.sh first.' >&2; exit 1; }
[[ -f "$COMPOSE_FILE" ]] || { echo 'Missing public compose.yml.' >&2; exit 1; }
[[ ! -L "$ENV_FILE" ]] || { echo 'Refusing symlinked hetzner.env.' >&2; exit 1; }
[[ $(stat -c '%U:%G:%a' "$ENV_FILE") == root:root:600 ]] || {
  echo 'hetzner.env must be root-owned with mode 0600.' >&2; exit 1;
}

required=(
  PORTAL_PUBLIC_ORIGIN MCP_GATEWAY_ISSUER_URL MCP_GATEWAY_RESOURCE_URL MCP_GATEWAY_PORTAL_URL
  IDENTITY_HOST_STATE_ROOT PORTAL_HOST_STATE_ROOT MCP_GATEWAY_HOST_STATE_ROOT TENANT_STACK_HOST_ROOT
  TENANT_CONTROL_NETWORK IDENTITY_TRUSTED_PROXY_CIDRS PORTAL_AUTHENTICATION_FRESHNESS_SECONDS
  IDENTITY_ADMIN_TOKEN IDENTITY_INTERNAL_TOKEN
  IDENTITY_ENCRYPTION_KEY IDENTITY_RECOVERY_PEPPER MCP_GATEWAY_INTERNAL_TOKEN
  TENANT_POLICY_INTERNAL_TOKEN PORTAL_ASSERTION_PRIVATE_KEY BROKER_PORTAL_ASSERTION_PUBLIC_KEY
)
for name in "${required[@]}"; do
  value="$(sed -n "s/^${name}=//p" "$ENV_FILE")"
  if [[ -z "$value" || "$value" == *CHANGE_ME* || "$value" == *PLACEHOLDER* || "$value" == *example* ]]; then
    echo "Missing or placeholder value for $name." >&2
    exit 1
  fi
done

python3 "$SCRIPT_DIR/../tenants/validate.py"
python3 "$SCRIPT_DIR/validate.py"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" config --quiet

rendered="$(mktemp)"
trap 'rm -f -- "$rendered"' EXIT HUP INT TERM
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" config --format json >"$rendered"

# Control-plane services are reached through Traefik only where explicitly designed.
# The broker and controller must never have a published port or Traefik routing label.
python3 - "$rendered" <<'PY'
import json
import sys
from pathlib import Path

doc = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
services = doc.get("services") or {}
if not services:
    raise SystemExit("Rendered compose has no services")
for name, spec in services.items():
    if spec.get("ports"):
        raise SystemExit(f"Refusing rendered published ports on service: {name}")
    labels = spec.get("labels") or {}
    if isinstance(labels, dict):
        normalized = "\n".join(f"{key}={value}".lower() for key, value in labels.items())
    else:
        normalized = "\n".join(str(value).lower() for value in labels)
    if "traefik." in normalized and name not in {"portal", "mcp-gateway"}:
        raise SystemExit(f"Refusing Traefik labels on non-public service: {name}")
    if name in {"approval-broker", "broker", "controller"} and "traefik.http.routers." in normalized:
        raise SystemExit(f"Refusing Traefik routability for private service: {name}")
PY

# The explicit list prevents an accidental profile or tenant workload from starting.
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d --build identity tenant-policy portal mcp-gateway
