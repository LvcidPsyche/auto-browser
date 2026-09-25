"""Trusted read-only routing to identity-bound tenant browser stacks."""

from .policy import MAX_ALLOWED_HOSTS, normalize_hostname, normalize_hostnames
from .registry import TenantBrokerRegistry, TenantPolicyRegistry, stack_key_for

__all__ = [
    "MAX_ALLOWED_HOSTS",
    "TenantBrokerRegistry",
    "TenantPolicyRegistry",
    "normalize_hostname",
    "normalize_hostnames",
    "stack_key_for",
]
