from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .auth_policy import credentials_for
from .ocr import tesseract_available


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


def pre_request_gate_check(settings: Any, gates: dict[str, Any]) -> ReadinessCheck:
    """The pre-request gate's state (RequestGates.status). Reported, never "fail": a health check must not
    restart-loop the controller on it, and sessions are refused on a browser without a gate anyway."""
    name = "pre_request_gate"
    details = dict(gates)
    patterns = [item.strip() for item in str(getattr(settings, "allowed_hosts", "")).split(",") if item.strip()]
    if not getattr(settings, "pre_request_blocking", True):
        return ReadinessCheck(
            name=name,
            status="warn",
            message=(
                "PRE_REQUEST_BLOCKING=false: a page's navigation to a host ALLOWED_HOSTS does not allow is sent, "
                "and the page is refused only after it loads."
            ),
            fix="Set PRE_REQUEST_BLOCKING=true.",
            details=details,
        )
    if not patterns or "*" in patterns:
        return ReadinessCheck(name=name, status="pass", message="ALLOWED_HOSTS allows every host: nothing to stop.")
    state = gates.get("state")
    if state in {"lost", "attach_failed"}:
        return ReadinessCheck(
            name=name,
            status="warn",
            message=(
                f"The pre-request gate is {state.replace('_', ' ')} on a browser: sessions there are refused "
                "until a new gate attaches (the next session tries)."
            ),
            fix="Check the controller log; set PRE_REQUEST_BLOCKING=false to run on the post-load guard alone.",
            details=details,
        )
    # A launched browser (not an attach mode) whose preference did not read back 2, or was never read.
    prefetch_on = [
        gate for gate in gates.get("gates", []) if gate.get("attach_mode") is False and gate.get("prefetch_pref") != 2
    ]
    if prefetch_on:
        return ReadinessCheck(
            name=name,
            status="warn",
            message=(
                "Speculation-rules prefetch could not be turned off in a browser the controller launched: a "
                "prefetch of a host ALLOWED_HOSTS does not allow is sent, and only the page is refused."
            ),
            fix="Check the controller log; the browser-node image also sets the NetworkPredictionOptions policy.",
            details=details,
        )
    unswitched = [
        gate for gate in gates.get("gates", []) if gate.get("attach_mode") and not gate.get("switch_declared")
    ]
    if unswitched:
        return ReadinessCheck(
            name=name,
            status="warn",
            message=(
                "The pre-request gate is attached to a Chrome the controller did not launch, without "
                "CDP_BLOCK_ORIGIN_ON_REDIRECT_DISABLED=true: cross-origin 307/308 POST redirects are stopped in the "
                "controller's sessions and fail in the operator's own tabs too."
            ),
            fix=(
                "Start that Chrome with --disable-features=BlockOriginHeaderModificationOnRedirect and set "
                "CDP_BLOCK_ORIGIN_ON_REDIRECT_DISABLED=true."
            ),
            details=details,
        )
    message = (
        "The pre-request gate is attached: a navigation to a host ALLOWED_HOSTS does not allow is stopped before it "
        "is sent."
        if state == "attached"
        else "No browser yet; the pre-request gate attaches with the first."
    )
    return ReadinessCheck(name=name, status="pass", message=message, details=details)


def run_readiness_checks(
    settings: Any, mode: str = "normal", *, request_gates: dict[str, Any] | None = None
) -> ReadinessReport:
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

    bearer = credentials_for(settings)  # named API_BEARER_TOKENS count as authentication too
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
                message="No API_BEARER_TOKEN or API_BEARER_TOKENS set; API is unauthenticated.",
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
    if allowed_hosts.strip() == "*":
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

    # Screenshot redaction draws over OCR-detected words; with no OCR it leaves
    # the image untouched and says nothing, so the gap is reported here.
    # With PII_SCRUB_SCREENSHOT_STRICT the gap is a failure: those screenshots are withheld.
    if pii_enabled and getattr(settings, "pii_scrub_screenshot", True):
        strict = getattr(settings, "pii_scrub_screenshot_strict", False) is True
        gap_status = "fail" if strict else "warn"
        gap_effect = "are withheld (PII_SCRUB_SCREENSHOT_STRICT=true)" if strict else "are NOT PII-scrubbed"
        if not getattr(settings, "ocr_enabled", True):
            checks.append(
                ReadinessCheck(
                    name="screenshot_scrubbing",
                    status=gap_status,
                    message=f"OCR_ENABLED=false: screenshots {gap_effect} (redaction needs OCR boxes).",
                    fix="Set OCR_ENABLED=true, or set PII_SCRUB_SCREENSHOT=false to make the gap explicit.",
                )
            )
        elif not tesseract_available():
            checks.append(
                ReadinessCheck(
                    name="screenshot_scrubbing",
                    status=gap_status,
                    message=f"tesseract is not installed: screenshots {gap_effect} (redaction needs OCR boxes).",
                    fix="Run the controller from the Docker image, which ships tesseract, or install tesseract-ocr.",
                )
            )
        else:
            checks.append(
                ReadinessCheck(
                    name="screenshot_scrubbing",
                    status="pass",
                    message="Screenshot PII scrubbing is enabled and tesseract is available.",
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

    if len(credentials_for(settings)) > 1:
        if getattr(settings, "force_governed_approvals", False):
            checks.append(
                ReadinessCheck(
                    name="governed_approvals",
                    status="pass",
                    message="FORCE_GOVERNED_APPROVALS=true: MCP write tools and the agent loop need an operator's approval.",
                )
            )
        else:
            checks.append(
                ReadinessCheck(
                    name="governed_approvals",
                    status="warn",
                    message=(
                        "Several operator credentials are configured, but governed approvals are each "
                        "caller's choice: an agent that omits workflow_profile=governed runs its writes "
                        "(MCP tools and the agent loop) without approval."
                    ),
                    fix=(
                        "Set FORCE_GOVERNED_APPROVALS=true to require approval for every MCP write tool "
                        "and every agent-loop write, and leave AUTONOMOUS_APPROVALS off."
                    ),
                )
            )

    if request_gates is not None:
        checks.append(pre_request_gate_check(settings, request_gates))

    statuses = {check.status for check in checks}
    if "fail" in statuses:
        overall = "fail"
    elif "warn" in statuses:
        overall = "warn"
    else:
        overall = "pass"

    return ReadinessReport(overall=overall, checks=checks, mode=mode)
