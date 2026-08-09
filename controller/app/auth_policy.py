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

import hmac
import ipaddress
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Mapping, Sequence

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


@dataclass(frozen=True, slots=True)
class Credential:
    """A bearer token, and the operator it proves the holder to be — if any.

    `operator_id is None` means the shared `API_BEARER_TOKEN`: it proves the
    caller is authorised, and nothing at all about who they are. Named tokens
    from `API_BEARER_TOKENS` prove both, which is the difference between
    attribution and authentication.
    """

    token: str
    operator_id: str | None = None

    @property
    def verifies_identity(self) -> bool:
        return self.operator_id is not None


def parse_operator_tokens(raw: str) -> tuple[Credential, ...]:
    """Parse `alice:token-a,bob:token-b` into named credentials.

    Split on the first colon only — a token may contain colons, an operator id
    may not. Entries without a colon are skipped rather than guessed at: a
    malformed pair that silently became a shared token would hand one operator's
    identity to everyone holding it.
    """
    credentials: list[Credential] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        operator_id, _, token = entry.partition(":")
        operator_id, token = operator_id.strip(), token.strip()
        if operator_id and token:
            credentials.append(Credential(token=token, operator_id=operator_id))
    return tuple(credentials)


@lru_cache(maxsize=16)
def _credentials(named: str, shared: str | None) -> tuple[Credential, ...]:
    credentials = list(parse_operator_tokens(named))
    if shared:
        credentials.append(Credential(token=shared))
    return tuple(credentials)


def credentials_for(settings: Any) -> tuple[Credential, ...]:
    return _credentials(
        getattr(settings, "api_bearer_tokens", "") or "",
        getattr(settings, "api_bearer_token", None),
    )


def match_credential(credentials: Sequence[Credential], authorization: str) -> Credential | None:
    """The credential this request presented, or None.

    Every candidate is compared even after a match, so the work does not depend
    on which token was presented or how many are configured.
    """
    presented = authorization.encode()
    matched: Credential | None = None
    for credential in credentials:
        expected = f"Bearer {credential.token}".encode()
        if hmac.compare_digest(presented, expected) and matched is None:
            matched = credential
    return matched


def policy_for_scope(scope: str, credentials: Sequence[Credential]) -> AuthPolicy:
    """The decision itself, split out from where its inputs come from.

    Bind scope is fixed by the deployment and resolved once; credentials are
    read from live settings on each request. Both callers — the auth gate and
    the rate limiter — go through here, so "is this authenticated?" cannot drift
    between them again.
    """
    if credentials:
        return AuthPolicy(
            scope=scope,
            required=True,
            reason="at least one bearer credential is configured",
            has_credential=True,
        )

    if scope == BIND_SCOPE_EXPOSED:
        return AuthPolicy(
            scope=scope,
            required=True,
            reason=(
                "the API is reachable off-box (API_BIND_SCOPE=exposed) and no bearer credential "
                "is configured, so no request can be authorised"
            ),
            has_credential=False,
        )

    return AuthPolicy(
        scope=scope,
        required=False,
        reason="loopback-only bind with no bearer credential configured",
        has_credential=False,
    )


def resolve_auth_policy(settings: Any, environ: Mapping[str, str] | None = None) -> AuthPolicy:
    return policy_for_scope(resolve_bind_scope(settings, environ), credentials_for(settings))
