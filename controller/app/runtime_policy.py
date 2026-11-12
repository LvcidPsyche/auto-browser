from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from shutil import which
from typing import Iterable, Sequence
from urllib.parse import urlparse

from .auth_policy import (
    BIND_SCOPE_EXPOSED,
    Credential,
    credential_names_operator,
    credentials_for,
    is_local_only_host,
    resolve_bind_scope,
    tokenless_loopback_allowed,
)
from .config import Settings
from .host_patterns import is_wildcard_pattern
from .providers.base import BaseProviderAdapter, auth_mode_shown
from .url_safety import refuse_ambiguous_host
from .utils import error_text


@dataclass(slots=True)
class RuntimePolicyReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# Production floor for API_BEARER_TOKEN. "Required" alone was satisfied by
# API_BEARER_TOKEN=x, which is not meaningfully different from no token.
MIN_PRODUCTION_BEARER_TOKEN_LENGTH = 32

# An entry of APPROVAL_REFUSE_SELF_APPROVAL_OPERATORS with this many characters in a row of a bearer token of the
# deployment is taken for the token, or a part of it, and is not printed. Eight: a quarter of the shortest token
# production accepts, and long enough that an operator's id does not stand in a token by chance. At two or three
# characters it does (an id of two hexadecimal digits stands in one 40-digit token in seven), and the warning
# would then hide the very id it is there to name.
MIN_TOKEN_RUN_NOT_PRINTED = 8
# A token of that length or shorter (a development setup's: production's are 32 or more) has no such run short of
# itself, so the run that counts for it is one character fewer than it has, and such a token cut short by one is
# not printed either. Never fewer than this many, which the two or three above rule out: a token of this length or
# shorter keeps an entry back only where the entry holds it as a word of its own ('dev' hides 'Bearer dev', and does
# not hide 'developer').
MIN_SHORT_TOKEN_RUN_NOT_PRINTED = 4

CLI_PROVIDER_CHECKS = (
    {
        "provider": "openai",
        "auth_mode_attr": "OPENAI_AUTH_MODE",
        "allowed_modes": {"api", "cli", "host_bridge"},
        "api_key_attr": "OPENAI_API_KEY",
        "cli_path_attr": "OPENAI_CLI_PATH",
        "cli_label": "codex",
        "auth_markers": (".codex",),
        "host_bridge_socket_attr": "OPENAI_HOST_BRIDGE_SOCKET",
    },
    {
        "provider": "claude",
        "auth_mode_attr": "CLAUDE_AUTH_MODE",
        "allowed_modes": {"api", "cli"},
        "api_key_attr": "ANTHROPIC_API_KEY",
        "cli_path_attr": "CLAUDE_CLI_PATH",
        "cli_label": "claude",
        "auth_markers": (".claude.json", ".claude"),
    },
    {
        "provider": "gemini",
        "auth_mode_attr": "GEMINI_AUTH_MODE",
        "allowed_modes": {"api", "cli"},
        "api_key_attr": "GEMINI_API_KEY",
        "cli_path_attr": "GEMINI_CLI_PATH",
        "cli_label": "gemini",
        "auth_markers": (".gemini",),
    },
)


