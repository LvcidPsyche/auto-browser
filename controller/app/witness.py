from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .models import OperatorIdentity, ProtectionMode
from .utils import SECRET_KEY_FRAGMENTS, atomic_write_text, record_path, utc_now, without_lone_surrogates
from .witness_anchor import (
    anchor_fields,
    anchor_path,
    chain_records,
    compare_to_anchor,
    exclusive_lock,
    read_anchor,
    read_records,
    read_tail_line,
    write_anchor,
)
from .witness_signing import SIGNATURE_ALGORITHM

logger = logging.getLogger(__name__)

# Written in the witness root once the startup pass has anchored the legacy chains.
ANCHORS_MIGRATED_MARKER = ".anchors-migrated"


class ReceiptList(list):
    """Receipts read from a chain file, with how many lines were skipped because they did not parse.

    For a listing that is the lines in the listed window (the last `limit` lines), not the whole file: a page
    holding a bad line has fewer than `limit` receipts. An export counts the whole chain.

    A torn tail (see `_chain_lines`) is skipped by both and not counted; `torn_tail` says one was."""

    unparseable_lines: int = 0
    torn_tail: bool = False


def _parse_receipts(lines: list[str], path: Path) -> ReceiptList:
    """The receipts among a chain's lines. A line that does not parse is skipped and counted, never fatal:
    one bad line hid every good receipt. `verify` still fails on the gap."""
    receipts = ReceiptList()
    for line in lines:
        try:
            receipts.append(WitnessReceipt.model_validate_json(line))
        except ValueError:
            receipts.unparseable_lines += 1
    if receipts.unparseable_lines:
        logger.warning("witness: %s: skipped %d unparseable receipt line(s)", path.name, receipts.unparseable_lines)
    return receipts


def _chain_lines(data: bytes) -> tuple[list[str], bool]:
    """A chain's records without a torn tail, and whether it had one.

    A torn tail is bytes after the last newline that do not parse as a receipt: an append cut short, which the
    next append sets aside (`_set_aside_torn_tail`). It was never a receipt (nothing chains to it and the anchor
    never named it), so list and export both skip it and neither counts it as an unparseable line."""
    lines = chain_records(data)
    unterminated = data[data.rfind(b"\n") + 1 :]
    if unterminated.strip() and not _is_receipt(unterminated):
        lines.pop()
        return lines, True
    return lines, False


def _chain_hash_of(record: str) -> str | None:
    try:
        return WitnessReceipt.model_validate_json(record).chain_hash
    except ValueError:
        return None


def _is_receipt(segment: bytes) -> bool:
    """Whether bytes parse as a receipt. Whether its hashes hold is verify's question."""
    records = chain_records(segment)
    if len(records) != 1:
        return False
    try:
        WitnessReceipt.model_validate_json(records[0])
    except ValueError:
        return False
    return True


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

REDACTED = "[REDACTED]"
# Withheld from a restricted receipt by name, at any depth, beside a secret-named key (SECRET_KEY_FRAGMENTS): what an
# action typed or uploaded, and the free text an agent or a tool call wrote (an approval's reason previews the call's
# arguments; a takeover's is the agent's own words). The approval store keeps an approval's reason, and the audit log
# a takeover's.
_RESTRICTED_KEYS = frozenset({"text", "text_preview", "file_path", "password", "totp_secret", "reason"})
# What a select_option chose (by value, label or index); a None is another way of choosing, kept so the receipt shows
# which was used.
_CHOICE_KEYS = frozenset({"value", "label", "index"})


def _redact_restricted(value: Any) -> Any:
    """A copy of a restricted receipt's target or metadata with its secrets and typed text withheld, at any depth."""
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            name = str(key).lower()
            withheld = name in _RESTRICTED_KEYS or any(fragment in name for fragment in SECRET_KEY_FRAGMENTS)
            if withheld or (name in _CHOICE_KEYS and item is not None):
                redacted[key] = REDACTED
            else:
                redacted[key] = _redact_restricted(item)
        return redacted
    if isinstance(value, (list, tuple)):
        return [_redact_restricted(item) for item in value]
    return value


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
        # The session's own mode first: a confidential profile restricts the evidence of every action.
        outcome.evidence_mode = self.evaluate_session(session).evidence_mode
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
        """A receipt's target or metadata as the evidence mode keeps it (a copy either way)."""
        if evidence_mode != "restricted":
            return dict(target)
        return _redact_restricted(target)

    @staticmethod
    def redact_approval(approval: WitnessApproval, *, evidence_mode: EvidenceMode) -> WitnessApproval:
        """A receipt's approval as the evidence mode keeps it: restricted withholds the reason, and a None stays."""
        if evidence_mode != "restricted" or approval.reason is None:
            return approval.model_copy()
        return approval.model_copy(update={"reason": REDACTED})

    @staticmethod
    def _is_anonymous(operator: OperatorIdentity) -> bool:
        return (operator.id or "anonymous").strip() in {"", "anonymous"}


