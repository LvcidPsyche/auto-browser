"""One answer to "is this API authenticated?", resolved once at startup.

The question used to have three answers that disagreed. `API_BEARER_TOKEN`
defaulted to unset; the bearer middleware returned early when it was unset, so
the *absence* of a token disabled authentication rather than denying requests;
and the only hard enforcement lived in `validate_runtime_policy`, which
downgraded to a warning and returned unless `APP_ENV=production` — while shipped
compose set `APP_ENV: ${APP_ENV:-development}`. Three layers, each failing open,
so the shipped default was an unauthenticated control plane driving a browser
that holds stored logins (GHSA-xmh3-cw7j-9gp5).

The switch is now reachability rather than an environment name, because
reachability is what decides whether an open control plane is a local
convenience or an account takeover. `APP_ENV` describes intent; a publish
mapping describes exposure.

The controller cannot observe its own reachability: inside Docker it always
binds `0.0.0.0`, and it is the host publish mapping — `127.0.0.1:8000:8000` in
the base compose — that makes it loopback-only. So the scope is *declared*, and
declared fail-safe: a deployment that says nothing is assumed reachable. Someone
hand-rolling a compose file that publishes on `0.0.0.0` gets a refusal to start
instead of a silent open control plane, and `docker compose up` on a clean
checkout still needs no token because the base compose declares `loopback`.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from typing import Any, Mapping

BIND_SCOPE_LOOPBACK = "loopback"
BIND_SCOPE_EXPOSED = "exposed"
BIND_SCOPES = (BIND_SCOPE_LOOPBACK, BIND_SCOPE_EXPOSED)

# Consulted only when API_BIND_SCOPE is not set explicitly, so that a bare
# `uvicorn --host 127.0.0.1` dev run stays frictionless without the developer
# having to learn a new variable.
BIND_HOST_ENV_VARS = ("API_BIND_HOST", "UVICORN_HOST", "HOST")


@dataclass(frozen=True, slots=True)
class AuthPolicy:
    """The resolved authentication stance for this process."""

    scope: str
    required: bool
    reason: str
    has_credential: bool = False

    @property
    def satisfiable(self) -> bool:
        """False when auth is required but no credential exists to satisfy it.

        Startup refuses this configuration, so it should be unreachable in a
        running server. The middleware checks anyway, because "the other layer
        already prevented it" is exactly the assumption that produced the
        original defect.
        """
        return not self.required or self.has_credential


def is_loopback_host(host: str) -> bool:
    candidate = host.strip()
    if candidate.startswith("["):  # [::1] or [::1]:8000
        candidate = candidate[1:].partition("]")[0]
    elif candidate.count(":") == 1:  # host:port — a bare IPv6 literal has more
        candidate = candidate.partition(":")[0]
    if not candidate:
        return False
    if candidate.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def resolve_bind_scope(settings: Any, environ: Mapping[str, str] | None = None) -> str:
    """Where this API is reachable from: an explicit declaration, else inferred.

    An explicit `API_BIND_SCOPE` always wins. Otherwise a loopback bind host in
    the environment counts as a loopback deployment, and anything else — an
    unknown or absent bind host — falls back to the fail-safe default.
    """
    env = os.environ if environ is None else environ
    declared = getattr(settings, "api_bind_scope", BIND_SCOPE_EXPOSED)

    if "api_bind_scope" in getattr(settings, "model_fields_set", ()):
        return declared

    for name in BIND_HOST_ENV_VARS:
        host = env.get(name)
        if host and is_loopback_host(host):
            return BIND_SCOPE_LOOPBACK

    return declared


def policy_for_scope(scope: str, token: str | None) -> AuthPolicy:
    """The decision itself, split out from where its inputs come from.

    Bind scope is fixed by the deployment and resolved once; the credential is
    read from live settings on each request. Both callers — the auth gate and
    the rate limiter — go through here, so "is this authenticated?" cannot drift
    between them again.
    """
    if token:
        return AuthPolicy(
            scope=scope,
            required=True,
            reason="API_BEARER_TOKEN is set",
            has_credential=True,
        )

    if scope == BIND_SCOPE_EXPOSED:
        return AuthPolicy(
            scope=scope,
            required=True,
            reason=(
                "the API is reachable off-box (API_BIND_SCOPE=exposed) and no API_BEARER_TOKEN "
                "is set, so no request can be authorised"
            ),
            has_credential=False,
        )

    return AuthPolicy(
        scope=scope,
        required=False,
        reason="loopback-only bind with no API_BEARER_TOKEN set",
        has_credential=False,
    )


def resolve_auth_policy(settings: Any, environ: Mapping[str, str] | None = None) -> AuthPolicy:
    return policy_for_scope(
        resolve_bind_scope(settings, environ),
        getattr(settings, "api_bearer_token", None),
    )