def _validate_provider_runtime(settings: Settings, report: RuntimePolicyReport) -> None:
    any_provider_ready = False
    cli_home = (settings.cli_home or "").strip()
    cli_home_path = Path(cli_home) if cli_home else None
    missing_cli_home_reported = False

    for check in CLI_PROVIDER_CHECKS:
        provider_name = check["provider"]
        auth_mode_attr = check["auth_mode_attr"]
        api_key_attr = check["api_key_attr"]
        cli_path_attr = check.get("cli_path_attr")
        cli_label = check.get("cli_label")
        auth_markers = check.get("auth_markers", ())
        allowed_modes = check["allowed_modes"]
        auth_mode = (getattr(settings, auth_mode_attr.lower()) or "").strip().lower()
        if auth_mode not in allowed_modes:
            supported = ", ".join(sorted(allowed_modes))
            # A refused start logs this: a value too long to be a mode is not printed (auth_mode_shown).
            shown = auth_mode_shown(auth_mode) or "<empty>"
            report.errors.append(f"{auth_mode_attr}={shown} is invalid; expected one of: {supported}")
            continue

        if auth_mode == "api":
            if getattr(settings, api_key_attr.lower()):
                any_provider_ready = True
            continue

        if auth_mode == "host_bridge":
            socket_attr = check.get("host_bridge_socket_attr")
            socket_path = getattr(settings, socket_attr.lower()) if socket_attr else None
            ready, detail = BaseProviderAdapter.describe_socket_readiness(
                socket_path=str(socket_path) if socket_path else None,
                label=f"{provider_name} host bridge",
            )
            if not ready:
                report.errors.append(f"{auth_mode_attr}=host_bridge is not ready: {detail}")
                continue
            any_provider_ready = True
            continue

        cli_path = getattr(settings, cli_path_attr.lower())
        resolved_cli = which(cli_path) if cli_path else None
        if not resolved_cli:
            report.errors.append(
                f"{auth_mode_attr}=cli requires a working {cli_label} CLI in {cli_path_attr}; the controller "
                "image includes the provider CLIs only when built with INSTALL_AGENT_CLIS=true"
            )
            continue

        if cli_home_path is None:
            any_provider_ready = True
            report.warnings.append(
                f"{provider_name} uses CLI auth but CLI_HOME is unset; startup cannot verify signed-in state"
            )
            continue

        if not cli_home_path.exists():
            if not missing_cli_home_reported:
                report.errors.append(f"CLI_HOME path does not exist: {cli_home_path}")
                missing_cli_home_reported = True
            continue

        if not any((cli_home_path / marker).exists() for marker in auth_markers):
            expected = ", ".join(str(cli_home_path / marker) for marker in auth_markers)
            report.errors.append(
                f"{provider_name} uses CLI auth but no auth state was found; expected one of: {expected}"
            )
            continue

        any_provider_ready = True

    if not any_provider_ready:
        report.warnings.append(
            "No model provider is ready; /agent/step and /agent/run will fail until a provider is configured"
        )


def _warn_weak_loopback_tokens(credentials: Sequence[Credential], report: RuntimePolicyReport) -> None:
    """Loopback keeps short tokens legal, but containers on the compose network can reach port 8000."""
    for credential in credentials:
        if len(credential.token) >= MIN_PRODUCTION_BEARER_TOKEN_LENGTH:
            continue
        name = f"the token for operator '{credential.operator_id}'" if credential.operator_id else "API_BEARER_TOKEN"
        report.warnings.append(
            f"{name} is {len(credential.token)} characters, shorter than the {MIN_PRODUCTION_BEARER_TOKEN_LENGTH} "
            "production floor. A loopback publish mapping does not stop containers on the compose network "
            "from reaching port 8000, and a short token is brute-forceable from them. Use a longer one, or "
            "remove it to have the controller generate its own."
        )


def _validate_api_authentication(settings: Settings, report: RuntimePolicyReport) -> None:
    """The API must carry a token whenever it is reachable, or in production.

    Reachability is the load-bearing half. `APP_ENV=production` was the only
    trigger until 1.7.0, and shipped compose defaulted it to `development`, so
    the check that was supposed to guarantee authentication never ran on the
    configuration people actually deployed.
    """
    scope = resolve_bind_scope(settings)
    exposed = scope == BIND_SCOPE_EXPOSED
    if not (exposed or settings.is_production):
        credentials = credentials_for(settings)
        if credentials:
            _warn_weak_loopback_tokens(credentials, report)
            return
        if tokenless_loopback_allowed(settings):
            wildcards = [
                pattern for pattern in settings.controller_allowed_host_patterns if is_wildcard_pattern(pattern)
            ]
            if wildcards:
                # The Host check admits no name by a wildcard on such a controller (middleware/hosts.py); a page that
                # rebinds a name under one to 127.0.0.1 would otherwise drive it whole, with no token to ask for.
                report.errors.append(
                    f"CONTROLLER_ALLOWED_HOSTS lists {', '.join(repr(pattern) for pattern in wildcards)}, and this "
                    "controller has no API token (API_ALLOW_TOKENLESS_LOOPBACK=true): a wildcard admits a web page "
                    "that rebinds a name under it to 127.0.0.1. Set API_BEARER_TOKEN (or remove "
                    "API_ALLOW_TOKENLESS_LOOPBACK: the controller then uses the token in API_TOKEN_FILE, and "
                    "generates one there if the file holds none, and every client must send it), or list exact host "
                    "names, whose DNS you control."
                )
                return
            report.warnings.append(
                "API_BEARER_TOKEN is unset and API_ALLOW_TOKENLESS_LOOPBACK=true, so the controller serves "
                "every route unauthenticated. API_BIND_SCOPE=loopback keeps it off other machines, but any "
                "process that can reach port 8000, including containers on the compose network, can drive "
                "the browser. Remove API_ALLOW_TOKENLESS_LOOPBACK: the controller then uses the token in "
                "API_TOKEN_FILE, and generates one there if the file holds none, and every client must send it."
            )
            return
        # ensure_api_credential runs first and loads or generates one; this is the
        # backstop if it did not.
        report.errors.append(
            "No API credential was configured, loaded or generated from API_TOKEN_FILE "
            f"({getattr(settings, 'api_token_file', '')}), and API_ALLOW_TOKENLESS_LOOPBACK is off, "
            "so no request could be authorised. Set API_BEARER_TOKEN, make API_TOKEN_FILE writable, "
            "or set API_ALLOW_TOKENLESS_LOOPBACK=true."
        )
        return

    trigger = "the API is reachable off-box (API_BIND_SCOPE=exposed)" if exposed else "APP_ENV=production"
    credentials = credentials_for(settings)
    if not credentials:
        report.errors.append(
            f"API_BEARER_TOKEN is required when {trigger}. Set a token (API_BEARER_TOKEN, or a file at "
            "API_TOKEN_FILE), or set API_BIND_SCOPE=loopback if this instance is only published on 127.0.0.1."
        )
        return

    # "Required" was satisfied by API_BEARER_TOKEN=x. A one-character token is
    # not meaningfully different from no token, and unauthenticated requests are
    # only throttled per-IP, so a weak token is guessable. Every credential has
    # to clear the floor — one weak entry in API_BEARER_TOKENS is a way in.
    for credential in credentials:
        if len(credential.token) >= MIN_PRODUCTION_BEARER_TOKEN_LENGTH:
            continue
        name = f"the token for operator '{credential.operator_id}'" if credential.operator_id else "API_BEARER_TOKEN"
        report.errors.append(
            f"{name} must be at least {MIN_PRODUCTION_BEARER_TOKEN_LENGTH} characters "
            f"when {trigger} (got {len(credential.token)})"
        )


