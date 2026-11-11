from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from .approval_backends import (
    DIGEST_KEY_FILE,
    ApprovalStoreBackend,
    FileApprovalStore,
    SQLiteApprovalStore,
    UnreadableRecord,
)
from .approval_display import UNBOUND_FIELDS, approval_provenance

# The execution side moved to approval_execution.py; importers keep reaching it here.
from .approval_execution import (
    _HELD_APPROVALS,
    ApprovalRefusal,
    ApprovalRequiredError,
    ClaimedApprovalError,
    ExecutionHold,
    ServerStateRefusal,
    _active_hold,
    _claim_conflict,
    _run_through,
    effect_started,
    executed_refusal,
    given_back_notes,
    held_action_approval,
    held_for_execution,
    held_status,
    held_tool_call_approval,
    refused_before_effect,
    spent_refusal,
)
from .models import (
    ApprovalDecider,
    ApprovalKind,
    ApprovalOrigin,
    ApprovalRecord,
    ApprovalStatus,
    BrowserActionDecision,
)
from .session_ownership import as_system, may_use, verified_operator
from .utils import UTC, utc_now

logger = logging.getLogger(__name__)

__all__ = [
    "CONTROLLER_SCOPE",
    "SENSITIVE_TEXT_PREFIX",
    "SENSITIVE_TEXT_RETENTION",
    "ApprovalRefusal",
    "ApprovalRequiredError",
    "ApprovalStore",
    "ClaimedApprovalError",
    "ServerStateRefusal",
    "ExecutionHold",
    "effect_started",
    "given_back_notes",
    "held_action_approval",
    "held_for_execution",
    "held_status",
    "held_tool_call_approval",
    "refused_before_effect",
    "spent_refusal",
]

# The scope of an approval that belongs to no session (a governed tool whose
# arguments name none). Session ids are uuid4().hex[:12], so this can never be one.
# There is no session owner to ask, so these approvals follow ApprovalRecord.owner.
CONTROLLER_SCOPE = "controller"
# SQLite's statuses the startup catch-up keeps over a file record of another status (_undoes_a_final_decision):
# a spend whatever the file record's time, an operator's rejection over one not later than it.
KEPT_OVER_THE_FILE = frozenset({"executed", "rejected"})
# What a sensitive `type` action's text is stored as in its approval.
SENSITIVE_TEXT_PREFIX = "[sensitive text] hmac-sha256:"
# How long the text of an undecided sensitive approval is kept in memory.
SENSITIVE_TEXT_RETENTION = timedelta(hours=24)
# A tool call's stand-in text as the gateway writes it (McpToolGateway._governed_call_decision), and as an approval
# keeps it (_keyed): the digest as an HMAC, after the id of the key it is under (_key_id).
_PLAIN_STAND_IN = re.compile(r"(\S+) sha256:[0-9a-f]{64}")
_KEYED_STAND_IN = re.compile(r"\S+ hmac-sha256:([0-9a-f]{8}):[0-9a-f]{64}")
# What a refusal says of a digest whose key this process does not hold (_kept_under_another_key).
_ANOTHER_KEY = (
    f"kept under another key than this controller holds ({DIGEST_KEY_FILE} in APPROVAL_ROOT was replaced, or a "
    "start could not read it)"
)
# What an approval of each origin authorises, as a refusal to spend it names it.
_ORIGIN_NAMES: dict[str, str] = {"action": "a browser action", "tool_call": "a tool call"}


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _key_id(key: bytes) -> str:
    """What names a tool call digest key in the records kept under it; it says nothing of the key."""
    return hmac.new(key, b"tool call digest key id", hashlib.sha256).hexdigest()[:8]


@dataclass(frozen=True)
class Decision:
    """What a decision did: the record, and what it did to a claim its action holds.

    ``deferred``: a reject put on a claim that has not begun, which stops the
    action. ``withdrawn``: an approve that took that reject back while the action
    may still start. ``withdrawn_after_refusal``: one that took it back once the
    action had already been refused (by that rejection, or before it began for
    another reason), so the approval goes back to approved without the action.
    ``withdrawn_spent``: one that took it back once the action had ended with an
    error that gives nothing back by itself, so the approval stays spent (call A).
    """

    record: ApprovalRecord
    on_claim: Literal["deferred", "withdrawn", "withdrawn_after_refusal", "withdrawn_spent"] | None = None


class ApprovalStoresUntrusted(RuntimeError):
    """Neither approval store can be trusted at a start: the file store still holds records that SQLite's spent
    or rejected ones could not be written over, and SQLite could not take the file store's other records."""

    def __init__(self, approval_ids: list[str]) -> None:
        super().__init__(
            "the approval store cannot start: the file store still holds records of approvals "
            f"{', '.join(approval_ids)} that SQLite's spent or rejected ones could not be written over, and SQLite "
            "could not take the file store's other records. Check that APPROVAL_ROOT and STATE_DB_PATH are writable, "
            "then start again"
        )


