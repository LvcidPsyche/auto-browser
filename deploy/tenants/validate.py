#!/usr/bin/env python3
"""Fast, offline invariants for the five-tenant pilot template."""

import base64
import tempfile
from pathlib import Path

from provision import TENANTS, provision


def parse_env(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


def main() -> None:
    compose = Path(__file__).with_name("compose.yml").read_text()
    assert "ports:" not in compose and "network_mode:" not in compose
    assert "profiles: [pilot]" in compose
    assert "SESSION_ISOLATION_MODE: shared_browser_node" in compose
    assert "cap_drop: [ALL]" in compose
    assert "mem_limit:" in compose and "memswap_limit:" in compose
    assert "API_BIND_SCOPE: exposed" in compose
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private"
        provision(root, "example.com")
        envs = [parse_env(root / tenant / ".env") for tenant in TENANTS]
        for name in ("COMPOSE_PROJECT_NAME", "TENANT_DATA_ROOT", "TENANT_BEARER_TOKEN", "TENANT_SHARE_SECRET", "TENANT_FERNET_KEY"):
            assert len({env[name] for env in envs}) == 5, name
        for tenant, env in zip(TENANTS, envs):
            assert env["COMPOSE_PROJECT_NAME"] == f"ab-{tenant}"
            assert Path(env["TENANT_DATA_ROOT"]).parent == root / tenant
            assert len(base64.urlsafe_b64decode(env["TENANT_FERNET_KEY"])) == 32
            assert (root / tenant / "data" / "browser-profile").is_dir()
        try:
            provision(root, "example.com")
        except (FileExistsError, ValueError):
            pass
        else:
            raise AssertionError("Provisioning must never overwrite existing state")
    print("Offline tenant invariants passed; Docker runtime and capacity not verified.")


if __name__ == "__main__":
    main()