def _validate_cdp_connect_url(settings: Settings, report: RuntimePolicyReport) -> None:
    """Playwright parses CDP_CONNECT_URL as a browser does and the startup log as urllib does: where they could read
    different hosts, the log would name one and the controller connect to the other."""
    if not settings.cdp_connect_url:
        return
    try:
        refuse_ambiguous_host(settings.cdp_connect_url, field_name="CDP_CONNECT_URL")
    except ValueError as exc:
        report.errors.append(error_text(exc))


def _shows_a_token(text: str, credentials: Iterable[Credential], *, an_operator_id: bool) -> bool:
    """Whether printing ``text`` would show a bearer token of the deployment, whole or in part. Compared without
    regard to case (_case_free): a token of hexadecimal digits in upper case is the same secret.

    In part is MIN_TOKEN_RUN_NOT_PRINTED of a token's characters in a row, and for a token no longer than that all
    but one of them, down to MIN_SHORT_TOKEN_RUN_NOT_PRINTED: below it an id holds a token's letters by chance, and
    only a text that holds the token as a word of its own shows it (_holds_as_a_word: 'Bearer dev', not
    'developer'). A run that stands in the token's own operator id is no secret, and
    does not count: with alice-prod:alice-prod-9f8a..., 'alice-prod2' is an id like any other.

    An operator id of API_BEARER_TOKENS shows a token only where it holds one of MIN_TOKEN_RUN_NOT_PRINTED
    characters or more whole.

    Every length is the token's as it is configured, and each run is that many of its characters, folded: a fold can
    be longer than what it folds (a sharp s folds to 'ss', the ligature fi to 'fi'), and runs or lengths of the fold
    stood for fewer of the token's characters: 'boss123' was kept back for a token of a sharp s and '12345', and an
    operator id for a token of seven. The fold of a text is the folds of its characters one after another, so
    whatever holds a run in any case holds the run's fold.
    """
    folded = _case_free(text)
    for credential in credentials:
        configured = credential.token
        token = _case_free(configured)
        if an_operator_id:
            if len(configured) >= MIN_TOKEN_RUN_NOT_PRINTED and token in folded:
                return True
            continue
        run = min(MIN_TOKEN_RUN_NOT_PRINTED, len(configured) - 1)
        if run < MIN_SHORT_TOKEN_RUN_NOT_PRINTED:
            if _holds_as_a_word(folded, token):
                return True
            continue
        if token in folded:
            return True
        own_id = _case_free(credential.operator_id or "")
        for start in range(len(configured) - run + 1):
            part = _case_free(configured[start : start + run])
            if part in folded and part not in own_id:
                return True
    return False


def _case_free(text: str) -> str:
    """``text`` folded so that each of its characters is one with its every case: casefold() of its upper case.
    casefold() alone leaves dotless i (U+0131) apart from its upper case 'I', which folds to 'i', so a token of it
    was printed in upper case."""
    return text.upper().casefold()


