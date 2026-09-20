#!/usr/bin/env python3
"""Create five *offline* tenant state roots; never starts or exposes services.

Run deliberately on the target Linux host only after approving capacity and
data location. Secrets are written with mode 0600 and never printed.
"""

import argparse
import base64
import os
import re
import secrets
import stat
from pathlib import Path

TENANTS = tuple(f"tenant{i:02d}" for i in range(1, 6))
HOST = re.compile(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")


def token() -> str:
    return secrets.token_urlsafe(36)


def key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def provision(root: Path, allowed_hosts: str) -> None:
    if not root.is_absolute():
        raise ValueError("State root must be an absolute path")
    if root.is_symlink():
        raise ValueError("State root must not be a symlink")
    repository = Path(__file__).resolve().parents[2]
    if root == repository or repository in root.parents:
        raise ValueError("Keep secret state outside the source repository")
    hosts = [host.strip() for host in allowed_hosts.split(",")]
    if not hosts or any(not HOST.fullmatch(host) or ".." in host or host == "*" for host in hosts):
        raise ValueError("Supply a comma-separated list of explicit website hostnames")
    if root.exists():
        if not root.is_dir() or stat.S_IMODE(root.stat().st_mode) & 0o077:
            raise ValueError("Existing state root must be a private directory (0700)")
    if any((root / tenant).exists() or (root / tenant).is_symlink() for tenant in TENANTS):
        raise FileExistsError("A tenant directory already exists; refusing to overwrite any tenant")

    old_mask = os.umask(0o077)
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        for tenant in TENANTS:
            home = root / tenant
            home.mkdir(mode=0o700)
            data = home / "data"
            data.mkdir(mode=0o700)
            for folder in ("browser-profile", "downloads", "auth", "db", "sessions"):
                (data / folder).mkdir(mode=0o700)
            values = {
                "COMPOSE_PROJECT_NAME": f"ab-{tenant}",
                "TENANT_DATA_ROOT": str(data),
                "TENANT_OPERATOR_ID": tenant,
                "TENANT_BEARER_TOKEN": token(),
                "TENANT_SHARE_SECRET": token(),
                "TENANT_FERNET_KEY": key(),
                "TENANT_ALLOWED_HOSTS": ",".join(hosts),
            }
            descriptor = os.open(home / ".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="ascii") as output:
                for name, value in values.items():
                    output.write(f"{name}={value}\n")
    finally:
        os.umask(old_mask)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--allowed-hosts", required=True)
    args = parser.parse_args()
    provision(args.state_root, args.allowed_hosts)
    print("Created five private tenant roots; no services were started and no secrets were printed.")


if __name__ == "__main__":
    main()
