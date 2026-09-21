#!/usr/bin/env python3
"""Private host administration for the public identity service."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / "hetzner.env"
COMPOSE_FILE = ROOT / "compose.yml"

CREATE_INVITATION = r'''
import json, os, sys, urllib.request
origin, name, tenant, ttl = sys.argv[1:]
payload = {"intended_display_name": name}
if tenant:
    payload["tenant_id"] = tenant
if ttl:
    payload["expires_in_seconds"] = int(ttl)
request = urllib.request.Request(
    "http://127.0.0.1:18003/admin/invitations",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Authorization": "Bearer " + os.environ["IDENTITY_ADMIN_TOKEN"], "Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=10) as response:
    value = json.load(response)
print(origin.rstrip("/") + "/invite/" + value["invitation_token"])
'''

LOOK_UP_IDENTITY = r'''
import json, os, sqlite3, sys
name = sys.argv[1]
with sqlite3.connect(os.path.join(os.environ["IDENTITY_STATE_ROOT"], "identity.sqlite3")) as db:
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT user_id,tenant_id FROM users WHERE display_name=? COLLATE NOCASE AND reenrollment_pending=0",
        (name,),
    ).fetchone()
if row is None:
    raise SystemExit(4)
print(json.dumps(dict(row), separators=(",", ":")))
'''


def stack_key(user_id: str, tenant_id: str) -> str:
    digest = hashlib.sha256()
    for value in (user_id, tenant_id):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()[:32]


def make_service_owned(path: Path) -> None:
    """Hand one validated tenant descriptor tree to the unprivileged services."""
    if path.is_symlink() or not path.is_dir():
        raise SystemExit("Provisioned tenant state path is missing or unsafe")
    for root, directories, files in os.walk(path, topdown=True, followlinks=False):
        root_path = Path(root)
        if any((root_path / name).is_symlink() for name in (*directories, *files)):
            raise SystemExit("Refusing symlink in provisioned tenant state")
        os.chown(root_path, 10001, 10001)
        os.chmod(root_path, 0o700)
        for name in files:
            item = root_path / name
            os.chown(item, 10001, 10001)
            os.chmod(item, 0o600)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    invite = sub.add_parser("invite", help="create an identity invitation")
    invite.add_argument("--name", required=True)
    invite.add_argument("--tenant-id")
    invite.add_argument("--expires-in-seconds", type=int)
    provision = sub.add_parser("provision", help="provision the enrolled identity's private browser stack")
    provision.add_argument("--name", required=True)
    provision.add_argument("--allowed-hosts", required=True)
    provision.add_argument("--max-running", type=int, default=1)
    provision.add_argument("--idle-timeout-seconds", type=int, default=900)
    for action, help_text in (
        ("allow", "allow one hostname for an enrolled identity's browser stack"),
        ("deny", "remove one hostname from an enrolled identity's browser stack"),
    ):
        policy = sub.add_parser(action, help=help_text)
        policy.add_argument("--name", required=True)
        policy.add_argument("--hostname", required=True)
        policy.add_argument("--expected-revision", type=int)
    args = parser.parse_args()

    if os.geteuid() != 0:
        parser.error("run as root so Docker can read the protected environment")
    if not ENV_FILE.is_file() or ENV_FILE.is_symlink() or not COMPOSE_FILE.is_file():
        parser.error("protected environment or compose file is missing")
    env_mode = ENV_FILE.stat().st_mode
    if ENV_FILE.stat().st_uid != 0 or stat.S_IMODE(env_mode) != 0o600:
        parser.error("hetzner.env must be root-owned with mode 0600")
    if getattr(args, "expires_in_seconds", None) is not None and not 60 <= args.expires_in_seconds <= 2592000:
        parser.error("--expires-in-seconds must be between 60 and 2592000")

    values = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    origin = values.get("PORTAL_PUBLIC_ORIGIN", "")
    if not origin.startswith("https://"):
        parser.error("PORTAL_PUBLIC_ORIGIN is missing or invalid")

    compose_exec = [
        "docker", "compose", "--env-file", str(ENV_FILE), "-f", str(COMPOSE_FILE),
        "exec", "-T", "identity", "python", "-c",
    ]
    if args.command == "invite":
        command = [
            *compose_exec, CREATE_INVITATION, origin, args.name, args.tenant_id or "",
            str(args.expires_in_seconds or ""),
        ]
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        if result.returncode:
            sys.stderr.write("Invitation creation failed. Check the private service logs.\n")
            raise SystemExit(result.returncode)
        link = result.stdout.strip()
        if not link.startswith(origin.rstrip("/") + "/invite/") or "\n" in link:
            raise SystemExit("Identity service returned an invalid invitation result")
        print(link)
        return

    max_running = getattr(args, "max_running", 1)
    idle_timeout_seconds = getattr(args, "idle_timeout_seconds", 900)
    if args.command == "provision":
        if max_running < 1 or idle_timeout_seconds < 1:
            parser.error("provisioning limits must be positive")
        if not args.allowed_hosts.strip() or "\n" in args.allowed_hosts or "\r" in args.allowed_hosts:
            parser.error("--allowed-hosts must be a non-empty single-line value")
    elif args.expected_revision is not None and args.expected_revision < 0:
        parser.error("--expected-revision must be non-negative")
    lookup = subprocess.run(
        [*compose_exec, LOOK_UP_IDENTITY, args.name],
        check=False,
        capture_output=True,
        text=True,
    )
    if lookup.returncode:
        raise SystemExit("Enrolled identity was not found")
    try:
        identity = json.loads(lookup.stdout)
        user_id, tenant_id = identity["user_id"], identity["tenant_id"]
    except (json.JSONDecodeError, KeyError, TypeError):
        raise SystemExit("Identity lookup returned an invalid result") from None
    state_root = Path(values.get("TENANT_STACK_HOST_ROOT", ""))
    public_key = values.get("BROKER_PORTAL_ASSERTION_PUBLIC_KEY", "")
    if not state_root.is_absolute() or not public_key:
        raise SystemExit("Tenant provisioning configuration is missing")
    provisioner = ROOT.parent / "tenants" / "provision.py"
    provision_action = {"provision": "provision", "allow": "allow-host", "deny": "deny-host"}[args.command]
    command = [
        sys.executable, str(provisioner), provision_action, "--state-root", str(state_root),
        "--user-id", user_id, "--tenant-id", tenant_id,
        "--max-running", str(max_running), "--idle-timeout-seconds", str(idle_timeout_seconds),
        "--portal-assertion-public-key", public_key,
    ]
    if args.command == "provision":
        command.extend(("--allowed-hosts", args.allowed_hosts))
    else:
        command.extend(("--hostname", args.hostname))
        if args.expected_revision is not None:
            command.extend(("--expected-revision", str(args.expected_revision)))
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode:
        sys.stderr.write("Tenant provisioning failed. Check private host logs.\n")
        raise SystemExit(result.returncode)
    root = state_root.resolve(strict=True)
    home = root / stack_key(user_id, tenant_id)
    if home.resolve(strict=True).parent != root:
        raise SystemExit("Provisioned tenant state escaped its configured root")
    make_service_owned(home)
    if args.command == "provision":
        print("Tenant browser stack provisioned without printing identifiers or credentials.")
    else:
        print("Tenant browser hostname policy updated without printing identifiers or credentials.")


if __name__ == "__main__":
    main()
