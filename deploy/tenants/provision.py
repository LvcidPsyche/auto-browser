#!/usr/bin/env python3
"""Trusted-host command line for one enrolled tenant stack.

The caller is responsible for authenticating enrollment and supplying immutable
``user_id`` and ``tenant_id`` values. This program deliberately does not accept
browser, controller, or Docker names from a request.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from provisioner import ProvisioningConfig, TenantEnrollment, TenantProvisioner


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("provision", "idle", "deprovision", "reap-idle", "rotate-credentials"))
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--user-id")
    parser.add_argument("--tenant-id")
    parser.add_argument("--allowed-hosts", required=True)
    parser.add_argument("--max-running", type=int, default=1)
    parser.add_argument("--idle-timeout-seconds", type=int, default=900)
    parser.add_argument("--portal-assertion-public-key", required=True)
    args = parser.parse_args()

    provisioner = TenantProvisioner(
        ProvisioningConfig(
            state_root=args.state_root,
            allowed_hosts=args.allowed_hosts,
            max_running=args.max_running,
            portal_assertion_public_key=args.portal_assertion_public_key,
            idle_timeout_seconds=args.idle_timeout_seconds,
        )
    )
    if args.action == "reap-idle":
        provisioner.reap_idle()
        print("Idle tenant stacks were reaped without printing credentials.")
        return
    if not args.user_id or not args.tenant_id:
        parser.error("--user-id and --tenant-id are required for this action")
    enrollment = TenantEnrollment(user_id=args.user_id, tenant_id=args.tenant_id)
    if args.action == "provision":
        provisioner.provision(enrollment)
    elif args.action == "idle":
        provisioner.stop_for_idle(enrollment)
    elif args.action == "deprovision":
        provisioner.deprovision(enrollment)
    else:
        provisioner.rotate_credentials(enrollment)
    print(f"Tenant stack {args.action} completed without printing credentials.")


if __name__ == "__main__":
    main()
