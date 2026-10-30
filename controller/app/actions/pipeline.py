from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from playwright.async_api import Error as PlaywrightError

from .. import events as _events
from ..action_errors import (
    BROWSER_ACTION_BLOCKED,
    PAGE_NOT_RESPONDING,
    SCREENSHOT_WITHHELD,
    BrowserActionError,
    NavigationStoppedError,
    SessionNotFoundError,
)
from ..approvals import ApprovalRefusal, effect_started, held_status, refused_before_effect
from ..browser.request_gate import UNATTRIBUTED_STOP, block_summary, stopped_by_gate
from ..utils import utc_now
from ..witness import WitnessApproval

logger = logging.getLogger(__name__)

ActionOperation = Callable[[], Awaitable[None]]

# The only actions that may run while the page sits on a host ALLOWED_HOSTS refuses:
# they are how a session gets off it. Everything else would act on, or read, that page.
LEAVES_DISALLOWED_PAGE = frozenset({"navigate", "go_back"})
# Actions a block is not rolled back from with go_back: after a go_back it goes one page further back, after
# a reload to the page before the reloaded one; a refused page is blanked instead. A go_forward is rolled back:
# a redirect replaces the entry it moved to, so a go_back returns to the page the action left.
NOT_ROLLED_BACK = frozenset({"go_back", "reload"})
# Errors that mean only the page's state was not read: the snapshot or observation reports them in place.
_UNOBSERVED = frozenset({SCREENSHOT_WITHHELD, PAGE_NOT_RESPONDING})
# Where an action's target records the step that failed after its change was made (see ActionRunContext).
AFTER_CHANGE = "after_change"
WITHHELD_SNAPSHOT_ERROR = "The page is on a host ALLOWED_HOSTS does not allow; its content is withheld."
# For an OS-level PermissionError while observing (an artifact file the controller could not write): its
# text can name a server path, so it is logged and this goes to the agent instead.
OS_OBSERVATION_ERROR = "The page could not be observed after the action (a file error on the controller)."
# Actions whose operation checks its target before it begins its change, and marks that moment itself
# (effect_started): an upload's refusal before the transfer leaves the approval it ran under unspent.
MARKS_OWN_EFFECT = frozenset({"upload"})


def _withheld_observation(manager: Any, session: Any, exc: PermissionError) -> dict[str, Any]:
    """A post-action observation refused with ``exc``: the URL as shown, and the refusal, or for an
    OS-level error (it has an errno, and its text can name a server path) a fixed text, the OS text logged."""
    logger.warning("post-action observation withheld for session %s: %s", session.id, exc)
    error = OS_OBSERVATION_ERROR if exc.errno is not None else str(exc)[:300]
    return {"url": manager._shown_url(session.page), "observation_error": error}


def _withheld_snapshot(manager: Any, session: Any) -> dict[str, Any]:
    """The snapshot of a page that moved off ALLOWED_HOSTS while it was read: where it is, and nothing it showed.

    Shown as _shown_url shows it everywhere else: by its origin while the page is refused, whole once it is
    not. Always cut to its origin, an allowed page's URL differed from the one the rollback compares, so a
    blocked action went back from a page nothing had moved, and a success reported url_changed.
    """
    return {"url": manager._shown_url(session.page), "snapshot_error": WITHHELD_SNAPSHOT_ERROR}


def _no_change_made() -> bool:
    return False


def _not_begun() -> bool:
    return False


@dataclass(slots=True)
class ActionRunContext:
    """One action's run. ``change_made`` answers whether the operation has made a change that cannot be
    taken back (an upload, once the file has gone in): from then on a page failure or a retryable error
    does not fail the action, since a retry would make the change twice. The action is recorded and
    reported as done, with the step that failed under AFTER_CHANGE in its target."""

    manager: Any
    session: Any
    action_name: str
    target: dict[str, Any]
    operation: ActionOperation
    change_made: Callable[[], bool] = _no_change_made
    # For an action that marks its own effect (MARKS_OWN_EFFECT): whether its change has begun (an
    # upload's transfer). Refused before then, it has nothing of its own for a block to roll back.
    began: Callable[[], bool] = _not_begun


