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
instead of a silent open control plane.

Loopback is not a reason to go without a token either. The publish mapping
decides who on the host can reach port 8000, not who on the compose network
can: browser-node, and anything else beside the controller, reached a tokenless
API by sending `Host: localhost`. So a loopback controller with no configured
credential generates one at startup and demands it (app/api_token.py), and
`docker compose up` on a clean checkout writes it to ./data/api-token.
API_ALLOW_TOKENLESS_LOOPBACK=true keeps the old tokenless loopback API.
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
        candidate, closed, rest = candidate[1:].partition("]")
        # Anything else after the bracket ("[::1]evil.com") is not a loopback host.
        if not closed or (rest and not (rest.startswith(":") and rest[1:].isdigit())):
            return False
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


def is_local_only_host(host: str) -> bool:
    """Whether a bare host name (a URL's hostname, no port) names only this machine, or no host at all.

    The empty host, ``localhost`` and ``*.localhost``, and any loopback or unspecified address, an IPv4-mapped IPv6
    one included (127.0.0.2, ::ffff:127.0.0.1, 0.0.0.0). It judges what an address is, not whether it may be bound
    to: 0.0.0.0 is local-only as a URL to open and public as an address to listen on, so a bind check uses
    ``is_loopback_host``.
    """
    candidate = host.strip().lower()
    if not candidate or candidate == "localhost" or candidate.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback or address.is_unspecified


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


def tokenless_loopback_allowed(settings: Any) -> bool:
    """Whether API_ALLOW_TOKENLESS_LOOPBACK is on. Read per request, so a patched setting applies.

    `is True`, not truthiness: tests pass MagicMock settings, whose every attribute
    is truthy, and a stand-in without the attribute must fall to "required".
    Never in production, whatever the setting: the gate must not depend on the
    lifespan having refused it first.
    """
    if getattr(settings, "is_production", False) is True:
        return False
    return getattr(settings, "api_allow_tokenless_loopback", False) is True


_DOCS_PATHS = frozenset({"/docs", "/redoc", "/openapi.json"})

# Its own route because middleware/http.py judges a cross-site GET of it as a write.
AUTH_PROFILE_EXPORT_ROUTE = "/auth-profiles/{profile_name}/export"


def is_bearer_exempt_path(path: str, *, docs_are_public: bool) -> bool:
    """Whether a path answers without the bearer token: the one list, the token gate's and the session check's."""
    # /share/{token}/... authenticates with the signed share token instead
    # (app/routes/share.py). POST /sessions/{id}/share, which mints one, is not
    # under this prefix and stays behind the bearer token.
    if path.startswith("/share/"):
        return True
    # /ui/{anything} only redirects to /dashboard, as /ui and /ui/ do, and serves
    # nothing; answering 401 for old bookmarks like /ui/foo was inconsistent.
    if path.startswith("/ui/"):
        return True
    # /docs, /redoc and /openapi.json serve the API schema, which is public (it is
    # in the repo) and holds no data. A browser opening /docs cannot send the
    # bearer token; the schema declares it, so Swagger's Authorize sends it from there.
    # Only on a loopback deployment, where the browser is the operator's own: an
    # exposed one answers 401 as it did before they were opened up.
    if path in _DOCS_PATHS:
        return docs_are_public
    return path in {
        "/healthz",
        "/readyz",
        "/mesh/receive",
        "/version",
        "/dashboard",
        "/dashboard/",
        "/ui",
        "/ui/",
    }


def has_named_credentials(settings: Any) -> bool:
    """True when operators can be told apart: at least one credential proves who holds it."""
    return any(credential.verifies_identity for credential in credentials_for(settings))


def credential_names_operator(settings: Any, operator_id: str) -> bool:
    """Whether a named credential proves ``operator_id`` now, by the settings as they are: a removal counts at once."""
    return any(credential.operator_id == operator_id for credential in credentials_for(settings))


_HTTP_OPTIONAL_WHITESPACE = " \t"


def normalize_authorization(authorization: str) -> str:
    """The Authorization header as `<scheme> <token>`: the scheme lowercased, no spaces or tabs around the token.

    Whitespace is not part of a configured token, so it is not part of the presented
    one either: RFC 7235 allows several spaces after the scheme, and uvicorn's default
    parser (httptools) keeps trailing whitespace that h11 trims. Only HTTP's optional
    whitespace counts, so both parsers agree. The scheme is case-insensitive and the
    token is not. The rate limiter keys on this too: a bucket per spelling would give a
    token holder a fresh limit for each one.
    """
    scheme, _, token = authorization.strip(_HTTP_OPTIONAL_WHITESPACE).partition(" ")
    return f"{scheme.lower()} {token.strip(_HTTP_OPTIONAL_WHITESPACE)}"


def match_credential(credentials: Sequence[Credential], authorization: str) -> Credential | None:
    """The credential this request presented, or None.

    Every candidate is compared even after a match, so the work does not depend
    on which token was presented or how many are configured.
    """
    presented = normalize_authorization(authorization).encode()
    matched: Credential | None = None
    for credential in credentials:
        expected = f"bearer {credential.token}".encode()
        if hmac.compare_digest(presented, expected) and matched is None:
            matched = credential
    return matched


def policy_for_scope(
    scope: str,
    credentials: Sequence[Credential],
    *,
    allow_tokenless_loopback: bool = False,
) -> AuthPolicy:
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

    if allow_tokenless_loopback:
        return AuthPolicy(
            scope=scope,
            required=False,
            reason="loopback-only bind, no bearer credential, and API_ALLOW_TOKENLESS_LOOPBACK=true",
            has_credential=False,
        )

    # Startup loads or generates a token before any request (app/api_token.py), so
    # this is reached only if that did not happen: fail closed, never open.
    return AuthPolicy(
        scope=scope,
        required=True,
        reason=(
            "loopback-only bind with no bearer credential configured or loaded from API_TOKEN_FILE, "
            "and API_ALLOW_TOKENLESS_LOOPBACK is off, so no request can be authorised"
        ),
        has_credential=False,
    )


def resolve_auth_policy(settings: Any, environ: Mapping[str, str] | None = None) -> AuthPolicy:
    return policy_for_scope(
        resolve_bind_scope(settings, environ),
        credentials_for(settings),
        allow_tokenless_loopback=tokenless_loopback_allowed(settings),
    )
