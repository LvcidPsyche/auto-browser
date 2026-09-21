"""Offline assertions for the unapplied public control-plane compose file."""

import re
from pathlib import Path


def require(text: str, value: str, message: str) -> None:
    assert value in text, message


def main() -> None:
    compose = Path(__file__).with_name("compose.yml").read_text(encoding="utf-8")
    # Comments intentionally name excluded services; inspect declarations only.
    effective = "\n".join(line for line in compose.splitlines() if not line.lstrip().startswith("#"))
    assert "ports:" not in effective, "Public control plane must not publish host ports"
    identity = effective.split("  portal:", 1)[0]
    assert "dokploy-network" not in identity, "Identity must not join dokploy-network"
    assert "networks: [control-plane]" in identity, "Identity must only join the private control-plane network"
    assert effective.count("traefik.enable=true") == 2, "Only portal and mcp-gateway may have Traefik labels"
    required_variables = {
        "IDENTITY_ADMIN_TOKEN", "IDENTITY_ENCRYPTION_KEY", "IDENTITY_HOST_STATE_ROOT",
        "IDENTITY_INTERNAL_TOKEN", "IDENTITY_RECOVERY_PEPPER", "IDENTITY_TRUSTED_PROXY_CIDRS",
        "MCP_GATEWAY_HOST_STATE_ROOT", "MCP_GATEWAY_INTERNAL_TOKEN", "MCP_GATEWAY_ISSUER_URL",
        "MCP_GATEWAY_PORTAL_URL", "MCP_GATEWAY_RESOURCE_URL", "PORTAL_ASSERTION_PRIVATE_KEY",
        "PORTAL_AUTHENTICATION_FRESHNESS_SECONDS", "PORTAL_HOST_STATE_ROOT",
        "PORTAL_PUBLIC_ORIGIN", "TENANT_CONTROL_NETWORK", "TENANT_STACK_HOST_ROOT",
    }
    failures = re.findall(r"\$\{([A-Z][A-Z0-9_]*):\?([^}]+)\}", effective)
    assert {name for name, message in failures if message.strip()} == required_variables, (
        "Every required public variable must have a clear Compose failure message"
    )
    for value, message in (
        ("Host(`secure-browser.fareeqk.com`)", "Portal host route is required"),
        ("!Path(`/healthz`)", "Portal health endpoint must stay off public ingress"),
        ("Host(`mcp-browser.fareeqk.com`)", "MCP host route is required"),
        ("letsencrypt-dns", "Wildcard DNS certificate resolver is required"),
        ("name: dokploy-network", "Existing dokploy overlay is required"),
        ("TENANT_STACK_ROOT: /tenant-stacks", "Portal and gateway need tenant stack registry access"),
        ("control-plane:\n    internal: true", "Private stack-local control-plane network is required"),
        ("tenant-control:\n    external: true\n    name: ${TENANT_CONTROL_NETWORK:?Set existing private tenant control network}", "Private tenant broker network is required"),
        ("--host, 0.0.0.0, --port, \"18003\"", "Identity private bind override is required"),
        ("stsSeconds=31536000", "HSTS security header is required"),
        ("redirectscheme.scheme=https", "HTTP to HTTPS redirect is required"),
        ("ratelimit.average", "Edge rate limits are required"),
    ):
        require(effective, value, message)
    assert effective.count("networks: [dokploy-network, control-plane, tenant-control]") == 2, "Portal and gateway require both private control networks and Dokploy ingress"
    allow = "Path(`/.well-known/oauth-authorization-server`) || Path(`/.well-known/oauth-protected-resource`) || Path(`/register`) || Path(`/authorize`) || Path(`/token`) || Path(`/mcp`)"
    assert effective.count(allow) == 2, "MCP Traefik routes must use the exact OAuth/MCP allow-list for HTTPS and HTTP redirect"
    for forbidden in ("approval-broker:", "controller:", "browser-node:", "noVNC", "VNC", "CDP"):
        assert forbidden not in effective, f"Public compose must not route or define {forbidden}"
    print("Offline public control-plane compose invariants passed.")


if __name__ == "__main__":
    main()