@dataclass(slots=True)
class ActionWitnessState:
    witness_context: dict[str, Any]
    action_class: str
    outcome: Any
    before: dict[str, Any]
    # The URL of the page the agent read before the action; None when it was not read (refused, or moved
    # while it was read). A blocked action's rollback that lands back on it clears the taint the action left.
    escape_url: str | None = None
    # Whether this run's operation began; until then a block has nothing of the action's to roll back.
    operation_started: bool = False


class BrowserActionPipeline:
    async def run(self, context: ActionRunContext) -> dict[str, Any]:
        try:
            return await self._run(context)
        except Exception as exc:
            # Raised before the operation began its change (a page off the list, a
            # witness that is not ready, a policy block): nothing ran, so the layer
            # holding the approval gives it back for a retry. Once the change has
            # begun, nothing is recorded and the approval stays spent.
            refused_before_effect(exc)
            raise

    async def _run(self, context: ActionRunContext) -> dict[str, Any]:
        async with context.session.lock:
            if context.manager.sessions.get(context.session.id) is not context.session:
                # Closed while this action waited for the lock (containment among others): say so, with the
                # recorded reason, rather than run on a closed context.
                await context.manager.get_session(context.session.id)
                raise SessionNotFoundError(context.session.id, status="closed")
            gates = getattr(context.manager, "request_gates", None)
            if gates is not None:
                # Nothing the action does (its snapshot, its effect) starts on a lost gate.
                gates.assert_live(context.session)
            # Right after a refused tab closed (the refusal's advice), what the page commits because of this
            # action would count as that tab's choice: the action waits out the grace (bounded), before its
            # snapshot and before its effect is marked, so a cancellation meanwhile gives an approval back.
            await context.manager._wait_out_closed_page_grace(context.session)
            witness_state = await self._prepare(context)
            try:
                await self._execute(context, witness_state)
            except PermissionError as exc:
                # The approval guard says what became of the approval; anything else is a policy block.
                if isinstance(exc, ApprovalRefusal):
                    message, code, status_code = str(exc), exc.code, 409
                elif isinstance(exc, NavigationStoppedError):
                    # Stopped before it was sent (the pre-request gate): its text names only the origin.
                    message, code, status_code = str(exc), BROWSER_ACTION_BLOCKED, 403
                else:
                    message, code, status_code = "Action blocked by policy", BROWSER_ACTION_BLOCKED, 403
                # On the holds now, not once the block's snapshot and trail are written: an operator's
                # approve meanwhile is told the action was refused, not that it may start. The error
                # raised below replaces it as the refusal (run).
                refused_before_effect(exc)
                failed = await self._handle_policy_block(context, witness_state, error=message, code=code)
                raise BrowserActionError(
                    message,
                    code=code,
                    action=context.action_name,
                    status_code=status_code,
                    retryable=False,
                    url=context.manager._shown_url(context.session.page),
                    details={"snapshot": failed},
                ) from exc
            except BrowserActionError as exc:
                if exc.retryable and context.change_made():
                    return await self._record_after_change(context, witness_state, exc.message)
                await self._handle_browser_action_error(context, witness_state, exc)
                raise
            except PlaywrightError as exc:
                if context.change_made():
                    return await self._record_after_change(context, witness_state, exc.message)
                failed = await self._handle_playwright_error(context, witness_state)
                raise BrowserActionError(
                    "Action failed. Refresh observation and retry.",
                    code="browser_action_failed",
                    action=context.action_name,
                    status_code=400,
                    retryable=True,
                    url=context.manager._shown_url(context.session.page),
                    details={"snapshot": failed},
                ) from exc
            return await self._record_success(context, witness_state)

    async def _prepare(self, context: ActionRunContext) -> ActionWitnessState:
        manager = context.manager
        session = context.session
        witness_context = manager._consume_witness_context(session)
        action_class = manager._witness_action_class(
            context.action_name,
            risk_category=witness_context.get("risk_category"),
        )
        outcome = manager.witness_policy.evaluate_action(
            session=manager._witness_session_context(session),
            action=manager._build_witness_action_context(
                action_name=context.action_name,
                target=context.target,
                witness_context=witness_context,
            ),
        )
        escape_url = None
        if manager._page_off_list(session):
            # Not read: the before-state goes to the audit log and the receipts, and
            # _execute refuses every action but a way out of this page.
            before = {
                "url": manager._shown_url(session.page),
                "snapshot_error": "The page is on a host ALLOWED_HOSTS does not allow; its content is not read.",
            }
        else:
            try:
                before = await manager._light_snapshot(session, label=f"before-{context.action_name}")
                escape_url = before.get("url")
            except PermissionError:
                # Refused after the read: the page moved off the list meanwhile. The
                # action is still decided and recorded; _execute refuses it if it must.
                before = _withheld_snapshot(manager, session)
        return ActionWitnessState(
            witness_context=witness_context,
            action_class=action_class,
            outcome=outcome,
            before=before,
            escape_url=escape_url,
        )

    async def _execute(self, context: ActionRunContext, witness_state: ActionWitnessState) -> None:
        manager = context.manager
        session = context.session
        if context.action_name not in LEAVES_DISALLOWED_PAGE:
            manager._assert_page_allowed(session)
        if witness_state.action_class != "read":
            await manager._ensure_witness_remote_ready(session, action=context.action_name)
        if witness_state.outcome.should_block:
            raise PermissionError(witness_state.outcome.block_reason or "Witness policy blocked this action")
        gates = getattr(manager, "request_gates", None)
        if gates is not None:
            # Again before the effect and the operation: the gate can be lost while the action prepares.
            gates.assert_live(session)
        if context.action_name not in MARKS_OWN_EFFECT:
            effect_started()
            witness_state.operation_started = True
        page = session.page
        browser = getattr(session, "browser", None)
        # The browser's request sequence now: a block after it on this page is this action's.
        blocked_before = gates.mark(browser) if gates is not None else 0
        try:
            await context.operation()
        except PlaywrightError as exc:
            # Only the gate's own failure (its first line): a selector timeout keeps its error.
            if not stopped_by_gate(exc):
                raise
            stopped = await self._blocks_during(context, page, blocked_before)
            # A block, not a retryable failure; with no block tied to the page (its targetId could not be read),
            # a text that names no URL.
            if stopped:
                raise NavigationStoppedError(stopped[-1].explanation, origin=stopped[-1].origin) from exc
            raise NavigationStoppedError(UNATTRIBUTED_STOP) from exc
        stopped = await self._blocks_during(context, page, blocked_before)
        if stopped:
            # The action did its work; a navigation it led to on its own page was stopped before it was sent.
            context.target["blocked_navigations"] = [block_summary(block) for block in stopped]
        # Checked before the TOTP autofill, which types a live code into the
        # page: a redirect to a disallowed host must not receive one first.
        try:
            manager._assert_runtime_url_allowed(session.page.url)
        except PermissionError as exc:
            # Still the policy block it is, but after the change: say so in the record.
            context.target[AFTER_CHANGE] = (
                f"The change was made, then the page it led to was blocked: {str(exc)[:300]}. Observe the page "
                "before acting again, and do not repeat the action: that would make the change twice."
            )
            raise
        totp_result = await manager._maybe_handle_totp(session)
        if totp_result is not None:
            context.target.setdefault("totp", totp_result)
        challenge = await manager._check_bot_challenge(session)
        if challenge is not None:
            await manager.request_human_takeover(session.id, reason=f"Bot challenge detected: {challenge['signal']}")
            raise BrowserActionError(
                f"Bot challenge detected: {challenge['signal']}",
                action=context.action_name,
                code="captcha_detected",
                retryable=False,
                details=challenge,
            )

    @staticmethod
    async def _blocks_during(context: ActionRunContext, page: Any, since: int) -> list[Any]:
        """The pre-request gate's blocks on the action's own page since ``since``: a background tab's are not its."""
        gates = getattr(context.manager, "request_gates", None)
        if gates is None:
            return []
        return await gates.blocks_on_page(getattr(context.session, "browser", None), page, since=since)

    async def _handle_policy_block(
        self,
        context: ActionRunContext,
        witness_state: ActionWitnessState,
        *,
        error: str,
        code: str,
    ) -> dict[str, Any]:
        session = context.session
        # A page that moved itself before the operation began is not this action's doing: going back
        # would be a navigation nobody approved, made while the approval is given back for a retry.
        started = witness_state.operation_started or context.began()
        try:
            # The before-snapshot shows a refused page by its origin; compared the same way.
            if (
                started
                and context.action_name not in NOT_ROLLED_BACK
                and context.manager._shown_url(session.page) != witness_state.before.get("url")
            ):
                # Back on the page the agent read before the action, the taint the action left clears.
                with context.manager.page_provenance.escape(session.page, witness_state.escape_url):
                    await session.page.go_back(wait_until="domcontentloaded")
                await context.manager._settle(session.page)
        except Exception as exc:
            logger.debug("failed to roll back blocked action navigation for session %s: %s", session.id, exc)
        await self._leave_disallowed_page(context)
        failed = await self._failure_snapshot(context, label=f"blocked-{context.action_name}")
        await self._record_terminal_event(
            context,
            witness_state,
            status="blocked",
            after=failed,
            error=error,
            code=code,
        )
        return failed

    @staticmethod
    async def _leave_disallowed_page(context: ActionRunContext) -> None:
        """Move a session that a blocked action left on a disallowed page to about:blank.

        go_back only leaves when the page has an earlier history entry. After
        location.replace() (or in a popup's first page) it has none, so the
        rollback did nothing and the session stayed on the page the policy
        refused, readable by the very next observe.
        """
        manager = context.manager
        session = context.session
        # By URL: a page on the controller's blank already is (the guard handles the rest).
        if not manager._url_off_list(session.page.url):
            return
        # The controller's own blank, as the guard's is (_return_to_blank): not counted as a refused
        # commit, which the guard would send to about:blank a second time with a second audit event.
        manager.page_provenance.blank_started(session.page)
        try:
            await session.page.goto("about:blank")
        except Exception as exc:
            manager.page_provenance.blank_failed(session.page)
            logger.warning("could not move session %s off a disallowed page: %s", session.id, exc)
        except BaseException:
            # Cancelled: a blank still counted would exempt the page's own next about:blank commit.
            manager.page_provenance.blank_failed(session.page)
            raise

    async def _handle_browser_action_error(
        self,
        context: ActionRunContext,
        witness_state: ActionWitnessState,
        exc: BrowserActionError,
    ) -> None:
        failed = await self._failure_snapshot(context, label=f"failed-{context.action_name}")
        await self._record_terminal_event(
            context,
            witness_state,
            status="failed",
            after=failed,
            error=exc.payload,
        )
        exc.details.setdefault("snapshot", failed)

    async def _handle_playwright_error(
        self,
        context: ActionRunContext,
        witness_state: ActionWitnessState,
    ) -> dict[str, Any]:
        failed = await self._failure_snapshot(context, label=f"failed-{context.action_name}")
        await self._record_terminal_event(
            context,
            witness_state,
            status="failed",
            after=failed,
            error="Action failed. Refresh observation and retry.",
        )
        return failed

    async def _record_terminal_event(
        self,
        context: ActionRunContext,
        witness_state: ActionWitnessState,
        *,
        status: str,
        after: dict[str, Any],
        error: Any,
        code: str | None = None,
    ) -> None:
        """Record how the action ended; ``code`` is a blocked action's refusal code (approval_rejected, ...)."""
        manager = context.manager
        session = context.session
        await manager._append_jsonl(
            session.artifact_dir / "actions.jsonl",
            {
                "timestamp": utc_now(),
                "action": context.action_name,
                "status": status,
                "target": context.target,
                "before": witness_state.before,
                "after": after,
                "error": error,
            },
        )
        await manager.audit.append(
            event_type="browser_action",
            status=status,
            action=context.action_name,
            session_id=session.id,
            details={"target": context.target, "error": error, **({"code": code} if code else {})},
        )
        await manager._record_witness_receipt(
            session,
            event_type="browser_action",
            status=status,
            action=context.action_name,
            action_class=witness_state.action_class,
            risk_category=witness_state.witness_context.get("risk_category"),
            target=context.target,
            outcome=witness_state.outcome,
            before=witness_state.before,
            after=after,
            approval=self._approval(
                context,
                witness_state,
                status=self._approval_outcome(context, witness_state),
            ),
            metadata={"error": error},
        )

    async def _record_after_change(
        self,
        context: ActionRunContext,
        witness_state: ActionWitnessState,
        error: str,
    ) -> dict[str, Any]:
        """Report an action whose change was made before a step after it failed.

        An upload's file has gone in once set_input_files returns. A page that
        closed while the upload settled made it a failure to retry, and the
        retry (or the approval, executed again) uploaded the file twice.
        """
        logger.warning(
            "%s in session %s: a step after the change failed: %s", context.action_name, context.session.id, error
        )
        context.target[AFTER_CHANGE] = (
            f"The change was made, then a step after it failed: {error.splitlines()[0][:300]}. Observe the page "
            "before acting again, and do not repeat the action: that would make the change twice."
        )
        return await self._record_success(context, witness_state)

    async def _record_success(
        self,
        context: ActionRunContext,
        witness_state: ActionWitnessState,
    ) -> dict[str, Any]:
        manager = context.manager
        session = context.session
        after = await self._after_observation(context)
        session.last_action = context.action_name
        verification = manager._action_verification(context.action_name, context.target, witness_state.before, after)
        action_class = manager._action_class(context.action_name)
        payload = {
            "timestamp": utc_now(),
            "action": context.action_name,
            "action_class": action_class,
            "target": context.target,
            "before": witness_state.before,
            "after": after,
            "verification": verification,
        }
        await manager._append_jsonl(session.artifact_dir / "actions.jsonl", payload)
        await manager.audit.append(
            event_type="browser_action",
            status="ok",
            action=context.action_name,
            session_id=session.id,
            details={"target": context.target, "verification": verification},
        )
        await manager._record_witness_receipt(
            session,
            event_type="browser_action",
            status="ok",
            action=context.action_name,
            action_class=witness_state.action_class,
            risk_category=witness_state.witness_context.get("risk_category"),
            target=context.target,
            outcome=witness_state.outcome,
            before=witness_state.before,
            after=after,
            verification=verification,
            approval=self._approval(
                context,
                witness_state,
                status=self._approval_outcome(context, witness_state),
            ),
        )
        await manager._persist_session(session, status="active")
        _events.emit_action(session.id, context.action_name, "ok", {"url": manager._shown_url(session.page)})
        return {
            "action": context.action_name,
            "action_class": action_class,
            "session": await manager._session_summary(session),
            "before": witness_state.before,
            "after": after,
            "target": context.target,
            "verification": verification,
        }

    @staticmethod
    async def _failure_snapshot(context: ActionRunContext, *, label: str) -> dict[str, Any]:
        """Snapshot for a failed or blocked action, never failing itself.

        The page that just failed is often the one that cannot be read (closed,
        crashed, mid-navigation). A PlaywrightError here replaced the action's
        normalized error with a raw one and skipped its audit record.

        A page the policy refuses is not read at all: its text and a screenshot
        of it would go to the caller in the error, which is the leak the policy
        exists to stop.
        """
        if context.manager._page_off_list(context.session):
            return {
                "url": context.manager._shown_url(context.session.page),
                "snapshot_error": "The page is on a host ALLOWED_HOSTS does not allow; its content is withheld.",
            }
        try:
            return await context.manager._light_snapshot(context.session, label=label)
        except PlaywrightError as exc:
            logger.warning("failure snapshot unavailable for session %s: %s", context.session.id, exc)
            return {"url": context.manager._shown_url(context.session.page), "snapshot_error": str(exc)[:300]}
        except PermissionError:
            # The page moved off ALLOWED_HOSTS while the snapshot read it.
            return _withheld_snapshot(context.manager, context.session)
        except BrowserActionError as exc:
            # PII_SCRUB_SCREENSHOT_STRICT withheld the snapshot, or the page stopped
            # answering; the action's own error is the one to report.
            if exc.code not in _UNOBSERVED:
                raise
            return {"url": context.manager._shown_url(context.session.page), "snapshot_error": exc.message[:300]}

    @staticmethod
    async def _after_observation(context: ActionRunContext) -> dict[str, Any]:
        """Observe the page after an action that has already happened.

        A page that navigates late (a click whose handler redirects after the
        settle window) destroys the execution context mid-observation. That
        PlaywrightError escaped as a failed action, with no audit record or
        witness receipt, and callers retried an action that had run: a second
        submit. Retry once after settling, then report the action with a
        minimal observation rather than as a failure.
        """
        manager = context.manager
        session = context.session
        label = f"after-{context.action_name}"
        try:
            return await manager._observation_payload(session, limit=20, screenshot_label=label)
        except PlaywrightError as exc:
            logger.info("post-action observation failed for session %s, retrying: %s", session.id, exc)
        except PermissionError as exc:
            # The page moved itself off ALLOWED_HOSTS after the action's own check.
            # The action ran, so report it with the URL and nothing the page showed.
            return _withheld_observation(manager, session, exc)
        except BrowserActionError as exc:
            # PII_SCRUB_SCREENSHOT_STRICT withheld the screenshot, or the page
            # stopped answering (not asked again: it had its deadline). The action
            # ran, so it is reported and recorded the same way, without the image.
            if exc.code not in _UNOBSERVED:
                raise
            logger.warning("post-action observation unavailable for session %s: %s", session.id, exc.message)
            return {"url": manager._shown_url(session.page), "observation_error": exc.message[:300]}
        try:
            await manager._settle(session.page)
            return await manager._observation_payload(session, limit=20, screenshot_label=label)
        except PlaywrightError as exc:
            logger.warning("post-action observation failed for session %s: %s", session.id, exc)
            return {
                "url": manager._shown_url(session.page),
                "observation_error": f"Page changed while it was being observed; observe again. ({exc})"[:300],
            }
        except PermissionError as exc:
            return _withheld_observation(manager, session, exc)
        except BrowserActionError as exc:
            if exc.code not in _UNOBSERVED:
                raise
            logger.warning("post-action observation unavailable for session %s: %s", session.id, exc.message)
            return {"url": manager._shown_url(session.page), "observation_error": exc.message[:300]}

    def _approval(
        self,
        context: ActionRunContext,
        witness_state: ActionWitnessState,
        *,
        status: str | None,
    ) -> WitnessApproval:
        approval_id = self._approval_id(context, witness_state)
        return WitnessApproval(
            required=bool(
                witness_state.outcome.require_approval
                or witness_state.witness_context.get("approval_id")
                or context.target.get("approval_id")
            ),
            approval_id=approval_id,
            status=status,
        )

    def _approval_outcome(self, context: ActionRunContext, witness_state: ActionWitnessState) -> str | None:
        """What happened to the action's approval, apart from how the action ended.

        Spent once the change had begun; otherwise given back (held_for_execution).
        None for an id this run does not hold: a caller's approval_id on an action
        that needs none was reported spent or given back, even one already spent or rejected.
        """
        approval_id = self._approval_id(context, witness_state)
        if approval_id is None:
            return None
        return held_status(approval_id)

    @staticmethod
    def _approval_id(context: ActionRunContext, witness_state: ActionWitnessState) -> str | None:
        return witness_state.witness_context.get("approval_id") or context.target.get("approval_id")
