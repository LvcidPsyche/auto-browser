from __future__ import annotations

import logging
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
                raise BrowserActionError(
                    "Action failed. Refresh observation and retry.",
                    code="browser_action_failed",
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
        totp_result = await manager._maybe_handle_totp(session)
        if totp_result is not None:
            context.target.setdefault("totp", totp_result)
        manager._assert_runtime_url_allowed(session.page.url)
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
        try:
            await manager._settle(session.page)
            return await manager._observation_payload(session, limit=20, screenshot_label=label)
        except PlaywrightError as exc:
            logger.warning("post-action observation failed for session %s: %s", session.id, exc)
            return {
                "url": session.page.url,
                "observation_error": f"Page changed while it was being observed; observe again. ({exc})"[:300],
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
