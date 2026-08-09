from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .models import OperatorIdentity, ProtectionMode
from .utils import utc_now
from .witness_anchor import compare_to_anchor, exclusive_lock, read_anchor, read_tail_line, write_anchor
from .witness_signing import SIGNATURE_ALGORITHM

logger = logging.getLogger(__name__)

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
    # Ed25519 signature over chain_hash, and the signing key's id. Optional so
    # chains written before signing existed still parse and still verify.
    chain_signature: str | None = None
    signing_key_id: str | None = None

    def chain_payload(self) -> dict[str, Any]:
        # The signature fields are excluded because they are derived *from* the
        # hash — including them would be circular. Excluding them also keeps the
        # hash input byte-identical to what pre-signing receipts were hashed
        # over, so existing chains continue to verify unchanged.
        return self.model_dump(mode="json", exclude={"chain_hash", "chain_signature", "signing_key_id"})


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
    """Append-only, hash-chained receipt log (one JSONL file per scope).

    Chain integrity assumes a single writer process: the asyncio.Lock serializes
    concurrent appends within one event loop and self._heads caches each scope's
    head hash in memory. This holds for the supported deployment (uvicorn with a
    single worker — see controller/Dockerfile). Running multiple worker processes
    against the same witness_root would fork the chain (duplicate chain_prev_hash)
    and needs OS-level file locking or a shared store — out of scope until
    multi-worker is actually adopted.
    """

    def __init__(self, root: str | Path, *, signer: Any = None):
        self.root = Path(root)
        self._lock = asyncio.Lock()
        # Three scopes, because each covers what the next cannot: the asyncio
        # lock orders tasks on one event loop, this one orders threads inside
        # this process (appends run in a worker thread, and Windows byte-range
        # locks are owned per process, so two threads contending for the file
        # lock deadlock rather than queue), and the file lock orders processes.
        self._thread_lock = threading.Lock()
        self._heads: dict[str, str] = {}
        self.signer = signer

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
            item = receipt.model_copy(deep=True)
            item.scope = scope
            # Reading the head, signing, and appending are one critical section
            # under a lock on the chain file. The asyncio lock above only orders
            # this process; two workers on a shared volume could otherwise read
            # the same head and write two receipts claiming the same
            # predecessor, forking the chain.
            item = await asyncio.to_thread(self._append_locked, path, item)
            self._heads[scope] = item.chain_hash
            return item

    def _append_locked(self, path: Path, item: WitnessReceipt) -> WitnessReceipt:
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock, open(path, "a+", encoding="utf-8") as handle, exclusive_lock(handle):
            last_line = read_tail_line(handle)
            item.chain_prev_hash = WitnessReceipt.model_validate_json(last_line).chain_hash if last_line else None
            previous_count = self._previous_count(path, handle, item.chain_prev_hash)
            item.chain_hash = self._compute_hash(item)
            if self.signer is not None:
                try:
                    item.chain_signature = self.signer.sign(item.chain_hash)
                    item.signing_key_id = self.signer.key_id
                except Exception:
                    # An unsigned receipt is worth far more than a lost one, so
                    # signing failure degrades rather than drops the record —
                    # but loudly, because a silently unsigned chain is the exact
                    # false assurance signing exists to remove. verify() reports
                    # unsigned receipts, so the gap stays visible downstream.
                    logger.exception("witness: failed to sign receipt %s; recording it unsigned", item.receipt_id)
            handle.seek(0, os.SEEK_END)
            handle.write(item.model_dump_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            write_anchor(path, head_hash=item.chain_hash, receipt_count=previous_count + 1)
        return item

    @staticmethod
    def _previous_count(path: Path, handle: Any, previous_hash: str | None) -> int:
        """How many receipts precede this one.

        Read through the already-open handle, never a second one: a Windows
        byte-range lock excludes every other handle, including this process's
        own, so reopening the chain inside the lock fails with EACCES.

        The anchor supplies the count in the steady state; a full count is only
        needed when the anchor is missing or stale, which is a chain written
        before this release or one appended to by an older build.
        """
        if previous_hash is None:
            return 0
        anchor = read_anchor(path)
        if anchor and anchor.get("head_hash") == previous_hash and isinstance(anchor.get("receipt_count"), int):
            return anchor["receipt_count"]
        handle.seek(0)
        return sum(1 for line in handle if line.strip())

    async def list(self, scope: str, *, limit: int = 100) -> list[WitnessReceipt]:
        return await asyncio.to_thread(self._list_sync, self._path(scope), limit)

    async def verify(self, scope: str) -> dict[str, Any]:
        """Walk a scope's full receipt chain and verify it end to end.

        Recomputes every chain hash and checks each receipt's chain_prev_hash
        against its predecessor, so deletion, reordering, insertion, and
        content edits all surface as the first divergent index.
        """
        return await asyncio.to_thread(self._verify_sync, scope, self._path(scope))

    async def verify_signatures(self, scope: str, *, public_key_b64: str | None = None) -> dict[str, Any]:
        """Verify every receipt signature in a scope against a public key.

        Separate from verify() because they answer different questions: verify()
        asks "is this chain internally consistent?", which anyone can forge by
        recomputing hashes. This asks "did the holder of this key attest to it?",
        which they cannot.
        """
        key = public_key_b64 or (self.signer.public_key_b64 if self.signer is not None else None)
        return await asyncio.to_thread(self._verify_signatures_sync, scope, self._path(scope), key)

    @staticmethod
    def _verify_signatures_sync(scope: str, path: Path, public_key_b64: str | None) -> dict[str, Any]:
        from .witness_signing import verify_signature

        result: dict[str, Any] = {
            "scope": scope,
            "valid": True,
            "checked": 0,
            "signed": 0,
            "unsigned": 0,
            "public_key_b64": public_key_b64,
            "first_invalid_index": None,
            "first_invalid_receipt_id": None,
            "reason": None,
        }
        if public_key_b64 is None:
            result["valid"] = False
            result["reason"] = "no public key available to verify against"
            return result
        if not path.exists():
            return result

        for index, line in enumerate(line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()):
            receipt = WitnessReceipt.model_validate_json(line)
            result["checked"] = index + 1
            if not receipt.chain_signature:
                result["unsigned"] += 1
                continue
            if not verify_signature(
                public_key_b64=public_key_b64,
                chain_hash=receipt.chain_hash or "",
                signature_b64=receipt.chain_signature,
            ):
                result["valid"] = False
                result["first_invalid_index"] = index
                result["first_invalid_receipt_id"] = receipt.receipt_id
                result["reason"] = "signature does not verify against the supplied public key"
                return result
            result["signed"] += 1
        return result

    async def export_bundle(self, scope: str) -> dict[str, Any]:
        """A self-contained, third-party-verifiable evidence bundle.

        Receipts alone prove nothing off-box. This packages them with the head
        hash, the public key, and the algorithm, so a recipient with
        `scripts/verify_witness_bundle.py` — which imports nothing from this
        project — can check the chain and its signatures independently.
        """
        receipts = await asyncio.to_thread(self._read_all_sync, self._path(scope))
        chain = await self.verify(scope)
        return {
            "format": "auto-browser-witness-bundle",
            "version": 1,
            "scope": scope,
            "exported_at": utc_now(),
            "algorithm": SIGNATURE_ALGORITHM,
            "signing_key_id": self.signer.key_id if self.signer is not None else None,
            "public_key_b64": self.signer.public_key_b64 if self.signer is not None else None,
            "receipt_count": len(receipts),
            "head_hash": chain.get("head_hash"),
            "chain_valid_at_export": chain.get("valid"),
            "receipts": [r.model_dump(mode="json") for r in receipts],
        }

    @staticmethod
    def _read_all_sync(path: Path) -> list[WitnessReceipt]:
        if not path.exists():
            return []
        return [
            WitnessReceipt.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    @classmethod
    def _verify_sync(cls, scope: str, path: Path) -> dict[str, Any]:
        result: dict[str, Any] = {
            "scope": scope,
            "valid": True,
            "receipt_count": 0,
            "head_hash": None,
            "signed_count": 0,
            "unsigned_count": 0,
            "first_invalid_index": None,
            "first_invalid_receipt_id": None,
            "reason": None,
        }
        if not path.exists():
            return result

        previous: str | None = None
        index = -1
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            index += 1
            try:
                receipt = WitnessReceipt.model_validate_json(line)
            except Exception:
                return cls._verify_failure(result, index, None, "receipt is not parseable")
            if receipt.chain_prev_hash != previous:
                return cls._verify_failure(
                    result,
                    index,
                    receipt.receipt_id,
                    "chain_prev_hash does not match the preceding receipt (chain reordered, truncated, or forked)",
                )
            if cls._compute_hash(receipt) != receipt.chain_hash:
                return cls._verify_failure(
                    result,
                    index,
                    receipt.receipt_id,
                    "chain_hash does not match receipt content (receipt altered after write)",
                )
            previous = receipt.chain_hash
            result["receipt_count"] = index + 1
            result["head_hash"] = receipt.chain_hash
            # Surfaced so an unsigned chain cannot masquerade as a verified one:
            # "valid" here only ever meant internally consistent.
            if receipt.chain_signature:
                result["signed_count"] += 1
            else:
                result["unsigned_count"] += 1

        # Signing cannot detect tail truncation: drop the last k receipts and
        # what remains is a shorter chain whose signatures all still verify. The
        # anchor is a separate record of where the chain had got to, so a short
        # chain stops being indistinguishable from a complete one.
        anchor = compare_to_anchor(
            path,
            head_hash=result["head_hash"],
            receipt_count=result["receipt_count"],
        )
        result["anchor"] = anchor
        if anchor["status"] in {"truncated", "diverged"}:
            result["valid"] = False
            result["reason"] = anchor.get("reason")
        return result

    @staticmethod
    def _verify_failure(
        result: dict[str, Any],
        index: int,
        receipt_id: str | None,
        reason: str,
    ) -> dict[str, Any]:
        result["valid"] = False
        result["first_invalid_index"] = index
        result["first_invalid_receipt_id"] = receipt_id
        result["reason"] = reason
        return result

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
        # limit <= 0 returns nothing. It used to slice `lines[-0:]`, which is the
        # whole list — so a caller passing a computed limit that reached zero got
        # the entire receipt chain dumped into a response instead of an empty page.
        if limit <= 0:
            return []
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        items = [WitnessReceipt.model_validate_json(line) for line in lines[-limit:]]
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
        raise RuntimeError(f"Witness remote {operation} failed with HTTP {response.status_code}: {detail}")