class ApprovalStore:
    def __init__(
        self,
        root: str | Path,
        db_path: str | None = None,
        *,
        approval_ttl_minutes: int = 15,
        retention_days: float = 0,
        refuse_self_approval: Iterable[str] = (),
        audit: Any = None,
        rejection_receipt: Callable[[ApprovalRecord], Awaitable[None]] | None = None,
        may_use: Callable[[ApprovalRecord], Awaitable[bool]] | None = None,
    ):
        self._lock = asyncio.Lock()
        # Operators who may not approve what they asked for (APPROVAL_REFUSE_SELF_APPROVAL_OPERATORS); empty, the
        # default and a bare store's, refuses nobody.
        self.refuse_self_approval = frozenset(refuse_self_approval)
        # Where a give-back is recorded (an AuditStore); None in a bare store.
        self.audit = audit
        # Records the witness receipt of an operator's rejection that a give-back
        # applied (the manager's); None in a bare store, which keeps no receipts.
        self.rejection_receipt = rejection_receipt
        # Whether the current operator may use an approval, which its session's owner decides (the manager's);
        # None in a bare store, which knows no session and goes by the operator the record names.
        self.may_use = may_use
        self.file_store = FileApprovalStore(root)
        self.sqlite_store = SQLiteApprovalStore(db_path) if db_path else None
        self._primary: ApprovalStoreBackend = self.file_store
        self.approval_ttl = timedelta(minutes=max(1, approval_ttl_minutes))
        # How long a decided approval is kept after its decision (prune_decided); 0 keeps them all, and is
        # what a bare store does. The controller passes APPROVAL_RETENTION_DAYS.
        self.retention_days = retention_days
        # What the retention has said once and does not repeat at every pass.
        self._retention_unreadable: set[str] = set()
        # The same for the arguments sweep, which runs at every create (_rejection_has_lapsed).
        self._sweep_unreadable: set[str] = set()
        self._retention_waits_for_sqlite = False
        # Keyed per process, so a stored digest cannot be brute-forced offline.
        # An approval for sensitive text therefore only works in the process
        # that created it: not after a restart, and not in another uvicorn
        # worker or replica. The action asks again rather than matching an
        # unverifiable record.
        self._sensitive_text_key = secrets.token_bytes(32)
        # The key a tool call's stand-in digest is kept under (_keyed): the file store's DIGEST_KEY_FILE, the same
        # at every start, read at startup or at first use (_digest_key).
        self._tool_call_key: bytes | None = None
        # Whether that key is this process's own, the file's being unusable at this start (_digest_key): the file's
        # key may then be the very one a record reads as kept under another key by.
        self._keyed_by_this_process_alone = False
        # The text itself stays in memory only, so an approved sensitive action
        # can still be run through execute_approval by this process.
        # Entries are dropped when the approval executes or is rejected, and
        # otherwise after SENSITIVE_TEXT_RETENTION, so text for an approval
        # nobody decides does not sit in memory until the process restarts.
        self._sensitive_texts: dict[str, tuple[str, float]] = {}
        # The live claims, by approval id, so an operator's reject can reach one
        # whose action has not started. In memory: a reject sent to another
        # worker or replica than the one running the action is refused as before.
        self._claims: dict[str, ExecutionHold] = {}

    async def startup(self) -> None:
        await self.file_store.startup()
        await asyncio.to_thread(self._digest_key)
        self._primary = self.file_store
        if self.sqlite_store is not None:
            try:
                await self.sqlite_store.startup()
            except Exception as exc:
                logger.warning("sqlite approval store unavailable, using file store fallback: %s", exc)
            else:
                try:
                    await self._catch_sqlite_up()
                except ApprovalStoresUntrusted:
                    raise
                except Exception as exc:
                    logger.warning(
                        "sqlite approval store could not catch up with the file store, using file store fallback: %r",
                        exc,
                    )
                else:
                    self._primary = self.sqlite_store
        await self._key_stored_digests()
        await self._sweep_arguments(startup=True)

    async def _catch_sqlite_up(self) -> None:
        """Copy into SQLite every file record it lacks or holds differently, before SQLite is primary again.

        Every write goes to the file store first, and to SQLite only while it is primary: a start whose
        SQLite failed ran on the file store alone, and an approval spent then (executed) still read approved
        in SQLite at the next start, so it ran twice.

        What holds within one process: writes under the lock land whole and in order (_persist), and one whose
        SQLite half fails puts the file record back (_write), so a file record that differs is a write SQLite
        missed, or one cut off by the process dying between the two. What does not: records stored by 1.11.1,
        several workers sharing the store (the lock is per process), and a restored backup can leave a file
        record that differs without being the latest word. So SQLite's executed record stays over a file record
        of another status whatever its time: a later one is a give-back SQLite never took (cut off by a crash,
        a write whose put-back failed too, several workers), and losing it leaves the approval spent, and the
        agent asks again. SQLite's rejected record stays over one not updated after it, and an approve after
        the reject, later than it (made while SQLite was unavailable), is caught up. A record SQLite keeps is
        written over the file's, so the two agree for a later start whose SQLite fails. The rule errs toward
        spent or rejected.

        The file store is repaired first, then SQLite takes the rest: a SQLite failure falls back to a file store
        already repaired. A repair that fails is logged and leaves SQLite primary; if SQLite then fails as well,
        neither store can be trusted, and the store refuses to start (ApprovalStoresUntrusted).
        """
        held = {record.id: record for record in await self.sqlite_store.list()}
        copies: list[ApprovalRecord] = []
        unrepaired: list[str] = []
        for record in await self.file_store.list():
            kept = held.get(record.id)
            if kept == record:
                continue
            if kept is None or not self._undoes_a_final_decision(kept, record):
                copies.append(record)
                continue
            logger.warning("approval %s: SQLite's %s record is kept over the file store's", record.id, kept.status)
            try:
                # And written over the file's: a start whose SQLite fails runs on the file store alone.
                await self.file_store.upsert(kept)
            except Exception as exc:
                logger.error(
                    "approval %s: SQLite's %s record could not be written over the file store's: %r",
                    record.id,
                    kept.status,
                    exc,
                )
                unrepaired.append(record.id)
        try:
            for record in copies:
                await self.sqlite_store.upsert(record)
        except Exception as exc:
            if unrepaired:
                raise ApprovalStoresUntrusted(unrepaired) from exc
            raise

    def _undoes_a_final_decision(self, kept: ApprovalRecord, record: ApprovalRecord) -> bool:
        """Whether ``record`` (the file's) would turn ``kept`` (SQLite's) to another status: a spend whatever
        the file record's time, a rejection when the file record is not later than it."""
        if kept.status not in KEPT_OVER_THE_FILE or record.status == kept.status:
            return False
        if kept.status == "executed":
            return True  # a spend is final at a start
        try:
            return self._parse_timestamp(record.updated_at) <= self._parse_timestamp(kept.updated_at)
        except (ValueError, OverflowError, OSError):
            # Neither record can be shown to be the later one, and the rule errs toward rejected. Raised from here,
            # the error ended the catch-up, and the start went on with the file records alone: all of them, this
            # one read by its file record. OSError: Windows, for a time with no offset before 1970 or far ahead.
            logger.warning(
                "approval %s: a time of its file or SQLite record cannot be read, so the rejection stands", record.id
            )
            return True

    async def _key_stored_digests(self) -> None:
        """At a start, rewrite each record that keeps a stand-in's plain digest with the keyed one, in both stores.

        Versions before 1.18.0 stored it plain. Nothing hands out the plain text (get and list key what they return:
        _handed_out), so this pass reads the backend itself to find it; left alone, the stores would hold it until
        each record's next write. Nothing but the text changes, so the stores agree as before (_catch_sqlite_up), and
        the same call still matches (_keyed). A record that cannot be rewritten stays, still matches, and is taken at
        the next start; the pass never stops one. A process that keys alone (_digest_key) rewrites none, and says how
        many it left: under its own key no later start could match them, so one start that could not use the key
        file ended every operator's rejection stored before 1.18.0 (the next good start's retention then removed it).

        Also says, once a start, how many approvals that could still run are kept under a key this process does not
        hold (_kept_under_another_key), and how many rejections by an operator: nothing else tells an operator
        before one of the first is refused, or that the others no longer match the call they refused
        (_operator_rejected).
        """
        try:
            async with self._lock:
                rewritten = 0
                failed: list[str] = []
                unreadable = 0
                rejections = 0
                for record in await self._primary.list():
                    keyed = self._keyed(record.action)
                    if keyed is record.action:
                        if self._kept_under_another_key(record.action):
                            if record.status in {"pending", "approved"}:
                                unreadable += 1
                            elif record.status == "rejected" and record.decided_via != "agent":
                                rejections += 1
                        continue
                    if self._keyed_by_this_process_alone:
                        # Read after _keyed, which reads the key: that is where a process learns that it keys alone.
                        failed.append(f"{record.id} (this start keys with a key of its own)")
                        continue
                    record.action = keyed
                    try:
                        await self._write(record)
                    except Exception as exc:
                        failed.append(f"{record.id} ({exc!r})")
                    else:
                        rewritten += 1
        except Exception:
            logger.exception("approval store: could not look for tool call digests an earlier version stored plain")
            return
        if rewritten:
            logger.info(
                "approval store: %d approval record(s) stored before 1.18.0 now keep their tool call digest keyed",
                rewritten,
            )
        if failed:
            logger.warning(
                "approval store: %d approval record(s) stored before 1.18.0 still keep a tool call digest unkeyed, "
                "as they could not be rewritten; the next start tries again. The first: %s",
                len(failed),
                failed[0],
            )
        # Two lines, each true alone: a start can find either kind without the other.
        if unreadable:
            logger.warning(
                "approval store: %d pending or approved approval(s) for tool calls are kept under another key than "
                "%s now holds (replaced since, or unreadable at an earlier start): none can be approved or run, and "
                "the agent asks again; reject them to end this warning",
                unreadable,
                self.file_store.root / DIGEST_KEY_FILE,
            )
        if rejections:
            logger.warning(
                "approval store: %d operator rejection(s) of tool calls are kept under another key than %s now holds "
                "(replaced since, or unreadable at an earlier start): they no longer stop an agent's own approval of "
                "the same call, which matters only under AUTONOMOUS_APPROVALS. Reject the call again when it is asked "
                "for, turn AUTONOMOUS_APPROVALS off, or restore the key file",
                rejections,
                self.file_store.root / DIGEST_KEY_FILE,
            )

    async def _sweep_arguments(self, *, startup: bool = False) -> None:
        """Remove the arguments no approve or call will read again: at each create (under the lock) and at startup.

        A rejected tool call's, once its rejection is older than the approval
        TTL: an operator may approve it again until then, and an approve after is
        refused for its arguments (_refuse_what_is_no_longer_whole). At startup
        also those of approvals spent, or gone, with no process left to: a
        call's release removes them once it spends the approval, and a crash or
        a restart mid-call left the record executed and its arguments readable
        for good.
        """
        for approval_id in await self.file_store.arguments_ids():
            try:
                approval: ApprovalRecord | None = await self.get(approval_id)
            except KeyError as exc:
                if isinstance(exc, UnreadableRecord):
                    if startup:
                        logger.warning("kept the arguments of approval %s: its record is unreadable", approval_id)
                    continue
                approval = None
            except Exception:
                if startup:
                    logger.warning(
                        "kept the arguments of approval %s: its record is unreadable", approval_id, exc_info=True
                    )
                continue
            if approval is None or approval.status == "executed":
                if startup:
                    await self.discard_arguments(approval_id)
            elif approval.status == "rejected" and self._rejection_has_lapsed(approval):
                await self.discard_arguments(approval_id)

    def _rejection_has_lapsed(self, approval: ApprovalRecord) -> bool:
        """Whether a rejection is older than the approval TTL, after which its tool call's arguments go.

        One whose decision time cannot be read has not: its arguments stay, and the record is named in the log once,
        as the retention does (_past_retention). Raised from here, the error left the sweep, and with it the start
        or the request for an approval the sweep ran in, for as long as the record existed.
        """
        if approval.decided_at is None:
            return False
        try:
            return self._parse_timestamp(approval.decided_at) + self.approval_ttl <= datetime.now(UTC)
        except (ValueError, OverflowError, OSError):
            # OSError: a time with no offset is read as local time, which Windows refuses before 1970 or far ahead.
            if approval.id not in self._sweep_unreadable:
                self._sweep_unreadable.add(approval.id)
                logger.warning("kept the arguments of approval %s: its decision time cannot be read", approval.id)
            return False

    async def prune_decided(self, *, open_sessions: Iterable[str] = ()) -> int:
        """Delete the approvals decided more than ``retention_days`` ago, from both stores: how many went.

        Run at startup and by each maintenance sweep. Kept whatever their age: a pending approval; one an
        execution holds; an approved one that has not expired, which can still run its action; an operator's
        rejection in controller scope or in a session of ``open_sessions`` (those live in this process), where
        it still refuses an agent's approval of the same request (_operator_rejected), unless it is a tool
        call's kept under a key that is gone, which matches no request (_kept_under_a_key_that_is_gone: never
        in a process that could not use the key file); and a record whose decision (or, approved, expiry) time
        cannot be read, named once in the log.

        Nothing goes while SQLite is configured and the file store stands in for it: SQLite is caught up from
        the file records at its next start, and a record removed meanwhile could not show that SQLite's copy of
        it is stale (pending there, rejected since).

        The pass runs under the lock and lands whole whatever happens to its caller (_run_through), as a write
        does. A failure is logged and answers 0, so a start or a sweep goes on, and the next pass takes what
        this one left.
        """
        if self.retention_days <= 0:
            return 0
        try:
            removed, cancelled = await _run_through(self._prune_decided(frozenset(open_sessions)))
        except Exception:
            logger.exception("the approval retention pass failed; the next one takes what it left")
            return 0
        if cancelled:
            raise asyncio.CancelledError
        return removed

    async def _prune_decided(self, open_sessions: frozenset[str]) -> int:
        async with self._lock:
            if self.sqlite_store is not None and self._primary is not self.sqlite_store:
                if not self._retention_waits_for_sqlite:
                    self._retention_waits_for_sqlite = True
                    logger.warning(
                        "approval retention removes nothing while SQLite is unavailable and the file store stands in"
                    )
                return 0
            now = datetime.now(UTC)
            past = [
                record.id for record in await self._primary.list() if self._past_retention(record, now, open_sessions)
            ]
            # The files first: a record whose file stays keeps its SQLite record too, so the two agree.
            removed = await self.file_store.remove_many(past)
            if self.sqlite_store is not None:
                await self.sqlite_store.remove_many(removed)
        if not removed:
            return 0
        logger.info(
            "approval retention removed %d approval(s) decided more than %g days ago", len(removed), self.retention_days
        )
        if self.audit is not None:
            try:
                with as_system():
                    await self.audit.append(
                        event_type="approvals_pruned",
                        status="ok",
                        action="retention",
                        details={"count": len(removed), "retention_days": self.retention_days},
                    )
            except Exception:
                logger.exception("could not audit the approval retention pass, which removed %d", len(removed))
        return len(removed)

    def _past_retention(self, record: ApprovalRecord, now: datetime, open_sessions: frozenset[str]) -> bool:
        """Whether the retention removes ``record``; prune_decided says what it keeps."""
        if record.status == "pending" or record.id in self._claims:
            return False
        if (
            record.status == "rejected"
            and record.decided_via != "agent"
            and (record.session_id == CONTROLLER_SCOPE or record.session_id in open_sessions)
            # Under a key that is gone it matches no call, so it refuses nothing: it goes as an agent's does.
            and not self._kept_under_a_key_that_is_gone(record.action)
        ):
            return False
        try:
            if record.status == "approved" and not self.has_expired(record):
                return False
            if record.decided_at is None:
                raise ValueError("no decided_at")
            age = now - self._parse_timestamp(record.decided_at)
        except (ValueError, OverflowError, OSError):
            # OSError: a time with no offset is read as local time, which Windows refuses before 1970 or far ahead.
            if record.id not in self._retention_unreadable:
                self._retention_unreadable.add(record.id)
                logger.warning(
                    "approval %s is kept by the retention: its decision or expiry time cannot be read", record.id
                )
            return False
        # Compared in seconds: a timedelta cannot hold more days than the calendar has.
        return age.total_seconds() > self.retention_days * 86400

    async def list(
        self,
        *,
        status: ApprovalStatus | None = None,
        session_id: str | None = None,
    ) -> list[ApprovalRecord]:
        return [self._handed_out(record) for record in await self._primary.list(status=status, session_id=session_id)]

    async def get(self, approval_id: str) -> ApprovalRecord:
        return self._handed_out(await self._primary.get(approval_id))

    def _handed_out(self, record: ApprovalRecord) -> ApprovalRecord:
        """``record`` as the store hands it out: a stand-in's digest keyed, whatever its stores still hold.

        A record stored before 1.18.0 that a start could not rewrite (_key_stored_digests) keeps its plain digest
        in the stores. Handed out as stored, it was listed and answered to the repeated call with that digest, and
        a decision, a claim or a give-back wrote it back plain. Nothing is written here: the next write of the
        record, or the next start, replaces what the stores hold. The passes that compare or rewrite what is
        stored read the backends themselves (_catch_sqlite_up, _key_stored_digests, _write). The record is changed
        in place: a backend reads one anew at every call, so no one else holds it.
        """
        record.action = self._keyed(record.action)
        return record

    async def create_or_reuse_pending(
        self,
        *,
        session_id: str,
        kind: ApprovalKind,
        reason: str,
        action: BrowserActionDecision,
        observation: dict | None = None,
        origin: ApprovalOrigin = "action",
        arguments: str | None = None,
    ) -> ApprovalRecord:
        """The pending approval of this action, or a new one; ``arguments`` is a tool call's whole arguments.

        They are written beside the record before it, so no listing shows an
        approval whose arguments cannot be read, and the record keeps their size
        and sha256 (the identical call reuses the record, and so its arguments).
        """
        async with self._lock:
            await self._sweep_arguments()
            owner = verified_operator()
            existing = await self._find_matching_pending(
                session_id=session_id, kind=kind, action=action, owner=owner, origin=origin
            )
            if existing is not None:
                if action.sensitive and action.text:
                    # Its digest matched, so this is the text it binds: held again from now, or an approval
                    # whose text aged out (SENSITIVE_TEXT_RETENTION) could never be approved.
                    self._prune_sensitive_texts()
                    self._sensitive_texts[existing.id] = (action.text or "", time.monotonic())
                return existing

            now = utc_now()
            approval = ApprovalRecord(
                id=uuid4().hex[:12],
                session_id=session_id,
                kind=kind,
                status="pending",
                created_at=now,
                updated_at=now,
                reason=reason,
                action=self._stored_action(action),
                observation=observation,
                owner=owner,
                origin=origin,
            )
            if arguments is not None:
                approval.arguments_size = len(arguments)
                # Off the event loop, as the gateway wrote them out: megabytes take a tenth of a second.
                approval.arguments_sha256 = await asyncio.to_thread(_text_sha256, arguments)
                await self.file_store.write_arguments(approval.id, arguments)
            await self._persist(approval)
            if self._needs_held_text(approval):
                self._prune_sensitive_texts()
                self._sensitive_texts[approval.id] = (action.text or "", time.monotonic())
            return approval

    async def approve(
        self,
        approval_id: str,
        comment: str | None = None,
        *,
        decided_via: ApprovalDecider = "operator",
    ) -> ApprovalRecord:
        return (await self.decide(approval_id, status="approved", comment=comment, decided_via=decided_via)).record

    async def reject(
        self,
        approval_id: str,
        comment: str | None = None,
        *,
        decided_via: ApprovalDecider = "operator",
    ) -> ApprovalRecord:
        return (await self.decide(approval_id, status="rejected", comment=comment, decided_via=decided_via)).record

    async def claim_execution(self, approval_id: str) -> ExecutionHold | None:
        """Spend an approved approval on the execution about to start, or refuse.

        An approval authorizes one action. It used to be reserved in memory and
        consumed only once the action had returned normally, so an action that
        went through and then failed (a later step, a captcha, a crash, a
        restart) left it approved, and the same approval ran the action again.
        The claim now marks it executed, persisted, before the action starts,
        so a second request carrying the same id is refused; the holder gives
        it back only when the action was refused before it changed anything
        (held_for_execution). Expiry is checked here, never after: an action
        that started in time is not refused once it has run.

        Returns the hold, which the caller releases in a finally; or None when
        the current task already holds it — an outer layer claimed the same
        approval and owns the execution. Each layer used to claim for itself,
        so the inner claim of a governed action failed and the action never ran.

        The write lands whatever happens to the caller, and the hold is made with
        it: a cancellation during the write is put on the hold (claim_cancelled),
        and held_for_execution ends the block as that cancellation. Raised there,
        it left the approval executed with no hold, no audit entry and nothing run.
        """
        if _active_hold(approval_id) is not None:
            return None
        async with self._lock:
            approval, cancelled = await self._mark_executed(approval_id)
            # Made with the write, under the lock: a decision that reads the record next finds the claim.
            hold = ExecutionHold(approval_id, approval.origin)
            hold.claim_cancelled = cancelled
            self._claims[approval_id] = hold
        _HELD_APPROVALS.set({**_HELD_APPROVALS.get(), approval_id: hold})
        return hold

    async def arguments(self, approval_id: str) -> str:
        """A tool call's whole arguments, while its approval is not final; KeyError once they are gone."""
        return await self.file_store.read_arguments(approval_id)

    async def discard_arguments(self, approval_id: str) -> None:
        """Remove a tool call's arguments once its approval is final; a failure is logged, never raised."""
        try:
            await self.file_store.delete_arguments(approval_id)
        except OSError:
            logger.warning("could not remove the arguments of approval %s", approval_id, exc_info=True)

    def revoke_execution(self, hold: ExecutionHold) -> None:
        """Stop a hold from counting, in every task carrying it: none of them can act under it now."""
        hold.active = False
        if self._claims.get(hold.approval_id) is hold:
            del self._claims[hold.approval_id]

    def release_execution(self, hold: ExecutionHold, *, spent: bool) -> None:
        """End a hold: a task still carrying it no longer counts as holding the approval."""
        self.revoke_execution(hold)
        held = _HELD_APPROVALS.get()
        if held.get(hold.approval_id) is hold:
            _HELD_APPROVALS.set({key: value for key, value in held.items() if key != hold.approval_id})
        if spent:
            # Kept through the claim, so that an approval given back can still be executed.
            self._sensitive_texts.pop(hold.approval_id, None)

    async def give_back(self, hold: ExecutionHold) -> ApprovalRecord | None:
        """Return a claimed approval: its action was refused before it changed anything.

        To approved, unless an operator rejected it while the claim waited: then
        to rejected, with their decision. None, with the record left executed,
        when only that reject made the ending give back and it has been withdrawn
        since: decided here, under the lock a withdraw takes, so the ending does
        not depend on whether the withdraw landed before or after the block ended.
        """
        async with self._lock:
            if hold.rejection is None and not hold.gives_back_on_its_own:
                return None
            approval = await self.get(hold.approval_id)
            now = self._later_than(approval.updated_at)
            approval.status = "approved"
            approval.updated_at = now
            approval.executed_at = None
            if hold.rejection is not None:
                approval.status = "rejected"
                approval.decision_comment, approval.decided_via, approval.decided_by = hold.rejection
                approval.decided_at = now
                approval.approved_expires_at = None
                self._sensitive_texts.pop(hold.approval_id, None)
            await self._persist(approval)
        return approval

    async def record_given_back(self, approval: ApprovalRecord) -> None:
        """Audit a give-back once its record is written: the claim persisted executed, this says why it is not.

        A give-back that applied an operator's rejection names their decision,
        and gets the witness receipt their reject would have had.

        A failure is logged and goes no further. The give-back stands, and the
        refusal it came with is what the agent needs: an audit error in its place
        hid that the approval was approved again.
        """
        rejected = approval.status == "rejected"
        details: dict[str, Any] = {"kind": approval.kind, **approval_provenance(approval)}
        if rejected:
            # Written by the execution's task, as its operator: decided_by names who rejected.
            details.update(
                comment=approval.decision_comment, decided_via=approval.decided_via, decided_by=approval.decided_by
            )
        if self.audit is not None:
            try:
                await self.audit.append(
                    event_type="approval_given_back",
                    status=approval.status,
                    action="give_back",
                    session_id=approval.session_id,
                    approval_id=approval.id,
                    details=details,
                )
            except Exception:
                logger.exception("could not audit the give-back of approval %s", approval.id)
        if rejected and self.rejection_receipt is not None:
            try:
                await self.rejection_receipt(approval)
            except Exception:
                logger.exception("could not record the witness receipt of the rejection of approval %s", approval.id)

    async def record_cancelled_claim(self, hold: ExecutionHold) -> None:
        """Audit a claim whose caller was cancelled while it was written, in a block that does not give back.

        Nothing ran, and the approval stays spent (a governed tool call's): the
        trail would otherwise end at the approve. A failure is logged, as for a give-back.
        """
        if self.audit is None:
            return
        try:
            approval = await self.get(hold.approval_id)
            await self.audit.append(
                event_type="approval_claim_cancelled",
                status="spent",
                action="claim",
                session_id=approval.session_id,
                approval_id=approval.id,
                details={"kind": approval.kind, **approval_provenance(approval)},
            )
        except Exception:
            logger.exception("could not audit the cancelled claim of approval %s", hold.approval_id)

    async def record_rejection_not_applied(self, hold: ExecutionHold) -> None:
        """Audit an operator's reject that waited on a claim whose execution ended spent all the same.

        The give-back that applies it did not complete (its write failed, or the
        process is stopping), so the approval stays spent; the trail would
        otherwise end at the "deferred" entry. A failure is logged, as for a give-back.
        """
        if self.audit is None or hold.rejection is None:
            return
        comment, decided_via, decided_by = hold.rejection
        try:
            approval = await self.get(hold.approval_id)
            await self.audit.append(
                event_type="approval_decision",
                status="not_applied",
                action="reject",
                session_id=approval.session_id,
                approval_id=approval.id,
                details={
                    "kind": approval.kind,
                    "comment": comment,
                    "decided_via": decided_via,
                    "decided_by": decided_by,
                    **approval_provenance(approval),
                },
            )
        except Exception:
            logger.exception("could not audit the rejection of approval %s that did not apply", hold.approval_id)

    async def mark_executed(self, approval_id: str) -> ApprovalRecord:
        async with self._lock:
            approval, cancelled = await self._mark_executed(approval_id)
        if cancelled:
            raise asyncio.CancelledError
        return approval

    async def _mark_executed(self, approval_id: str) -> tuple[ApprovalRecord, bool]:
        """Mark an approved approval executed, under the lock: the record, and whether the caller was cancelled.

        The write lands whatever happens to the caller (_run_through), and the
        cancellation is reported, not raised: one raised while the write was
        under way left the record executed and nothing to say so, an approval
        spent with nothing run.
        """
        approval = await self.get(approval_id)
        if approval.status == "executed":
            raise self.refusal_for_executed(approval)
        if approval.status != "approved":
            raise PermissionError(f"approval {approval_id} is not approved")
        self._ensure_not_expired(approval)
        now = utc_now()
        approval.status = "executed"
        approval.updated_at = now
        approval.executed_at = now
        # Run through here too, so the cancellation is reported: _persist would raise it once the write landed.
        _, cancelled = await _run_through(self._persist(approval))
        return approval, cancelled

    async def require_approved(
        self,
        *,
        approval_id: str,
        session_id: str,
        kind: ApprovalKind,
        action: BrowserActionDecision,
        origin: ApprovalOrigin = "action",
    ) -> ApprovalRecord:
        approval = await self.get(approval_id)
        if approval.session_id != session_id:
            # Presented in a session that is not its own. A caller who may not use it is answered as for an id no
            # record answers to, as the listing and the decisions answer: "does not belong" told an operator who
            # held another's id that it is an approval's.
            usable = may_use(approval.owner) if self.may_use is None else await self.may_use(approval)
            if not usable:
                raise KeyError(approval_id)
            raise PermissionError(f"approval {approval_id} does not belong to session {session_id}")
        # A controller-scope approval has no session whose owner could be asked,
        # so it is the asking operator's alone: another operator's identical call
        # hashes to the same action and would otherwise spend it. Answers like an
        # approval that does not exist, as the listing does.
        if session_id == CONTROLLER_SCOPE and not may_use(approval.owner):
            raise KeyError(approval_id)
        if approval.kind != kind:
            raise PermissionError(f"approval {approval_id} does not cover {kind}")
        if approval.origin != origin:
            raise PermissionError(
                f"approval {approval_id} is for {_ORIGIN_NAMES[approval.origin]}, not {_ORIGIN_NAMES[origin]}"
            )
        # Inside an execution that holds it, the claim has checked the status and
        # the expiry and spent it: a layer within accepts that, and an approval
        # that expires mid-run does not stop the run already under way.
        held = _active_hold(approval_id) is not None
        if approval.status == "executed" and not held:
            raise self.refusal_for_executed(approval)
        if approval.status != ("executed" if held else "approved"):
            raise PermissionError(f"approval {approval_id} is not approved")
        if not held:
            self._ensure_not_expired(approval)
        if not self._actions_match(approval.action, action):
            if self._kept_under_another_key(approval.action):
                # Not "does not match": the call may be the very one it was asked for.
                raise PermissionError(
                    f"approval {approval_id} can no longer be matched to a call: its arguments' digest is "
                    f"{_ANOTHER_KEY}; repeat the call without approval_id to ask for a new approval"
                )
            raise PermissionError(f"approval {approval_id} does not match the requested action")
        return approval

    def refusal_for_executed(self, approval: ApprovalRecord) -> PermissionError:
        """Why an executed approval cannot run again: spent, or held by an action that has not begun."""
        return executed_refusal(approval, self._claims.get(approval.id))

    async def decide(
        self,
        approval_id: str,
        *,
        status: ApprovalStatus,
        comment: str | None,
        decided_via: ApprovalDecider,
    ) -> Decision:
        """Approve or reject ``approval_id``, or, while its action holds it, put the decision on the claim."""
        decided_by = verified_operator()
        async with self._lock:
            approval = await self.get(approval_id)
            if status == "approved" and decided_via != "agent":
                # Before the claim: withdrawing a reject that waits on one is an approve of the same request.
                self._refuse_self_approval(approval, decided_by)
            if approval.status == "executed":
                claim = self._claims.get(approval_id)
                # Claimed by an action that has not begun, and that a refusal would give
                # back: an operator's reject waits on the claim and stops the action
                # (effect_started), and their approve withdraws a reject waiting there.
                # So too while the claim is being given back (no longer active): the
                # give-back reads the hold's rejection under this lock, after this.
                # A tool call's claim never gives back, so nothing could apply a reject to it.
                if decided_via != "agent" and claim is not None and claim.may_give_back and not claim.effect:
                    if status == "rejected":
                        claim.rejection = (comment, decided_via, decided_by)
                        return Decision(approval, "deferred")
                    if claim.rejection is not None:
                        claim.rejection = None
                        if not claim.active and not claim.gives_back_on_its_own:
                            return Decision(approval, "withdrawn_spent")
                        refused = claim.stopped or not claim.active or claim.refusal is not None
                        return Decision(approval, "withdrawn_after_refusal" if refused else "withdrawn")
                raise _claim_conflict(approval, claim, decided_via=decided_via, status=status)
            # An operator may change their mind before the action runs; an
            # agent may not overturn a decision, or rejecting would only delay it.
            if decided_via == "agent" and approval.status != "pending":
                raise PermissionError(f"approval {approval_id} has already been {approval.status}")
            # Nor by asking again: the same action requested anew is a new
            # approval, and it stays the operator's to decide.
            if decided_via == "agent" and status == "approved" and await self._operator_rejected(approval):
                raise PermissionError(
                    f"an operator rejected this action in session {approval.session_id}; "
                    f"only an operator can approve {approval_id}"
                )
            if status == "approved":
                await self._refuse_what_is_no_longer_whole(approval)
            # Later than the record it changes, as a give-back is: the catch-up keeps SQLite's rejection over a
            # file record not later than it, so an approve in the reject's microsecond was lost.
            now = self._later_than(approval.updated_at)
            approval.status = status
            approval.updated_at = now
            approval.decided_at = now
            approval.decision_comment = comment
            approval.decided_via = decided_via
            approval.decided_by = decided_by
            if status == "approved":
                approval.approved_expires_at = self._expiry_timestamp(now)
            else:
                approval.approved_expires_at = None
                self._sensitive_texts.pop(approval_id, None)
            await self._persist(approval)
        return Decision(approval)

    def _refuse_self_approval(self, approval: ApprovalRecord, decided_by: str | None) -> None:
        """Refuse an operator of APPROVAL_REFUSE_SELF_APPROVAL_OPERATORS an approve of what they asked for.

        Both named by credentials: an unowned request, or a decider no credential proves, is no one's own.
        The store cannot tell whether anyone else can reach the approval (the services decide that from its
        session's owner), so the refusal says where one can.
        """
        if decided_by is None or decided_by != approval.owner or decided_by not in self.refuse_self_approval:
            return
        raise ServerStateRefusal(
            f"approval {approval.id}: operator {decided_by} asked for it and may not approve it "
            "(APPROVAL_REFUSE_SELF_APPROVAL_OPERATORS). Another operator approves it where one can reach it, in a "
            f"session nobody owns; in a session {decided_by} owns, or in controller scope, no other operator can"
        )

    async def _refuse_what_is_no_longer_whole(self, approval: ApprovalRecord) -> None:
        """Refuse an approve of an approval missing what it was asked with: the agent asks again.

        A tool call's arguments, kept until the call spends the approval (a reject
        is not final): gone, the operator could not read what they approve. A
        sensitive text, dropped by a reject or a restart: gone, execute_approval
        could not type it. The key a tool call's digest is kept under
        (_kept_under_another_key): gone, no call could be matched to the approval.
        """
        if approval.arguments_size is not None and not await self.file_store.has_arguments(approval.id):
            raise ServerStateRefusal(
                f"approval {approval.id}: its arguments are no longer stored; the agent asks again"
            )
        if self._kept_under_another_key(approval.action):
            raise ServerStateRefusal(
                f"approval {approval.id}: its arguments' digest is {_ANOTHER_KEY}; the agent asks again"
            )
        if self._needs_held_text(approval) and self._held_text(approval) is None:
            raise ServerStateRefusal(f"approval {approval.id}: its text is no longer held; the agent asks again")

    async def _persist(self, approval: ApprovalRecord) -> None:
        """Write a record to its end under the caller's lock, whatever happens to the caller.

        The file write runs in a worker thread, which a cancellation does not stop: cancelled around it, the
        caller released the lock while the write went on, and a write landing after a later one (approved,
        after another call had spent the approval) revived the approval at the next start. A cancelled
        caller is cancelled once the write has landed; a claim reports it instead (_mark_executed).
        """
        _, cancelled = await _run_through(self._write(approval))
        if cancelled:
            raise asyncio.CancelledError

    async def _write(self, approval: ApprovalRecord) -> None:
        """The file store first, so a start whose SQLite fails sees every write, then SQLite while it is primary.

        When SQLite's half fails, the file is put back to the record SQLite still holds (none: removed), then
        the failure is raised: both stores hold what the caller is told, the old record. A file record left
        later than SQLite's was read as a write SQLite missed, and caught up at the next start (a give-back
        reported as failed, revived beside the new approval the agent asked for).
        """
        if self.sqlite_store is None or self._primary is not self.sqlite_store:
            await self.file_store.upsert(approval)
            return
        try:
            before: ApprovalRecord | None = await self.sqlite_store.get(approval.id)
        except KeyError:
            before = None
        await self.file_store.upsert(approval)
        try:
            await self.sqlite_store.upsert(approval)
        except Exception:
            await self._put_the_file_back(approval.id, before)
            raise

    async def _put_the_file_back(self, approval_id: str, before: ApprovalRecord | None) -> None:
        try:
            if before is None:
                await self.file_store.remove(approval_id)
            else:
                await self.file_store.upsert(before)
        except Exception as exc:
            # The SQLite failure is still raised; the next start may catch this file record up.
            logger.error(
                "approval %s: its SQLite write failed, and its file record could not be put back: %r",
                approval_id,
                exc,
                exc_info=exc,
            )

    async def _operator_rejected(self, approval: ApprovalRecord) -> bool:
        # Whatever the rejected record's origin: a rejection only ever restricts,
        # and one stored before origin existed loads as an action approval, so
        # comparing origins dropped every rejection an operator made before the upgrade.
        # Both keyed (_keyed): a stand-in rejected before 1.18.0 was stored with its plain digest. One kept under a
        # key that is gone matches nothing, as a sensitive text's digest matches nothing after a restart.
        requested = self._keyed(approval.action).model_dump(exclude=UNBOUND_FIELDS)
        return any(
            other.kind == approval.kind
            and other.decided_via != "agent"
            and self._keyed(other.action).model_dump(exclude=UNBOUND_FIELDS) == requested
            for other in await self.list(status="rejected", session_id=approval.session_id)
            # Controller scope is shared by every operator: only the same
            # requester's rejection counts, not another operator's.
            if approval.session_id != CONTROLLER_SCOPE or other.owner == approval.owner
        )

    async def _find_matching_pending(
        self,
        *,
        session_id: str,
        kind: ApprovalKind,
        action: BrowserActionDecision,
        owner: str | None,
        origin: ApprovalOrigin,
    ) -> ApprovalRecord | None:
        for approval in await self.list(status="pending", session_id=session_id):
            # Exactly the requester's own pending approval in controller scope:
            # joining another operator's would hand them its decision, and hand
            # the other operator a request they never made.
            if session_id == CONTROLLER_SCOPE and approval.owner != owner:
                continue
            if approval.kind == kind and approval.origin == origin and self._actions_match(approval.action, action):
                return approval
        return None

    def _stored_action(self, action: BrowserActionDecision) -> BrowserActionDecision:
        """The action as an approval records it: a sensitive text, and a tool call's digest, only as a keyed digest.

        Approvals are written to APPROVAL_ROOT, listed by GET /approvals and the
        approvals MCP tool, and shown to whoever approves. A `type` marked
        sensitive (a password, a card number) was stored and served verbatim,
        while the witness chain and action logs already redacted it.
        """
        if not (action.sensitive and action.text):
            return self._keyed(action)
        digest = hmac.new(self._sensitive_text_key, action.text.encode("utf-8"), hashlib.sha256).hexdigest()
        return action.model_copy(update={"text": f"{SENSITIVE_TEXT_PREFIX}{digest}"})

    def _keyed(self, action: BrowserActionDecision) -> BrowserActionDecision:
        """``action`` with a tool call stand-in's plain digest (_PLAIN_STAND_IN) as an HMAC under _digest_key.

        The gateway's sha256 covers the call's whole arguments, credentials included, and the preview beside it shows
        every field but those: kept as it is, a short cookie value or password could be guessed offline against the
        stored text. Matching keys both sides, so a record stored plain before 1.18.0 still matches its call. Any
        takeover whose text is a stand-in's, whatever asked for it: a record stored before approvals had an origin
        is one too, and the services queue no other (_refuse_unrunnable).
        """
        if action.action != "request_human_takeover":
            return action
        plain = _PLAIN_STAND_IN.fullmatch(action.text or "")
        if plain is None:
            return action
        key = self._digest_key()
        digest = hmac.new(key, plain.group(0).encode("utf-8"), hashlib.sha256).hexdigest()
        return action.model_copy(update={"text": f"{plain.group(1)} hmac-sha256:{_key_id(key)}:{digest}"})

    def _kept_under_another_key(self, action: BrowserActionDecision) -> bool:
        """Whether ``action`` is a stand-in whose digest is keyed by a key this process does not hold.

        The key file was replaced or removed before this start, or one start could not read it and keyed with a
        key of its own (_digest_key). Whether a call is the one such an approval was asked for cannot be told.
        """
        if action.action != "request_human_takeover":
            return False
        keyed = _KEYED_STAND_IN.fullmatch(action.text or "")
        return keyed is not None and keyed.group(1) != _key_id(self._digest_key())

    def _kept_under_a_key_that_is_gone(self, action: BrowserActionDecision) -> bool:
        """Whether ``action`` is kept under another key (_kept_under_another_key) that no later start will hold.

        Known only to a process that holds the key file's key: the other key is then one the file held before, or
        one a start that could not use the file made for itself. A process keying alone cannot tell it from the
        file's own key, under which the next start that reads the file matches the record again. The retention
        asks this (_past_retention): while it asked _kept_under_another_key, one start that could not read the
        file deleted every operator's rejection of a tool call past its days.
        """
        # In this order: the first reads the key, which is where a process learns that it keys alone.
        return self._kept_under_another_key(action) and not self._keyed_by_this_process_alone

    def _digest_key(self) -> bytes:
        """The tool call digest key: the file store's, or this process's own, with a warning, when that is unusable."""
        if self._tool_call_key is None:
            try:
                self._tool_call_key = self.file_store.digest_key()
            except (OSError, ValueError) as exc:
                logger.warning(
                    "approval store: %s cannot be used (%s), so tool call approvals are keyed by this process alone: "
                    "after a restart they are refused, and the agent asks again. Restart once it can be read, or "
                    "written where there is none; a file that holds no key has to be removed first",
                    self.file_store.root / DIGEST_KEY_FILE,
                    exc,
                )
                self._tool_call_key = secrets.token_bytes(32)
                self._keyed_by_this_process_alone = True
        return self._tool_call_key

    def _prune_sensitive_texts(self) -> None:
        cutoff = time.monotonic() - SENSITIVE_TEXT_RETENTION.total_seconds()
        for approval_id in [key for key, (_, kept_at) in self._sensitive_texts.items() if kept_at < cutoff]:
            del self._sensitive_texts[approval_id]

    @staticmethod
    def _needs_held_text(approval: ApprovalRecord) -> bool:
        """Whether the approval stores a sensitive text's digest, so running it takes the text this process holds."""
        action = approval.action
        return bool(action.sensitive and action.text and action.text.startswith(SENSITIVE_TEXT_PREFIX))

    def _held_text(self, approval: ApprovalRecord) -> str | None:
        self._prune_sensitive_texts()
        entry = self._sensitive_texts.get(approval.id)
        return entry[0] if entry is not None else None

    def executable_action(self, approval: ApprovalRecord) -> BrowserActionDecision:
        """The approved action with its sensitive text restored, for execute_approval."""
        action = approval.action
        if not self._needs_held_text(approval):
            return action
        text = self._held_text(approval)
        if text is None:
            raise PermissionError(
                f"approval {approval.id} is for sensitive text, which this controller process no longer holds "
                "(restarted, another worker, or older than 24 hours); run the action again to request a new approval"
            )
        return action.model_copy(update={"text": text})

    def covers(self, approval: ApprovalRecord, action: BrowserActionDecision) -> bool:
        """Whether ``approval`` was asked for ``action``, matched as require_approved matches them."""
        return self._actions_match(approval.action, action)

    def _actions_match(self, stored: BrowserActionDecision, requested: BrowserActionDecision) -> bool:
        return self._keyed(stored).model_dump(exclude=UNBOUND_FIELDS) == self._stored_action(requested).model_dump(
            exclude=UNBOUND_FIELDS
        )

    def has_expired(self, approval: ApprovalRecord) -> bool:
        if approval.approved_expires_at is None:
            return False
        return self._parse_timestamp(approval.approved_expires_at) <= datetime.now(UTC)

    def _ensure_not_expired(self, approval: ApprovalRecord) -> None:
        if self.has_expired(approval):
            raise PermissionError(f"approval {approval.id} has expired")

    def _expiry_timestamp(self, decided_at: str) -> str:
        decided = self._parse_timestamp(decided_at)
        return (decided + self.approval_ttl).isoformat().replace("+00:00", "Z")

    @classmethod
    def _later_than(cls, stamp: str) -> str:
        """utc_now(), or ``stamp`` plus a microsecond when the clock has not moved past it.

        For a give-back: the catch-up reads a file record not later than SQLite's executed one as a stale
        write, and two utc_now() calls can return the same microsecond, so a give-back carries a later time
        than the claim it turns back.
        """
        now = utc_now()
        if cls._parse_timestamp(now) > cls._parse_timestamp(stamp):
            return now
        later = cls._parse_timestamp(stamp) + timedelta(microseconds=1)
        return later.isoformat(timespec="microseconds").replace("+00:00", "Z")

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
