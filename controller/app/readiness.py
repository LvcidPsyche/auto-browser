from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ReadinessCheck:
    name: str
    status: str
    message: str
    fix: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReadinessReport:
    overall: str
    checks: list[ReadinessCheck] = field(default_factory=list)
    mode: str = "normal"

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall": self.overall,
            "mode": self.mode,
            "checks": [
                {
                    "name": check.name,
                    "status": check.status,
                    "message": check.message,
                    "fix": check.fix,
                    "details": check.details,
                }
                for check in self.checks
            ],
        }


def run_readiness_checks(settings: Any, mode: str = "normal") -> ReadinessReport:
    checks: list[ReadinessCheck] = []

    enc_key = getattr(settings, "auth_state_encryption_key", None)
    require_encryption = getattr(settings, "require_auth_state_encryption", False)
    if enc_key:
        checks.append(
            ReadinessCheck(
                name="auth_state_encryption",
                status="pass",
                message="Auth state encryption key is configured.",
            )
        )
    elif require_encryption:
        checks.append(
            ReadinessCheck(
                name="auth_state_encryption",
                status="fail",
                message="REQUIRE_AUTH_STATE_ENCRYPTION=true but AUTH_STATE_ENCRYPTION_KEY is not set.",
                fix="Set AUTH_STATE_ENCRYPTION_KEY to a valid Fernet key.",
            )
        )
    else:
        checks.append(
            ReadinessCheck(
                name="auth_state_encryption",
                status="warn",
                message="Auth state is stored unencrypted.",
                fix="Set AUTH_STATE_ENCRYPTION_KEY=<fernet-key> and REQUIRE_AUTH_STATE_ENCRYPTION=true.",
            )
        )

    require_operator = getattr(settings, "require_operator_id", False)
    if require_operator:
        checks.append(
            ReadinessCheck(
                name="operator_identity",
                status="pass",
                message="Operator identity header is required.",
            )
        )
    else:
        checks.append(
            ReadinessCheck(
                name="operator_identity",
                status="warn",
                message="REQUIRE_OPERATOR_ID is false; actions may run without an identified operator.",
                fix="Set REQUIRE_OPERATOR_ID=true for production.",
            )
        )

    bearer = getattr(settings, "api_bearer_token", None)
    if bearer:
        checks.append(
            ReadinessCheck(
                name="api_bearer_token",
                status="pass",
                message="API bearer token is configured.",
            )
        )
    else:
        checks.append(
            ReadinessCheck(
                name="api_bearer_token",
                status="warn",
                message="No API_BEARER_TOKEN set; API is unauthenticated.",
                fix="Set API_BEARER_TOKEN=<secret> in production.",
            )
        )

    isolation = getattr(settings, "session_isolation_mode", "shared_browser_node")
    if isolation == "docker_ephemeral":
        checks.append(
            ReadinessCheck(
                name="session_isolation",
                status="pass",
                message="Session isolation mode is docker_ephemeral.",
                details={"mode": isolation},
            )
        )
    else:
        checks.append(
            ReadinessCheck(
                name="session_isolation",
                status="warn" if mode == "normal" else "fail",
                message="Sessions share a single browser process.",
                fix="Set SESSION_ISOLATION_MODE=docker_ephemeral for confidential workloads.",
                details={"mode": isolation},
            )
        )

    witness_enabled = getattr(settings, "witness_enabled", True)
    witness_remote = getattr(settings, "witness_remote_url", None)
    if not witness_enabled:
        checks.append(
            ReadinessCheck(
                name="witness_audit",
                status="fail",
                message="Witness audit trail is disabled.",
                fix="Set WITNESS_ENABLED=true.",
            )
        )
    elif witness_remote:
        checks.append(
            ReadinessCheck(
                name="witness_audit",
                status="pass",
                message="Witness enabled with remote anchoring.",
                details={"remote_url": witness_remote},
            )
        )
    else:
        checks.append(
            ReadinessCheck(
                name="witness_audit",
                status="warn",
                message="Witness enabled locally only; receipts are not externally anchored.",
                fix="Set WITNESS_REMOTE_URL for external audit anchoring.",
            )
        )

    allowed_hosts = str(getattr(settings, "allowed_hosts", "*") or "*")
    if getattr(settings, "navigation_policy", "allowlist") == "public_internet":
        checks.append(
            ReadinessCheck(
                name="host_allowlist",
                status="warn" if mode == "confidential" else "pass",
                message=(
                    "NAVIGATION_POLICY=public_internet: any public site; private/internal "
                    "addresses and NAVIGATION_DENY_HOSTS are refused."
                ),
                details={"navigation_deny_hosts": str(getattr(settings, "navigation_deny_hosts", "") or "")},
            )
        )
    elif allowed_hosts.strip() == "*":
        checks.append(
            ReadinessCheck(
                name="host_allowlist",
                status="warn" if mode == "confidential" else "pass",
                message="ALLOWED_HOSTS is unrestricted (*). All URLs are permitted.",
                fix=(
                    "Set ALLOWED_HOSTS=domain1.com,domain2.com to restrict navigation."
                    if mode == "confidential"
                    else None
                ),
            )
        )
    else:
        checks.append(
            ReadinessCheck(
                name="host_allowlist",
                status="pass",
                message=f"Host allowlist configured: {allowed_hosts}",
                details={"allowed_hosts": allowed_hosts},
            )
        )

    pii_enabled = getattr(settings, "pii_scrub_enabled", True)
    if pii_enabled:
        checks.append(
            ReadinessCheck(
                name="pii_scrubbing",
                status="pass",
                message="PII scrubbing is enabled.",
            )
        )
    else:
        checks.append(
            ReadinessCheck(
                name="pii_scrubbing",
                status="warn",
                message="PII scrubbing is disabled.",
                fix="Set PII_SCRUB_ENABLED=true.",
            )
        )

    require_upload_approval = getattr(settings, "require_approval_for_uploads", True)
    if require_upload_approval:
        checks.append(
            ReadinessCheck(
                name="upload_approval",
                status="pass",
                message="File uploads require approval.",
            )
        )
    else:
        checks.append(
            ReadinessCheck(
                name="upload_approval",
                status="warn",
                message="File uploads do not require approval.",
                fix="Set REQUIRE_APPROVAL_FOR_UPLOADS=true.",
            )
        )

    statuses = {check.status for check in checks}
    if "fail" in statuses:
        overall = "fail"
    elif "warn" in statuses:
        overall = "warn"
    else:
        overall = "pass"

    return ReadinessReport(overall=overall, checks=checks, mode=mode)
