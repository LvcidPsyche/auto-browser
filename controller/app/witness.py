from __future__ import annotations

import asyncio
import json
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .models import OperatorIdentity, ProtectionMode
from .utils import utc_now

EvidenceMode = Literal["standard", "restricted"]
ConcernSeverity = Literal["info", "warn", "high", "critical"]
ActionClass = Literal[
    "read",
    "write",
    "upload",
    "post",
    "payment",
    "account_change",
    "destructive",
    "control",
    "auth",
]

_HIGH_RISK_CLASSES = {"upload", "post", "payment", "account_change", "destructive", "auth"}


class WitnessConcern(BaseModel):
    code: str
    severity: ConcernSeverity
    summary: str
    enforced: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class WitnessApproval(BaseModel):
    required: bool = False
    approval_id: str | None = None
    status: str | None = None
    reason: str | None = None


class WitnessEvidence(BaseModel):
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    artifacts: dict[str, Any] = Field(default_factory=dict)


class WitnessReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt_id: str
    timestamp: str
    profile: ProtectionMode
    scope: str
    event_type: str
    status: str
    action: str
    action_class: ActionClass
    session_id: str | None = None
    risk_category: str | None = None
    operator: OperatorIdentity
    approval: WitnessApproval = Field(default_factory=WitnessApproval)
    target: dict[str, Any] = Field(default_factory=dict)
    concerns: list[WitnessConcern] = Field(default_factory=list)
    evidence_mode: EvidenceMode = "standard"
    evidence: WitnessEvidence = Field(default_factory=WitnessEvidence)
    metadata: dict[str, Any] = Field(default_factory=dict)
    chain_prev_hash: str | None = None
    chain_hash: str | None = None

    def chain_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"chain_hash"})


class WitnessSessionContext(BaseModel):
    session_id: str
    profile: ProtectionMode = "normal"
    isolation_mode: str = "shared_browser_node"
    shared_takeover_surface: bool = True
    shared_browser_process: bool = True
    auth_state_encrypted: bool = False
    operator: OperatorIdentity


class WitnessActionContext(BaseModel):
    action: str
    action_class: ActionClass
    risk_category: str | None = None
    target: dict[str, Any] = Field(default_factory=dict)
    approval_id: str | None = None
    approval_status: str | None = None
    sensitive_input: bool = False
    stores_auth_material: bool = False
    runtime_requires_approval: bool = False

    @property
    def is_high_risk(self) -> bool:
        return self.action_class in _HIGH_RISK_CLASSES or (self.risk_category or "") in _HIGH_RISK_CLASSES


class WitnessPolicyOutcome(BaseModel):
    profile: ProtectionMode
    evidence_mode: EvidenceMode = "standard"
    concerns: list[WitnessConcern] = Field(default_factory=list)
    require_approval: bool = False
    should_block: bool = False
    block_reason: str | None = None


class WitnessPolicyEngine:
    def evaluate_session(self, session: WitnessSessionContext) -> WitnessPolicyOutcome:
        outcome = WitnessPolicyOutcome(profile=session.profile)
        if session.profile == "confidential":
            outcome.evidence_mode = "restricted"
            if session.shared_takeover_surface:
                outcome.concerns.append(
                    WitnessConcern(
                        code="shared_takeover_surface",
                        severity="high",
                        summary="Confidential sessions should avoid a shared takeover surface.",
                    )
                )
            if session.shared_browser_process:
                outcome.concerns.append(
                    WitnessConcern(
                        code="shared_browser_process",
                        severity="high",
                        summary="Confidential sessions should avoid a shared browser process.",
                    )
                )
        elif not session.auth_state_encrypted:
            outcome.concerns.append(
                WitnessConcern(
                    code="auth_state_unencrypted",
                    severity="warn",
                    summary="Auth state is not encrypted; Witness will track this as an operational concern.",
                )
            )
        return outcome

    def evaluate_action(
        self,
        *,
        session: WitnessSessionContext,
        action: WitnessActionContext,
    ) -> WitnessPolicyOutcome:
        outcome = WitnessPolicyOutcome(profile=session.profile)
        if action.sensitive_input or action.stores_auth_material or action.is_high_risk:
            outcome.evidence_mode = "restricted"

        if action.sensitive_input:
            outcome.concerns.append(
                WitnessConcern(
                    code="sensitive_input",
                    severity="warn" if session.profile == "normal" else "high",
                    summary="Sensitive input was used; receipt evidence is restricted.",
                )
            )
        if action.stores_auth_material:
            outcome.concerns.append(
                WitnessConcern(
                    code="auth_material_handling",
                    severity="warn" if session.profile == "normal" else "high",
                    summary="This action stores or moves authentication material.",
                )
            )
        if action.runtime_requires_approval:
            outcome.require_approval = True

        if session.profile == "normal":
            if action.is_high_risk and self._is_anonymous(session.operator):
                outcome.concerns.append(
                    WitnessConcern(
                        code="operator_missing",
                        severity="warn",
                        summary="High-risk action ran without a named operator identity.",
                    )
                )
            if action.is_high_risk and session.shared_takeover_surface:
                outcome.concerns.append(
                    WitnessConcern(
                        code="shared_takeover_surface",
                        severity="warn",
                        summary="High-risk action ran on a shared takeover surface.",
                    )
                )
            return outcome

        if action.is_high_risk:
            outcome.require_approval = True

        if action.is_high_risk and self._is_anonymous(session.operator):
            outcome.concerns.append(
                WitnessConcern(
                    code="operator_missing",
                    severity="critical",
                    summary="Confidential high-risk action requires a named operator identity.",
                    enforced=True,
                )
            )
            outcome.should_block = True
            outcome.block_reason = "Confidential high-risk actions require a named operator identity."
        if action.is_high_risk and session.shared_takeover_surface:
            outcome.concerns.append(
                WitnessConcern(
                    code="shared_takeover_surface",
                    severity="critical",
                    summary="Confidential high-risk action cannot run on a shared takeover surface.",
                    enforced=True,
                )
            )
            outcome.should_block = True
            outcome.block_reason = "Confidential high-risk actions require an isolated takeover surface."
        if action.is_high_risk and session.shared_browser_process:
            outcome.concerns.append(
                WitnessConcern(
                    code="shared_browser_process",
                    severity="critical",
                    summary="Confidential high-risk action cannot run in a shared browser process.",
                    enforced=True,
                )
            )
            outcome.should_block = True
            outcome.block_reason = "Confidential high-risk actions require isolated browser execution."
        if action.stores_auth_material and not session.auth_state_encrypted:
            outcome.concerns.append(
                WitnessConcern(
                    code="auth_state_unencrypted",
                    severity="critical",
                    summary="Confidential auth material handling requires encrypted auth state.",
                    enforced=True,
                )
            )
            outcome.should_block = True
            outcome.block_reason = "Confidential auth material handling requires encrypted auth state."
        return outcome

    @staticmethod
    def redact_target(target: dict[str, Any], *, evidence_mode: EvidenceMode) -> dict[str, Any]:
        if evidence_mode != "restricted":
            return dict(target)
        redacted = dict(target)
        for key in ("text", "text_preview", "file_path", "password", "totp_secret"):
            if key in redacted:
                redacted[key] = "[REDACTED]"
        return redacted

    @staticmethod
    def _is_anonymous(operator: OperatorIdentity) -> bool:
        return (operator.id or "anonymous").strip() in {"", "anonymous"}


