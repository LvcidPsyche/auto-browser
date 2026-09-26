from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from playwright.async_api import Error as PlaywrightError

from .. import events as _events
from ..action_errors import BrowserActionError
from ..utils import utc_now
from ..witness import WitnessApproval

logger = logging.getLogger(__name__)

ActionOperation = Callable[[], Awaitable[None]]

# Markers within a raw Playwright error message that mean "the element is simply
# no longer there" -- a fresh observation and a different element_id is the fix,
# not a bare retry. Strict-mode violation cannot actually happen (every locator we
# build ends in `.first`), but it means the same thing if it ever did.
_TARGET_NOT_FOUND_MARKERS = (
    "waiting for locator",
    "strict mode violation",
    "is not attached to the dom",
    "element is not attached",
    "resolved to 0 elements",
    "timeout while waiting for selector",
)

# The element exists and is attached, but Playwright would not actually be able to
# click/type on it as it stands (off-screen, display:none, still animating).
_TARGET_NOT_VISIBLE_MARKERS = (
    "not visible",
    "outside of the viewport",
    "element is not visible",
    "element is not stable",
)

# Something else is drawn on top of the target and would eat the click.
_CLICK_INTERCEPTED_MARKERS = (
    "intercepts pointer events",
    "would receive the click",
)


def classify_playwright_error(message: str) -> tuple[str, str]:
    """Map a raw Playwright error message to a specific (code, message) pair.

    Everything used to surface as one generic "browser_action_failed" no matter
    the cause, which left the only recovery a blind retry. This gives the caller
    (an agent, or the owner) enough to react correctly: re-observe and pick a
    different element vs. just wait and retry vs. the target being covered."""
    lowered = (message or "").lower()

    # Most specific first: a Playwright timeout's call log always starts with
    # "waiting for locator", and only its later lines say what actually stopped it.
    if any(marker in lowered for marker in _CLICK_INTERCEPTED_MARKERS):
        return ("click_intercepted", "Another element covers the target.")
    if any(marker in lowered for marker in _TARGET_NOT_VISIBLE_MARKERS):
        return ("target_not_visible", "The element is on the page but not visible or not stable.")
    if any(marker in lowered for marker in _TARGET_NOT_FOUND_MARKERS):
        return (
            "target_not_found",
            "The element is no longer on the page. Observe again and pick a current element.",
        )
    return ("browser_action_failed", "Action failed. Refresh observation and retry.")


def _dialogs(manager: Any) -> Any:
    from ..browser.services.dialogs import BrowserDialogService

    dialogs = getattr(manager, "dialogs", None)
    return dialogs if isinstance(dialogs, BrowserDialogService) else None


async def _runtime_dns_check(manager: Any, url: str) -> None:
    """Public-internet mode: the page an action landed on (after redirects) must
    still resolve to a public address -- the DNS-rebinding half of the policy."""
    check = getattr(type(manager), "_assert_runtime_url_resolves_public", None)
    if check is None or not asyncio.iscoroutinefunction(check):
        return
    await check(manager, url)


@dataclass(slots=True)
class ActionRunContext:
    manager: Any
    session: Any
    action_name: str
    target: dict[str, Any]
    operation: ActionOperation


@dataclass(slots=True)
class ActionWitnessState:
    witness_context: dict[str, Any]
    action_class: str
    outcome: Any
    before: dict[str, Any]