def _holds_as_a_word(text: str, word: str) -> bool:
    """Whether ``word`` stands in ``text`` with, on each side, an end of the text or a character that is no letter
    and no number, of any script (str.isalnum: a Roman numeral and a fraction are numbers): 'dev' in 'Bearer dev',
    'alice=dev' or "'dev'", not in 'developer' or '4dev'."""
    start = text.find(word)
    while start != -1:
        end = start + len(word)
        if (start == 0 or not text[start - 1].isalnum()) and (end == len(text) or not text[end].isalnum()):
            return True
        start = text.find(word, start + 1)
    return False


def _warn_listed_operators_no_credential_proves(settings: Settings, report: RuntimePolicyReport) -> None:
    """APPROVAL_REFUSE_SELF_APPROVAL_OPERATORS is compared to the proven operator id exactly (ApprovalStore), so a
    listed id that no named credential proves refuses nobody, and nothing else would say so.

    The warnings go to the log, and an entry can be a secret in the wrong place: API_BEARER_TOKENS pasted whole
    (id:token pairs), a token alone, or a token with something round it (Bearer, a quote, alice=) or cut short.
    Nothing after an entry's colon is printed. What would be printed, the entry or the text before its colon, is
    printed only when it shows no token of this deployment, whole or in part (_shows_a_token): decided by what it
    contains, since an entry that is a token and one character more is no token by equality. The entries kept back
    are counted. The tokens they hold cannot be: an entry that holds one of three tokens that begin alike holds a
    part of each.
    """
    credentials = credentials_for(settings)
    named = {credential.operator_id for credential in credentials if credential.operator_id}
    named_whatever_the_case = {name.casefold() for name in named}
    pairs: set[str] = set()
    kept_back = 0
    for listed in sorted(settings.approval_refuse_self_approval_operator_ids):
        if credential_names_operator(settings, listed):
            continue
        # An operator id holds no colon: parse_operator_tokens splits an entry at its first one.
        before_colon = listed.partition(":")[0].strip()
        printed = before_colon if ":" in listed else listed
        # An operator id of API_BEARER_TOKENS, in whatever case, is no secret, though a token may begin with it
        # (alice-prod:alice-prod-9f8a...): only a whole token in it keeps it out of the log, and only one of
        # MIN_TOKEN_RUN_NOT_PRINTED characters or more (alice:alice is a development setup's).
        if _shows_a_token(printed, credentials, an_operator_id=printed.casefold() in named_whatever_the_case):
            kept_back += 1
            continue
        if ":" in listed:
            pairs.add(before_colon)
            continue
        warning = (
            f"APPROVAL_REFUSE_SELF_APPROVAL_OPERATORS lists '{listed}', which no credential in API_BEARER_TOKENS "
            "proves, so it refuses nobody: a listed id is compared to the operator's id exactly"
        )
        if listed in named:
            warning += (
                ". Its token is also an earlier entry's, and a token listed twice proves only the first operator "
                "listed with it"
            )
        elif by_case := sorted(name for name in named if name.casefold() == listed.casefold()):
            warning += f". It differs from '{by_case[0]}' only by case"
        report.warnings.append(warning)
    for before_colon in sorted(pairs):
        report.warnings.append(
            f"APPROVAL_REFUSE_SELF_APPROVAL_OPERATORS lists an entry that begins '{before_colon}:' (the rest is not "
            "printed). The setting takes operator ids, not id:token pairs: an operator id holds no colon, so the "
            "entry refuses nobody"
        )
    if kept_back:
        counted = "1 entry that is not printed here, because it holds"
        if kept_back != 1:
            counted = f"{kept_back} entries that are not printed here, because each holds"
        report.warnings.append(
            f"APPROVAL_REFUSE_SELF_APPROVAL_OPERATORS lists {counted} a bearer token of this deployment or a part of "
            f"one ({MIN_TOKEN_RUN_NOT_PRINTED} characters in a row, all but one of a token of "
            f"{MIN_SHORT_TOKEN_RUN_NOT_PRINTED + 1} to {MIN_TOKEN_RUN_NOT_PRINTED}, or a shorter token as a word of "
            "its own), where it takes operator ids: such an entry refuses nobody"
        )