@dataclass
class _SharedLock:
    """A lock and how many holders and waiters it has, so it is dropped when it has none."""

    lock: Any
    users: int = 0


class _ChainThreadLocks:
    """One threading.Lock per chain file, dropped once no thread holds or waits for it.

    Per file, so appends to different chains run side by side. Two threads on one file still queue here, which is
    what Windows needs: its byte-range locks are owned per process, so two of this process's threads contending for
    the file lock deadlock rather than queue. The key is the path as the file system compares it (case-folded on
    Windows), so two spellings of one file share a lock."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, _SharedLock] = {}

    @contextmanager
    def hold(self, path: Path) -> Iterator[None]:
        key = os.path.normcase(os.path.abspath(path))
        with self._guard:
            shared = self._locks.setdefault(key, _SharedLock(threading.Lock()))
            shared.users += 1
        try:
            with shared.lock:
                yield
        finally:
            with self._guard:
                shared.users -= 1
                if not shared.users:
                    del self._locks[key]


class WitnessRecorder:
    """Append-only, hash-chained receipt log (one JSONL file per scope).

    Chain integrity assumes a single writer process: a scope's asyncio lock
    orders its appends within one event loop (other scopes' appends run beside
    them) and self._heads caches each scope's head hash in memory. This holds for
    the supported deployment (uvicorn with a single worker — see
    controller/Dockerfile). Running multiple worker processes against the same
    witness_root would fork the chain (duplicate chain_prev_hash) and needs
    OS-level file locking or a shared store — out of scope until multi-worker is
    actually adopted.
    """

    def __init__(self, root: str | Path, *, signer: Any = None):
        self.root = Path(root)
        # Three kinds of lock, because each covers what the next cannot: a
        # scope's asyncio lock orders its tasks on one event loop, first come
        # first served; a chain file's thread lock orders threads inside this
        # process (appends run in a worker thread; see _ChainThreadLocks); and
        # the file lock orders processes. The first two are per scope and per
        # file, so one session's appends never wait for another's.
        self._scope_locks: dict[str, _SharedLock] = {}
        self._thread_locks = _ChainThreadLocks()
        self._heads: dict[str, str] = {}
        self.signer = signer

    @asynccontextmanager
    async def _scope_lock(self, scope: str) -> AsyncIterator[None]:
        """Hold the scope's asyncio lock; it is dropped once no task holds or waits for it."""
        shared = self._scope_locks.setdefault(scope, _SharedLock(asyncio.Lock()))
        shared.users += 1
        try:
            async with shared.lock:
                yield
        finally:
            shared.users -= 1
            if not shared.users:
                del self._scope_locks[scope]

    async def startup(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._anchor_legacy_chains)

    def _anchor_legacy_chains(self) -> None:
        """Anchor every chain that has none, once, as it stands.

        Chains written before anchors existed have no anchor, and a missing one
        fails verification. The first start that finds the root unmarked anchors
        them as they are (whatever was lost from them earlier cannot be known) and
        marks the root. After that no start anchors anything: an anchor that
        disappears later is evidence, and re-anchoring on restart would turn a
        cut chain back into a valid one.
        """
        marker = self.root / ANCHORS_MIGRATED_MARKER
        if marker.exists():
            return
        for path in sorted(self.root.glob("*.jsonl")):
            if anchor_path(path).exists():
                continue
            try:
                self._anchor_chain(path)
            except Exception:
                logger.exception("witness: could not anchor legacy chain %s; it will fail verification", path)
        atomic_write_text(marker, utc_now())

    def _anchor_chain(self, path: Path) -> None:
        with self._thread_locks.hold(path), open(path, "a+", encoding="utf-8") as handle, exclusive_lock(handle):
            if anchor_path(path).exists():  # another worker anchored it first
                return
            self._set_aside_torn_tail(path, handle)
            records = read_records(handle)
            if not records:
                return
            head_hash = WitnessReceipt.model_validate_json(records[-1]).chain_hash
            write_anchor(path, head_hash=head_hash, receipt_count=len(records))

    def has_evidence(self, scope: str) -> bool:
        """Whether a chain file or an anchor exists for the scope."""
        path = self._path(scope)
        return path.exists() or anchor_path(path).exists()

    async def record(self, scope: str, **kwargs) -> WitnessReceipt:
        receipt = WitnessReceipt(
            receipt_id=kwargs.pop("receipt_id", uuid4().hex[:12]),
            timestamp=kwargs.pop("timestamp", utc_now()),
            scope=scope,
            **kwargs,
        )
        return await self.append(scope, receipt)

    async def append(self, scope: str, receipt: WitnessReceipt) -> WitnessReceipt:
        async with self._scope_lock(scope):
            path = self._path(scope)
            # A copy, its lone surrogates replaced before it is hashed: UTF-8 cannot encode one, so the receipt
            # could not be written, and the action it records had already run.
            item = WitnessReceipt.model_validate(without_lone_surrogates(receipt.model_dump()))
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
        with self._thread_locks.hold(path), open(path, "a+", encoding="utf-8") as handle, exclusive_lock(handle):
            self._set_aside_torn_tail(path, handle)
            last_line = read_tail_line(handle)
            item.chain_prev_hash = WitnessReceipt.model_validate_json(last_line).chain_hash if last_line else None
            anchored_count = self._next_anchor_count(path, handle, item.chain_prev_hash)
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
            if anchored_count is not None:
                write_anchor(path, head_hash=item.chain_hash, receipt_count=anchored_count)
        return item

    @staticmethod
    def _set_aside_torn_tail(path: Path, handle: Any) -> None:
        """Move an unfinished last record out of the chain before appending.

        Every receipt is written as one line ending in a newline and fsynced, so
        bytes after the last newline are an append that never completed (a crash
        or a full disk mid-write). Parsing that fragment as the chain head made
        every later append for the scope raise, so the session could record
        nothing more. The fragment was never a receipt: nothing chained to it
        and the anchor never named it. It is kept beside the chain for
        inspection, not deleted, and the chain continues from the last complete
        receipt. A complete last line that does not parse is not a torn write,
        and still fails closed.

        A last segment that parses as a receipt is a record that lost only its
        newline (or kept a dangling carriage return): it is terminated, not set
        aside, which is how verify, list and export read it too.
        """
        handle.flush()
        raw = handle.buffer
        raw.seek(0, os.SEEK_END)
        size = raw.tell()
        if size == 0:
            return
        raw.seek(size - 1)
        if raw.read(1) == b"\n":
            return
        raw.seek(0)
        data = raw.read()
        keep = data.rfind(b"\n") + 1
        if _is_receipt(data[keep:]) or not data[keep:].strip():
            raw.seek(0, os.SEEK_END)
            raw.write(b"\n")
            raw.flush()
            os.fsync(raw.fileno())
            handle.seek(0, os.SEEK_END)
            return
        torn = path.with_name(f"{path.name}.torn-{uuid4().hex[:12]}")
        torn.write_bytes(data[keep:])
        raw.truncate(keep)
        raw.flush()
        os.fsync(raw.fileno())
        handle.seek(0, os.SEEK_END)
        logger.error(
            "witness: chain %s ended in an incomplete record (%d bytes); moved it to %s and continued",
            path,
            size - keep,
            torn.name,
        )

    @staticmethod
    def _next_anchor_count(path: Path, handle: Any, previous_hash: str | None) -> int | None:
        """The receipt count the anchor records after this append, or None to leave it.

        An append advances an anchor only over a chain that still extends what the
        anchor describes. Writing it unconditionally turned a truncated chain into
        a valid one with the next receipt, and a deleted anchor into a fresh one.
        When the anchor is left as it is, verify keeps reporting the chain as
        truncated, diverged, or missing its anchor, and the receipt is recorded
        all the same: evidence is never dropped.

        Read through the already-open handle, never a second one: a Windows
        byte-range lock excludes every other handle, including this process's
        own, so reopening the chain inside the lock fails with EACCES.
        """
        fields = anchor_fields(read_anchor(path))
        if previous_hash is None:
            if not anchor_path(path).exists():
                return 1  # a new scope
            reason = "the chain is empty but its anchor remains"
        elif fields is None:
            reason = "its anchor is missing or unreadable"
        else:
            head, count = fields
            if head == previous_hash:
                # O(1) on purpose: a chain missing a middle receipt still fails
                # verify's hash walk, whatever the anchor then says.
                return count + 1
            records = read_records(handle)
            # The chain may be one receipt ahead of the anchor: a crash after the
            # receipt was written and before its anchor was.
            if len(records) >= count and _chain_hash_of(records[count - 1]) == head:
                return len(records) + 1
            reason = "the chain no longer extends its anchor"
        logger.warning("witness: %s: %s; recording the receipt and leaving the anchor as it is", path.name, reason)
        return None

    async def list(self, scope: str, *, limit: int = 100) -> ReceiptList:
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

    async def verify_with_signatures(self, scope: str) -> dict[str, Any]:
        """verify() and verify_signatures() under one verdict.

        `valid` passes only when the hash walk, the anchor and the signatures all
        do, so a client that reads one field cannot accept a chain whose
        signatures were stripped. `chain_valid` keeps the hash-walk verdict alone.
        """
        chain = await self.verify(scope)
        signatures = await self.verify_signatures(scope)
        result = {**chain, "chain_valid": chain["valid"], "signatures": signatures}
        if chain["valid"] and not signatures["valid"]:
            result["valid"] = False
            result["reason"] = f"signatures: {signatures['reason']}"
        return result

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

        for index, line in enumerate(chain_records(path.read_bytes())):
            try:
                receipt = WitnessReceipt.model_validate_json(line)
            except ValueError:
                result["valid"] = False
                result["first_invalid_index"] = index
                result["reason"] = "receipt is not parseable"
                return result
            result["checked"] = index + 1
            if not receipt.chain_signature:
                result["unsigned"] += 1
                if result["signed"]:
                    # Unsigned receipts before the first signed one predate
                    # signing and its hash covers them. One after it means a
                    # signature was removed — the forgery signing exists to stop.
                    result["valid"] = False
                    result["first_invalid_index"] = index
                    result["first_invalid_receipt_id"] = receipt.receipt_id
                    result["reason"] = "unsigned receipt after signed ones: a signature was removed"
                    return result
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

        if result["checked"] and not result["signed"]:
            # Stripping every signature and recomputing the hashes and the anchor
            # needs no key, only write access to the file, so a chain with no
            # signed receipt attests to nothing. The loop above only catches a
            # signature removed AFTER a signed one. Same rule, and wording, as
            # scripts/verify_witness_bundle.py.
            result["valid"] = False
            result["reason"] = (
                "no receipt is signed: the hashes are consistent, which anyone holding the receipts can produce"
            )
        return result

    async def export_bundle(self, scope: str) -> dict[str, Any]:
        """A self-contained, third-party-verifiable evidence bundle.

        Receipts alone prove nothing off-box. This packages them with the head
        hash, the public key, and the algorithm, so a recipient with
        `scripts/verify_witness_bundle.py` — which imports nothing from this
        project — can check the chain and its signatures independently.

        The receipts and the chain check come from one read of the chain: read
        twice, an append that landed in between gave a bundle whose head hash
        and verdict described a receipt its list did not hold.
        """
        path = self._path(scope)
        receipts, chain = await asyncio.to_thread(
            self._locked_read, path, lambda data: (self._receipts_of(path, data), self._verify_data(scope, path, data))
        )
        return {
            "format": "auto-browser-witness-bundle",
            "version": 1,
            "scope": scope,
            "exported_at": utc_now(),
            "algorithm": SIGNATURE_ALGORITHM,
            "signing_key_id": self.signer.key_id if self.signer is not None else None,
            "public_key_b64": self.signer.public_key_b64 if self.signer is not None else None,
            "receipt_count": len(receipts),
            # Lines that did not parse and are not in `receipts`: the bundle is incomplete when this is above 0,
            # and `chain_valid_at_export` is false because the chain has a gap there.
            "unparseable_lines": receipts.unparseable_lines,
            # True when the chain ended in a torn tail (an append cut short, never a receipt), which is skipped
            # as the listing skips it and not counted in `unparseable_lines`. The chain check still reads it as a
            # record that does not parse until the next append sets it aside, so `chain_valid_at_export` is false.
            "torn_tail": receipts.torn_tail,
            # None when a line was skipped: the chain stops at the first bad line, so its head would sit next
            # to a receipt list that has a gap, and look intact to a check that compares the two.
            "head_hash": None if receipts.unparseable_lines else chain.get("head_hash"),
            "chain_valid_at_export": chain.get("valid"),
            # The exporter's own check: why it failed, as verify's sentence ("chain holds 2 receipts but the
            # anchor records 3; ..."), and the anchor's status code (ok, anchor_missing, anchor_unreadable,
            # truncated, diverged). The reason is None when the check passed; the status is None when the check
            # stopped before the anchor (a record that does not parse, or a hash that does not match).
            "chain_reason_at_export": chain.get("reason"),
            "chain_anchor_status_at_export": (chain.get("anchor") or {}).get("status"),
            "receipts": [r.model_dump(mode="json") for r in receipts],
        }

    def _locked_read(self, path: Path, use: Callable[[bytes], Any]) -> Any:
        """``use`` applied to the chain's bytes, read once and used under the chain's thread and file locks.

        No append lands in between, nor between the chain and its anchor, which an
        append writes under the same locks. Read through the locked handle, never
        a second one: a Windows byte-range lock excludes every other handle, this
        process's own included. A chain that does not exist is read as empty, and
        is not created.
        """
        if not path.exists():
            return use(b"")
        with self._thread_locks.hold(path), open(path, "rb") as handle, exclusive_lock(handle):
            handle.seek(0)
            return use(handle.read())

    @staticmethod
    def _receipts_of(path: Path, data: bytes) -> ReceiptList:
        lines, torn_tail = _chain_lines(data)
        receipts = _parse_receipts(lines, path)
        receipts.torn_tail = torn_tail
        return receipts

    @classmethod
    def _verify_sync(cls, scope: str, path: Path) -> dict[str, Any]:
        return cls._verify_data(scope, path, path.read_bytes() if path.exists() else b"")

    @classmethod
    def _verify_data(cls, scope: str, path: Path, data: bytes) -> dict[str, Any]:
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
        lines = chain_records(data)

        previous: str | None = None
        index = -1
        for line in lines:
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
            return cls._verify_failure(result, None, None, anchor["reason"])
        if not result["receipt_count"]:
            # A session that was ever recorded has at least one receipt, so none
            # is missing evidence, and a chain of none attests to nothing.
            return cls._verify_failure(result, None, None, "no receipts recorded for this scope")
        if anchor["status"] != "ok":
            return cls._verify_failure(result, None, None, anchor["reason"])
        return result

    @staticmethod
    def _verify_failure(
        result: dict[str, Any],
        index: int | None,
        receipt_id: str | None,
        reason: str,
    ) -> dict[str, Any]:
        result["valid"] = False
        result["first_invalid_index"] = index
        result["first_invalid_receipt_id"] = receipt_id
        result["reason"] = reason
        return result

    def _path(self, scope: str) -> Path:
        return record_path(self.root, scope, ".jsonl")

    @staticmethod
    def _append_text(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)

    @staticmethod
    def _list_sync(path: Path, limit: int) -> ReceiptList:
        if not path.exists():
            return ReceiptList()
        # limit <= 0 returns nothing. It used to slice `lines[-0:]`, which is the
        # whole list — so a caller passing a computed limit that reached zero got
        # the entire receipt chain dumped into a response instead of an empty page.
        if limit <= 0:
            return ReceiptList()
        lines, torn_tail = _chain_lines(path.read_bytes())
        items = _parse_receipts(lines[-limit:], path)
        items.reverse()
        items.torn_tail = torn_tail
        return items

    @staticmethod
    def _compute_hash(receipt: WitnessReceipt) -> str:
        canonical = json.dumps(
            receipt.chain_payload(),
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(f"{receipt.chain_prev_hash or ''}:{canonical}".encode("utf-8")).hexdigest()


class WitnessRemoteError(RuntimeError):
    """An answer from the hosted Witness service that was not a success, with its HTTP status."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


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
        raise WitnessRemoteError(
            f"Witness remote {operation} failed with HTTP {response.status_code}: {detail}",
            status_code=response.status_code,
        )