class WitnessRecorder:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._lock = asyncio.Lock()
        self._heads: dict[str, str] = {}

    async def startup(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    async def record(self, scope: str, **kwargs) -> WitnessReceipt:
        receipt = WitnessReceipt(
            receipt_id=kwargs.pop("receipt_id", uuid4().hex[:12]),
            timestamp=kwargs.pop("timestamp", utc_now()),
            scope=scope,
            **kwargs,
        )
        return await self.append(scope, receipt)

    async def append(self, scope: str, receipt: WitnessReceipt) -> WitnessReceipt:
        async with self._lock:
            path = self._path(scope)
            previous = self._heads.get(scope) or await asyncio.to_thread(self._read_last_hash, path)
            item = receipt.model_copy(deep=True)
            item.scope = scope
            item.chain_prev_hash = previous
            item.chain_hash = self._compute_hash(item)
            await asyncio.to_thread(self._append_text, path, item.model_dump_json() + "\n")
            self._heads[scope] = item.chain_hash
            return item

    async def list(self, scope: str, *, limit: int = 100) -> list[WitnessReceipt]:
        return await asyncio.to_thread(self._list_sync, self._path(scope), limit)

    def _path(self, scope: str) -> Path:
        return self.root / f"{scope}.jsonl"

    @staticmethod
    def _append_text(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)

    @staticmethod
    def _list_sync(path: Path, limit: int) -> list[WitnessReceipt]:
        if not path.exists():
            return []
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        items = [WitnessReceipt.model_validate_json(line) for line in lines[-max(0, limit) :]]
        items.reverse()
        return items

    @staticmethod
    def _read_last_hash(path: Path) -> str | None:
        if not path.exists():
            return None
        last = None
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                last = line
        if last is None:
            return None
        return WitnessReceipt.model_validate_json(last).chain_hash

    @staticmethod
    def _compute_hash(receipt: WitnessReceipt) -> str:
        canonical = json.dumps(
            receipt.chain_payload(),
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(f"{receipt.chain_prev_hash or ''}:{canonical}".encode("utf-8")).hexdigest()


class WitnessRemoteClient:
    def __init__(
        self,
        *,
        base_url: str | None,
        api_key: str | None = None,
        tenant_id: str | None = None,
        timeout_seconds: float = 0.75,
        verify_tls: bool = True,
    ):
        self.base_url = base_url.rstrip("/") if base_url else None
        self.api_key = api_key
        self.tenant_id = tenant_id
        self.timeout_seconds = timeout_seconds
        self.verify_tls = verify_tls
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    async def startup(self) -> None:
        if not self.enabled or self._client is not None:
            return
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            verify=self.verify_tls,
        )

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def healthz(self) -> dict[str, Any]:
        client = self._require_client()
        response = await client.get("/healthz", headers=self._headers())
        return self._parse_response(response, operation="health check")

    async def record(self, scope: str, payload: dict[str, Any]) -> dict[str, Any]:
        client = self._require_client()
        response = await client.post(
            f"/api/scopes/{scope}/receipts",
            json=payload,
            params=self._params(),
            headers=self._headers(),
        )
        return self._parse_response(response, operation=f"record witness receipt for scope {scope}")

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Witness remote client is not started")
        return self._client

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            return {}
        return {"X-Witness-Key": self.api_key}

    def _params(self) -> dict[str, Any] | None:
        if self.tenant_id is None:
            return None
        return {"tenant": self.tenant_id}

    @staticmethod
    def _parse_response(response: httpx.Response, *, operation: str) -> dict[str, Any]:
        if response.is_success:
            return response.json()
        detail: str
        try:
            payload = response.json()
        except Exception:
            payload = response.text
        detail = payload.get("detail") if isinstance(payload, dict) else str(payload)
        raise RuntimeError(
            f"Witness remote {operation} failed with HTTP {response.status_code}: {detail}"
        )