class BrowserActionPipeline:
    async def run(self, context: ActionRunContext) -> dict[str, Any]:
        async with context.session.lock:
            lifecycle = getattr(context.manager, "session_lifecycle", None)
            settings = getattr(context.manager, "settings", None)
            timeout = getattr(settings, "browser_action_timeout_seconds", None)
            if lifecycle is None or not isinstance(timeout, (int, float)):
                return await self._run_locked(context)
            # A hard ceiling: one call that never returns must not hold the
            # session lock forever (see Settings.browser_action_timeout_seconds).
            return await lifecycle.guarded(
                context.session, self._run_locked(context), what=context.action_name, timeout=timeout
            )

    async def _run_locked(self, context: ActionRunContext) -> dict[str, Any]:
        # An agent action owns the dialogs/popups it causes (see
        # app/browser/services/dialogs.py). The owner's own "type here" bridge
        # (target mode "focused") is not an agent action.
        session = context.session
        agent_action = context.target.get("mode") != "focused" and isinstance(
            getattr(session, "agent_action_depth", None), int
        )
        if agent_action:
            session.agent_action_depth += 1
        try:
            return await self._run_marked(context)
        finally:
            if agent_action:
                session.agent_action_depth = max(0, session.agent_action_depth - 1)
                grace = getattr(getattr(context.manager, "settings", None), "agent_dialog_grace_seconds", 3.0)
                if not isinstance(grace, (int, float)):
                    grace = 3.0
                session.agent_dialog_grace_until = time.monotonic() + float(grace)

    async def _run_marked(self, context: ActionRunContext) -> dict[str, Any]:
        dialogs = _dialogs(context.manager)
        if dialogs is not None:
            # A dialog already up on this tab blocks every page call; fail fast
            # with its text instead of hanging until the action timeout.
            await dialogs.raise_if_open(context.session, context.action_name)
        witness_state = await self._prepare(context)
        try:
            await self._execute(context, witness_state)
        except PermissionError as exc:
            failed = await self._handle_policy_block(context, witness_state)
            raise BrowserActionError(
                "Action blocked by policy",
                code="browser_action_blocked",
                action=context.action_name,
                status_code=403,
                retryable=False,
                url=context.session.page.url,
                details={"snapshot": failed},
            ) from exc
        except BrowserActionError as exc:
            await self._handle_browser_action_error(context, witness_state, exc)
            raise
        except PlaywrightError as exc:
            failed = await self._handle_playwright_error(context, witness_state)
            code, message = classify_playwright_error(str(exc))
            raise BrowserActionError(
                message,
                code=code,
                action=context.action_name,
                status_code=400,
                retryable=True,
                url=context.session.page.url,
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
        before = await manager._light_snapshot(session, label=f"before-{context.action_name}")
        return ActionWitnessState(
            witness_context=witness_context,
            action_class=action_class,
            outcome=outcome,
            before=before,
        )

    async def _execute(self, context: ActionRunContext, witness_state: ActionWitnessState) -> None:
        manager = context.manager
        session = context.session
        if witness_state.action_class != "read":
            await manager._ensure_witness_remote_ready(session, action=context.action_name)
        if witness_state.outcome.should_block:
            raise PermissionError(witness_state.outcome.block_reason or "Witness policy blocked this action")
        await context.operation()
        dialogs = _dialogs(manager)
        if dialogs is not None:
            followed = await dialogs.follow_popup(session)
            if followed is not None:
                context.target["popup"] = followed
            if dialogs.heal_active_page(session):
                context.target["returned_to_opener"] = True
            open_dialog = await dialogs.open_dialog(session)
            if open_dialog is not None:
                # The page is blocked until the dialog is answered: report it and
                # stop here (every probe below would hang on it).
                context.target["dialog"] = open_dialog
                return
        totp_result = await manager._maybe_handle_totp(session)
        if totp_result is not None:
            context.target.setdefault("totp", totp_result)
        manager._assert_runtime_url_allowed(session.page.url)
        await _runtime_dns_check(manager, session.page.url)
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

    async def _handle_policy_block(
        self,
        context: ActionRunContext,
        witness_state: ActionWitnessState,
    ) -> dict[str, Any]:
        session = context.session
        try:
            if session.page.url != witness_state.before.get("url"):
                await session.page.go_back(wait_until="domcontentloaded")
                await context.manager._settle(session.page)
        except Exception as exc:
            logger.debug("failed to roll back blocked action navigation for session %s: %s", session.id, exc)
        failed = await context.manager._light_snapshot(session, label=f"blocked-{context.action_name}")
        await self._record_terminal_event(
            context,
            witness_state,
            status="blocked",
            after=failed,
            error="Action blocked by policy",
            approval_status="blocked",
        )
        return failed

    async def _handle_browser_action_error(
        self,
        context: ActionRunContext,
        witness_state: ActionWitnessState,
        exc: BrowserActionError,
    ) -> None:
        failed = await context.manager._light_snapshot(context.session, label=f"failed-{context.action_name}")
        await self._record_terminal_event(
            context,
            witness_state,
            status="failed",
            after=failed,
            error=exc.payload,
            approval_status="failed",
        )
        exc.details.setdefault("snapshot", failed)

    async def _handle_playwright_error(
        self,
        context: ActionRunContext,
        witness_state: ActionWitnessState,
    ) -> dict[str, Any]:
        failed = await context.manager._light_snapshot(context.session, label=f"failed-{context.action_name}")
        await self._record_terminal_event(
            context,
            witness_state,
            status="failed",
            after=failed,
            error="Action failed. Refresh observation and retry.",
            approval_status="failed",
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
        approval_status: str,
    ) -> None:
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
            details={"target": context.target, "error": error},
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
                status=approval_status,
            ),
            metadata={"error": error},
        )

    async def _record_success(
        self,
        context: ActionRunContext,
        witness_state: ActionWitnessState,
    ) -> dict[str, Any]:
        manager = context.manager
        session = context.session
        after = await manager._observation_payload(session, limit=20, screenshot_label=f"after-{context.action_name}")
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
                status="executed" if self._approval_id(context, witness_state) else None,
            ),
        )
        await manager._persist_session(session, status="active")
        _events.emit_action(session.id, context.action_name, "ok", {"url": session.page.url})
        return {
            "action": context.action_name,
            "action_class": action_class,
            "session": await manager._session_summary(session),
            "before": witness_state.before,
            "after": after,
            "target": context.target,
            "verification": verification,
        }

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

    @staticmethod
    def _approval_id(context: ActionRunContext, witness_state: ActionWitnessState) -> str | None:
        return witness_state.witness_context.get("approval_id") or context.target.get("approval_id")