def validate_runtime_policy(settings: Settings) -> RuntimePolicyReport:
    report = RuntimePolicyReport()

    _validate_api_authentication(settings, report)
    _validate_cdp_connect_url(settings, report)
    _warn_listed_operators_no_credential_proves(settings, report)

    if getattr(settings, "codex_allow_host_exec", False):
        report.warnings.append(
            "CODEX_BRIDGE_ALLOW_HOST_EXEC is set; Codex runs with approvals and sandboxing "
            "disabled, so a model decision can execute arbitrary commands on the host. Only "
            "use this inside an environment that is itself sandboxed."
        )

    if not settings.is_production:
        return report

    if not settings.share_token_secret:
        # There is no flag that disables sharing, so the endpoint is always
        # reachable. With no configured secret, SessionShareService generates an
        # ephemeral one per process (session_share.py) — so every issued link
        # silently dies on restart, and links minted by one replica are invalid
        # on another. Silent, and only discovered by a user whose link broke.
        report.errors.append(
            "SHARE_TOKEN_SECRET is required when APP_ENV=production; without it the share-link "
            "signing key is generated per process, so every link breaks on restart"
        )

    if not settings.require_operator_id:
        report.errors.append("REQUIRE_OPERATOR_ID=true is required when APP_ENV=production")

    if not settings.auth_state_encryption_key:
        report.errors.append("AUTH_STATE_ENCRYPTION_KEY is required when APP_ENV=production")

    if not settings.require_auth_state_encryption:
        report.errors.append("REQUIRE_AUTH_STATE_ENCRYPTION=true is required when APP_ENV=production")

    if not settings.request_rate_limit_enabled:
        report.errors.append("REQUEST_RATE_LIMIT_ENABLED=true is required when APP_ENV=production")

    if settings.request_rate_limit_requests <= 0 or settings.request_rate_limit_window_seconds <= 0:
        report.errors.append("REQUEST_RATE_LIMIT_REQUESTS and REQUEST_RATE_LIMIT_WINDOW_SECONDS must be positive")

    if settings.request_rate_limit_max_buckets <= 0:
        report.errors.append("REQUEST_RATE_LIMIT_MAX_BUCKETS must be positive")

    if not settings.controller_allowed_host_patterns:
        report.errors.append("CONTROLLER_ALLOWED_HOSTS is required when APP_ENV=production")

    if "*" in settings.allowed_host_patterns:
        report.errors.append("ALLOWED_HOSTS=* is not permitted when APP_ENV=production")
    elif not settings.allowed_host_patterns:
        report.errors.append("ALLOWED_HOSTS must name at least one host when APP_ENV=production")

    default_allowed_hosts = {
        "",
        "example.com",
        "example.com,localhost",
        "example.com,localhost,127.0.0.1,::1",
    }
    if settings.allowed_hosts.strip() in default_allowed_hosts:
        report.warnings.append("ALLOWED_HOSTS still contains the default placeholder values; tighten it before launch")

    if settings.session_isolation_mode != "docker_ephemeral":
        report.warnings.append(
            "SESSION_ISOLATION_MODE is not docker_ephemeral; keep single-tenant/shared-browser usage explicit"
        )

    if settings.witness_protection_mode_default == "confidential":
        if settings.session_isolation_mode != "docker_ephemeral":
            report.warnings.append(
                "WITNESS_PROTECTION_MODE_DEFAULT=confidential is strongest with SESSION_ISOLATION_MODE=docker_ephemeral"
            )
        if not settings.require_auth_state_encryption:
            report.warnings.append(
                "WITNESS_PROTECTION_MODE_DEFAULT=confidential should be paired with REQUIRE_AUTH_STATE_ENCRYPTION=true"
            )

    if settings.witness_remote_required_for_confidential:
        if not settings.witness_remote_url:
            message = (
                "WITNESS_REMOTE_REQUIRED_FOR_CONFIDENTIAL=true requires WITNESS_REMOTE_URL; "
                "confidential sessions cannot guarantee hosted Witness delivery without it"
            )
            if settings.witness_protection_mode_default == "confidential":
                report.errors.append(message)
            else:
                report.warnings.append(message)
        elif not settings.witness_remote_verify_tls and settings.app_env == "production":
            report.warnings.append(
                "WITNESS_REMOTE_VERIFY_TLS=false weakens hosted Witness transport integrity in production"
            )

    takeover_host = (urlparse(settings.takeover_url).hostname or "").strip().lower()
    if is_local_only_host(takeover_host) and not settings.isolated_tunnel_enabled:
        report.warnings.append(
            "TAKEOVER_URL is still local-only and ISOLATED_TUNNEL_ENABLED=false; front it with Cloudflare Access, Tailscale, or a tunnel before remote use"
        )

    if not settings.metrics_enabled:
        report.warnings.append("METRICS_ENABLED=false; observability will be limited in production")

    if settings.stealth_enabled:
        report.warnings.append("STEALTH_ENABLED=true is outside the default authorized-workflow posture")

    _validate_provider_runtime(settings, report)

    return report
