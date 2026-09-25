"""Private, assertion-bound bridge from the portal to the tenant provisioner.

This service is deliberately absent from public ingress.  It is the only
control-plane process with Docker access, and it accepts no caller-selected
path, project, container, user, or tenant identifier.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

TENANTS_DIR = Path(__file__).resolve().parents[1] / "deploy" / "tenants"
if str(TENANTS_DIR) not in sys.path:
    sys.path.insert(0, str(TENANTS_DIR))

from provisioner import (  # noqa: E402
    ProvisioningConfig,
    ProvisioningError,
    TenantEnrollment,
    TenantProvisioner,
)

from tenant_stacks.policy import MAX_ALLOWED_HOSTS, normalize_hostnames  # noqa: E402


class ApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    portal_assertion: str = Field(min_length=40, max_length=4096)
    allowed_hosts: list[str] = Field(max_length=MAX_ALLOWED_HOSTS)
    expected_revision: int = Field(ge=0)


def _public_key(value: str) -> Ed25519PublicKey:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        return Ed25519PublicKey.from_public_bytes(raw)
    except (TypeError, ValueError):
        raise ValueError("portal assertion public key is invalid") from None


def _claims(assertion: str, key: Ed25519PublicKey, now: float) -> tuple[str, str]:
    try:
        payload, encoded_signature = assertion.split(".", 1)
        signature = base64.urlsafe_b64decode(
            encoded_signature + "=" * (-len(encoded_signature) % 4)
        )
        key.verify(signature, payload.encode("ascii"))
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(raw)
    except (ValueError, UnicodeError, json.JSONDecodeError, InvalidSignature):
        raise HTTPException(403, "Invalid portal assertion") from None
    if not isinstance(claims, dict) or claims.get("purpose") != "site_policy_change":
        raise HTTPException(403, "Invalid portal assertion")
    user_id, tenant_id = claims.get("sub"), claims.get("tenant")
    issued, expires = claims.get("iat"), claims.get("exp")
    if (
        not isinstance(user_id, str)
        or not user_id
        or len(user_id) > 256
        or not isinstance(tenant_id, str)
        or not tenant_id
        or len(tenant_id) > 256
        or not isinstance(issued, int)
        or not isinstance(expires, int)
        or issued > now + 5
        or expires < now
        or expires - issued > 60
    ):
        raise HTTPException(403, "Invalid or expired portal assertion")
    return user_id, tenant_id


def create_app(
    *,
    internal_token: str,
    portal_assertion_public_key: str,
    state_root: str | Path,
    compose_file: str | Path,
    max_running: int = 1,
    idle_timeout_seconds: int = 900,
    clock: Callable[[], float] = time.time,
    provisioner_factory: Callable[[], TenantProvisioner] | None = None,
) -> FastAPI:
    if len(internal_token) < 32:
        raise ValueError("tenant policy internal token must have at least 32 characters")
    assertion_key = _public_key(portal_assertion_public_key)
    root, compose = Path(state_root), Path(compose_file)

    def make_provisioner() -> TenantProvisioner:
        if provisioner_factory is not None:
            return provisioner_factory()
        return TenantProvisioner(
            ProvisioningConfig(
                state_root=root,
                allowed_hosts="example.invalid",
                max_running=max_running,
                portal_assertion_public_key=portal_assertion_public_key,
                idle_timeout_seconds=idle_timeout_seconds,
                compose_file=compose,
            )
        )

    app = FastAPI(
        title="Auto Browser tenant policy applier",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def require_internal(authorization: str | None) -> None:
        if (
            not authorization
            or not authorization.startswith("Bearer ")
            or not secrets.compare_digest(authorization[7:], internal_token)
        ):
            raise HTTPException(401, "Internal bearer required")

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/internal/allowed-hosts/apply")
    async def apply_allowed_hosts(
        payload: ApplyRequest, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        require_internal(authorization)
        user_id, tenant_id = _claims(payload.portal_assertion, assertion_key, clock())
        try:
            hosts = normalize_hostnames(payload.allowed_hosts, allow_empty=True)
            descriptor = make_provisioner().update_allowed_hosts(
                TenantEnrollment(user_id, tenant_id),
                hosts,
                expected_revision=payload.expected_revision,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except ProvisioningError as exc:
            # Do not reflect paths, Docker output, or private descriptor data.
            detail = str(exc)
            status = 409 if "revision" in detail.lower() else 503
            raise HTTPException(status, "Allowed-sites update could not be applied") from None
        return {
            "allowed_hosts": list(descriptor.allowed_hosts),
            "revision": descriptor.policy_revision,
            "controller_restarted": True,
        }

    return app


def app_from_environment() -> FastAPI:
    return create_app(
        internal_token=os.environ["TENANT_POLICY_INTERNAL_TOKEN"],
        portal_assertion_public_key=os.environ["BROKER_PORTAL_ASSERTION_PUBLIC_KEY"],
        state_root=os.environ["TENANT_STACK_ROOT"],
        compose_file=os.environ.get("TENANT_COMPOSE_FILE", "/app/deploy/tenants/compose.yml"),
        max_running=int(os.environ.get("TENANT_MAX_RUNNING", "1")),
        idle_timeout_seconds=int(os.environ.get("TENANT_IDLE_TIMEOUT_SECONDS", "900")),
    )


app = app_from_environment() if os.environ.get("TENANT_POLICY_INTERNAL_TOKEN") else None
