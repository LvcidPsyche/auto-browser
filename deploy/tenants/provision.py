#!/usr/bin/env python3
"""Trusted-host command line for one enrolled tenant stack.

The caller is responsible for authenticating enrollment and supplying immutable
``user_id`` and ``tenant_id`` values. This program deliberately does not accept
browser, controller, or Docker names from a request.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from provisioner import ProvisioningConfig, TenantEnrollment, TenantProvisioner, stack_key_for

from tenant_stacks.policy import normalize_hostname


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "provision",
            "idle",
            "deprovision",
            "reap-idle",
            "rotate-credentials",
            "update-allowed-hosts",
            "allow-host",
            "deny-host",
        ),
    )
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--user-id")
    parser.add_argument("--tenant-id")
    parser.add_argument("--allowed-hosts")
    parser.add_argument("--hostname")
    parser.add_argument("--expected-revision", type=int)
    parser.add_argument("--max-running", type=int, default=1)
    parser.add_argument("--idle-timeout-seconds", type=int, default=900)
    parser.add_argument("--portal-assertion-public-key", required=True)
    args = parser.parse_args()

    provisioner = TenantProvisioner(
        ProvisioningConfig(
            state_root=args.state_root,
            allowed_hosts=args.allowed_hosts or "",
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
        if not args.allowed_hosts:
            parser.error("--allowed-hosts is required for provisioning")
        provisioner.provision(enrollment)
    elif args.action == "idle":
        provisioner.stop_for_idle(enrollment)
    elif args.action == "deprovision":
        provisioner.deprovision(enrollment)
    elif args.action == "update-allowed-hosts":
        if not args.allowed_hosts:
            parser.error("--allowed-hosts is required when updating the allow-list")
        provisioner.update_allowed_hosts(
            enrollment, args.allowed_hosts, expected_revision=args.expected_revision
        )
    elif args.action in {"allow-host", "deny-host"}:
        if not args.hostname:
            parser.error("--hostname is required when changing one allowed hostname")
        descriptor = provisioner.lookup(enrollment)
        current = descriptor.allowed_hosts
        if not current:
            current = provisioner._current_allowed_hosts(provisioner._home(stack_key_for(enrollment)), descriptor)
        hostname = normalize_hostname(args.hostname)
        values = (*current, hostname) if args.action == "allow-host" else tuple(host for host in current if host != hostname)
        provisioner.update_allowed_hosts(
            enrollment,
            values,
            expected_revision=descriptor.policy_revision if args.expected_revision is None else args.expected_revision,
        )
    else:
        provisioner.rotate_credentials(enrollment)
    print(f"Tenant stack {args.action} completed without printing credentials.")


if __name__ == "__main__":
    main()
