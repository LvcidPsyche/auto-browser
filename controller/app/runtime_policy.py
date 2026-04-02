from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from shutil import which
from urllib.parse import urlparse

from .config import Settings
from .providers.base import BaseProviderAdapter


@dataclass(slots=True)
class RuntimePolicyReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


LOCAL_HOSTS = {"", "127.0.0.1", "localhost", "::1", "0.0.0.0"}

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
            report.errors.append(
                f"{auth_mode_attr}={auth_mode or '<empty>'} is invalid; expected one of: {supported}"
            )
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
                f"{auth_mode_attr}=cli requires a working {cli_label} CLI in {cli_path_attr}"
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


def validate_runtime_policy(settings: Settings) -> RuntimePolicyReport:
    report = RuntimePolicyReport()

    if not settings.is_production:
        return report

    if not settings.api_bearer_token:
        report.errors.append("API_BEARER_TOKEN is required when APP_ENV=production")

    if not settings.require_operator_id:
        report.errors.append("REQUIRE_OPERATOR_ID=true is required when APP_ENV=production")

    if not settings.auth_state_encryption_key:
        report.errors.append("AUTH_STATE_ENCRYPTION_KEY is required when APP_ENV=production")

    if not settings.require_auth_state_encryption:
        report.errors.append(
            "REQUIRE_AUTH_STATE_ENCRYPTION=true is required when APP_ENV=production"
        )

    if not settings.request_rate_limit_enabled:
        report.errors.append("REQUEST_RATE_LIMIT_ENABLED=true is required when APP_ENV=production")

    if settings.request_rate_limit_requests <= 0 or settings.request_rate_limit_window_seconds <= 0:
        report.errors.append(
            "REQUEST_RATE_LIMIT_REQUESTS and REQUEST_RATE_LIMIT_WINDOW_SECONDS must be positive"
        )

    if settings.allowed_hosts.strip() in {"", "example.com", "example.com,localhost"}:
        report.warnings.append(
            "ALLOWED_HOSTS still contains the default placeholder values; tighten it before launch"
        )

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
    if takeover_host in LOCAL_HOSTS and not settings.isolated_tunnel_enabled:
        report.warnings.append(
            "TAKEOVER_URL is still local-only and ISOLATED_TUNNEL_ENABLED=false; front it with Cloudflare Access, Tailscale, or a tunnel before remote use"
        )

    if not settings.metrics_enabled:
        report.warnings.append("METRICS_ENABLED=false; observability will be limited in production")

    _validate_provider_runtime(settings, report)

    return report
