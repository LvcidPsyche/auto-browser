from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from .config import Settings


@dataclass(slots=True)
class RuntimePolicyReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


LOCAL_HOSTS = {"", "127.0.0.1", "localhost", "::1", "0.0.0.0"}


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

    takeover_host = (urlparse(settings.takeover_url).hostname or "").strip().lower()
    if takeover_host in LOCAL_HOSTS and not settings.isolated_tunnel_enabled:
        report.warnings.append(
            "TAKEOVER_URL is still local-only and ISOLATED_TUNNEL_ENABLED=false; front it with Cloudflare Access, Tailscale, or a tunnel before remote use"
        )

    if not settings.metrics_enabled:
        report.warnings.append("METRICS_ENABLED=false; observability will be limited in production")

    return report
