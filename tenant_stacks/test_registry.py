from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tenant_stacks.registry import TenantBrokerRegistry, stack_key_for


def descriptor(root: Path, user: str, tenant: str, **changes) -> Path:
    key = stack_key_for(user, tenant)
    home = root / key
    home.mkdir()
    value = {
        "schema_version": 1,
        "user_id": user,
        "tenant_id": tenant,
        "stack_key": key,
        "compose_project": f"ab-{key}",
        "broker_control_alias": f"tenant-broker-{key}",
        "broker_mcp_url": f"http://tenant-broker-{key}:18001/mcp",
        "portal_owner_token": (f"portal-{user}-" + "p" * 48)[:48],
        "gateway_agent_token": (f"gateway-{user}-" + "g" * 48)[:48],
    }
    value.update(changes)
    path = home / "descriptor.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    (home / "metadata.json").write_text(json.dumps({
        "schema_version": 1, "user_id": user, "tenant_id": tenant,
        "stack_key": key, "status": "running", "last_activity": 1.0,
    }), encoding="utf-8")
    return path


def test_two_identities_resolve_distinct_private_routes_and_credentials(tmp_path: Path) -> None:
    root = tmp_path / "stacks"
    root.mkdir()
    descriptor(root, "user-a", "tenant")
    descriptor(root, "user-b", "tenant")
    portal, gateway = TenantBrokerRegistry(root, "portal"), TenantBrokerRegistry(root, "gateway")
    client_a, owner_a = portal("user-a", "tenant")
    client_b, owner_b = portal("user-b", "tenant")
    _, agent_a = gateway("user-a", "tenant")
    _, agent_b = gateway("user-b", "tenant")
    assert client_a.base_url != client_b.base_url
    assert len({owner_a, owner_b, agent_a, agent_b}) == 4
    touched = json.loads((root / stack_key_for("user-a", "tenant") / "metadata.json").read_text())
    assert touched["last_activity"] > 1.0
    asyncio.run(portal.aclose())
    asyncio.run(gateway.aclose())


def test_guessed_identity_cannot_reuse_or_relabel_descriptor(tmp_path: Path) -> None:
    root = tmp_path / "stacks"
    root.mkdir()
    path = descriptor(root, "user-a", "tenant")
    registry = TenantBrokerRegistry(root, "gateway")
    with pytest.raises(LookupError):
        registry("user-b", "tenant")
    value = json.loads(path.read_text())
    value["user_id"] = "user-b"
    path.write_text(json.dumps(value))
    with pytest.raises(LookupError, match="ownership"):
        registry("user-a", "tenant")
