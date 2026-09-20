"""Read private provisioner descriptors without accepting a client selector."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import time
from pathlib import Path

import httpx


def stack_key_for(user_id: str, tenant_id: str) -> str:
    digest = hashlib.sha256()
    for value in (user_id, tenant_id):
        if not isinstance(value, str) or not value or len(value) > 256:
            raise ValueError("Immutable identity is invalid")
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()[:32]


class TenantBrokerRegistry:
    """Resolve a broker URL and role credential from a private owned record."""

    def __init__(self, root: str | Path, role: str, *, clock=time.time):
        self.root = Path(root)
        if not self.root.is_absolute() or self.root.is_symlink() or not self.root.is_dir():
            raise ValueError("Tenant stack root must be an existing absolute directory")
        self.root = self.root.resolve(strict=True)
        if os.name == "posix" and stat.S_IMODE(self.root.stat().st_mode) & 0o077:
            raise ValueError("Tenant stack root must be private (0700)")
        if role not in {"portal", "gateway"}:
            raise ValueError("Unknown tenant broker role")
        self.role = role
        self.clock = clock
        self.clients: dict[str, httpx.AsyncClient] = {}

    def __call__(self, user_id: str, tenant_id: str) -> tuple[httpx.AsyncClient, str]:
        key = stack_key_for(user_id, tenant_id)
        path = self.root / key / "descriptor.json"
        if path.is_symlink() or not path.is_file() or path.resolve().parent != self.root / key:
            raise LookupError("Tenant browser stack is not provisioned")
        if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise LookupError("Tenant descriptor is not private")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise LookupError("Tenant descriptor is unavailable") from None
        alias = f"tenant-broker-{key}"
        expected = {
            "schema_version": 1,
            "user_id": user_id,
            "tenant_id": tenant_id,
            "stack_key": key,
            "compose_project": f"ab-{key}",
            "broker_control_alias": alias,
            "broker_mcp_url": f"http://{alias}:18001/mcp",
        }
        if not isinstance(value, dict) or any(value.get(name) != item for name, item in expected.items()):
            raise LookupError("Tenant descriptor ownership is invalid")
        self._touch_running_stack(key, user_id, tenant_id)
        token_name = "portal_owner_token" if self.role == "portal" else "gateway_agent_token"
        token = value.get(token_name)
        if not isinstance(token, str) or len(token) < 32:
            raise LookupError("Tenant broker credential is unavailable")
        client = self.clients.get(key)
        if client is None:
            client = httpx.AsyncClient(
                base_url=f"http://{alias}:18001", timeout=20, follow_redirects=False
            )
            self.clients[key] = client
        return client, token

    def _touch_running_stack(self, key: str, user_id: str, tenant_id: str) -> None:
        path = self.root / key / "metadata.json"
        if path.is_symlink() or not path.is_file() or path.resolve().parent != self.root / key:
            raise LookupError("Tenant lifecycle state is unavailable")
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise LookupError("Tenant lifecycle state is unavailable") from None
        expected = {"user_id": user_id, "tenant_id": tenant_id, "stack_key": key}
        if (
            not isinstance(metadata, dict)
            or any(metadata.get(name) != item for name, item in expected.items())
            or metadata.get("status") != "running"
        ):
            raise LookupError("Tenant browser stack is not running")
        metadata["last_activity"] = self.clock()
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(json.dumps(metadata, sort_keys=True) + "\n")
            os.replace(temporary, path)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise LookupError("Tenant lifecycle state could not be updated") from None

    async def aclose(self) -> None:
        for client in self.clients.values():
            await client.aclose()
        self.clients.clear()
