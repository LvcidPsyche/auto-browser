from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import TYPE_CHECKING, Any

from ... import events as _events
from ...action_errors import BrowserActionError
from ...actions import ActionRunContext
from ...approvals import ApprovalRequiredError
from ...models import ApprovalKind, BrowserActionDecision
from ...navigation_policy import await_public_dns_check
from ...utils import spawn_background_task
from ...webhooks import dispatch_approval_event
from ...witness import WitnessApproval

try:  # pragma: no cover - optional until dependency is installed in runtime image
    import pyotp
except Exception:  # pragma: no cover - graceful fallback for non-login test runs
    pyotp = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ...browser_manager import BrowserSession

logger = logging.getLogger(__name__)


# document.activeElement's identifying attributes, for type_focused's redaction check.
FOCUSED_INPUT_ATTRIBUTES_SCRIPT = """() => {
  const el = document.activeElement;
  // Focus inside a frame cannot be classified from here: fail closed (null -> redact).
  if (!el || el === document.body || el.tagName === 'IFRAME' || el.tagName === 'FRAME') return null;
  return {
    type: el.getAttribute('type'),
    name: el.getAttribute('name'),
    id: el.id || null,
    autocomplete: el.getAttribute('autocomplete'),
    placeholder: el.getAttribute('placeholder'),
    aria_label: el.getAttribute('aria-label'),
  };
}"""


class BrowserActionService:
    """Encapsulates browser action execution and approval orchestration."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager

    async def navigate(self, session_id: str, url: str) -> dict[str, Any]:
        self.manager._assert_url_allowed(url)
        await await_public_dns_check(self.manager, url)
        session = await self.manager.get_session(session_id)

        async def operation() -> None:
            await session.page.goto(url, wait_until="domcontentloaded")
            await self.manager._settle(session.page)
            challenge = await self.manager._check_bot_challenge(session)
            if challenge:
                logger.warning("bot challenge detected after navigation: %s", challenge)
                try:
                    await self.manager.request_human_takeover(
                        session.id,
                        reason=f"Bot challenge detected: {challenge['signal']}",
                    )
                except Exception as exc:
                    logger.warning(
                        "failed to request human takeover after bot challenge on session %s: %s",
                        session.id,
                        exc,
                    )

        return await self.manager._run_action(session, "navigate", {"url": url}, operation)

    async def click(
        self,
        session_id: str,
        *,
        selector: str | None = None,
        element_id: str | None = None,
        x: float | None = None,
        y: float | None = None,
        pace: str = "human",
    ) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        target = self.resolve_target(selector=selector, element_id=element_id, x=x, y=y)
        fast = pace == "fast"

        async def operation() -> None:
            if target["mode"] == "coordinates":
                await self.click_human_like(session, float(x), float(y), fast=fast)
            else:
                locator = session.page.locator(target["selector"]).first
                await locator.scroll_into_view_if_needed()
                coords = await self.locator_center(locator)
                if coords is None:
                    await locator.click()
                else:
                    target["x"], target["y"] = coords
                    await self.click_human_like(session, coords[0], coords[1], fast=fast)
            await self.manager._settle(session.page)
            await self.pace_delay(pace)

        return await self.manager._run_action(session, "click", target, operation)

    async def hover(
        self,
        session_id: str,
        *,
        selector: str | None = None,
        element_id: str | None = None,
        x: float | None = None,
        y: float | None = None,
        pace: str = "human",
    ) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        target = self.resolve_target(selector=selector, element_id=element_id, x=x, y=y)
        fast = pace == "fast"

        async def operation() -> None:
            if target["mode"] == "coordinates":
                await self.move_mouse_human_like(session, float(x), float(y), fast=fast)
            else:
                locator = session.page.locator(target["selector"]).first
                await locator.scroll_into_view_if_needed()
                coords = await self.locator_center(locator)
                if coords is None:
                    await locator.hover()
                else:
                    target["x"], target["y"] = coords
                    await self.move_mouse_human_like(session, coords[0], coords[1], fast=fast)
            await self.manager._settle(session.page)
            await self.pace_delay(pace)

        return await self.manager._run_action(session, "hover", target, operation)

    async def select_option(
        self,
        session_id: str,
        *,
        selector: str | None = None,
        element_id: str | None = None,
        value: str | None = None,
        label: str | None = None,
        index: int | None = None,
    ) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        target = self.resolve_target(selector=selector, element_id=element_id)

        async def operation() -> None:
            locator = session.page.locator(target["selector"]).first
            await locator.scroll_into_view_if_needed()
            if index is not None:
                await locator.select_option(index=index)
            elif value is not None:
                await locator.select_option(value=value)
            else:
                await locator.select_option(label=label)
            await self.manager._settle(session.page)

        return await self.manager._run_action(
            session,
            "select_option",
            {**target, "value": value, "label": label, "index": index},
            operation,
        )

    async def type(
        self,
        session_id: str,
        *,
        text: str,
        selector: str | None = None,
        element_id: str | None = None,
        clear_first: bool = True,
        sensitive: bool = False,
        pace: str = "human",
    ) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        target = self.resolve_target(selector=selector, element_id=element_id)
        payload = self.text_target_payload(
            target,
            text,
            clear_first=clear_first,
            sensitive=sensitive,
            preview_chars=80,
        )
        fast = pace == "fast"

        async def operation() -> None:
            locator = session.page.locator(target["selector"]).first
            if await self.locator_is_sensitive_input(locator):
                payload.pop("text_preview", None)
                payload["text_redacted"] = True
            await locator.scroll_into_view_if_needed()
            await self.focus_locator(session, locator, fast=fast)
            if clear_first:
                await session.page.keyboard.press("Control+a")
                if not fast:
                    await asyncio.sleep(0.03)
                await session.page.keyboard.press("Delete")
                if not fast:
                    await asyncio.sleep(0.05)
            await self.type_text_human_like(session.page, text, fast=fast)
            await self.manager._settle(session.page)
            await self.pace_delay(pace)

        return await self.manager._run_action(session, "type", payload, operation)

    async def type_focused(self, session_id: str, *, text: str) -> dict[str, Any]:
        """Insert text into whatever element already has focus, no target needed.

        This backs the owner's "type here" bridge in the noVNC viewer: the owner
        clicks a field through the VNC mouse (real click, so real DOM focus,
        unaffected by any VNC keyboard limitation) and this delivers the text via
        CDP directly, bypassing X11 keysyms entirely. That is the only reliable
        path for Arabic and other non-Latin scripts typed on a phone keyboard,
        since a mobile IME composes such text through `input`/composition events
        that the VNC keyboard channel never sees.
        """
        session = await self.manager.get_session(session_id)
        target = {"mode": "focused"}
        payload = self.text_target_payload(target, text, clear_first=False, sensitive=False, preview_chars=80)

        async def operation() -> None:
            # The owner types passwords through this bridge too: never keep a preview of what
            # went into a password / one-time-code field (same check type() uses).
            # Reads document.activeElement directly (no locator auto-wait); unreadable focus
            # fails closed.
            try:
                attributes = await session.page.evaluate(FOCUSED_INPUT_ATTRIBUTES_SCRIPT)
                sensitive_field = attributes is None or self.attributes_are_sensitive(attributes)
            except Exception:
                sensitive_field = True
            if sensitive_field:
                payload.pop("text_preview", None)
                payload["text_redacted"] = True
            await session.page.keyboard.insert_text(text)
            await self.manager._settle(session.page)

        return await self.manager._run_action(session, "type", payload, operation)

    async def press(self, session_id: str, key: str) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)

        async def operation() -> None:
            await session.page.keyboard.press(key)
            await self.manager._settle(session.page)

        return await self.manager._run_action(session, "press", {"key": key}, operation)

    async def scroll(
        self, session_id: str, delta_x: float, delta_y: float, *, pace: str = "human",
    ) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)

        async def operation() -> None:
            if pace == "fast":
                await session.page.mouse.wheel(delta_x, delta_y)
            else:
                # Natural scroll: a real trackpad/wheel arrives as several small
                # ticks, not one jump -- split the requested delta into a handful
                # of uneven chunks with a brief pause between them.
                steps = random.randint(3, 6)
                remaining_x, remaining_y = delta_x, delta_y
                for step in range(steps):
                    if step == steps - 1:
                        chunk_x, chunk_y = remaining_x, remaining_y
                    else:
                        fraction = random.uniform(0.15, 0.35)
                        chunk_x, chunk_y = remaining_x * fraction, remaining_y * fraction
                        remaining_x -= chunk_x
                        remaining_y -= chunk_y
                    await session.page.mouse.wheel(chunk_x, chunk_y)
                    await asyncio.sleep(random.uniform(0.02, 0.09))
            await self.manager._settle(session.page)
            await self.pace_delay(pace)

        return await self.manager._run_action(
            session,
            "scroll",
            {"delta_x": delta_x, "delta_y": delta_y},
            operation,
        )

    async def wait(self, session_id: str, wait_ms: int) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)

        async def operation() -> None:
            await asyncio.sleep(max(0, wait_ms) / 1000)

        return await self.manager._run_action(session, "wait", {"wait_ms": wait_ms}, operation)

    async def reload(self, session_id: str) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)

        async def operation() -> None:
            await session.page.reload(wait_until="domcontentloaded")
            await self.manager._settle(session.page)

        return await self.manager._run_action(session, "reload", {}, operation)

    async def go_back(self, session_id: str) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)

        async def operation() -> None:
            await session.page.go_back(wait_until="domcontentloaded")
            await self.manager._settle(session.page)

        return await self.manager._run_action(session, "go_back", {}, operation)

    async def go_forward(self, session_id: str) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)

        async def operation() -> None:
            await session.page.go_forward(wait_until="domcontentloaded")
            await self.manager._settle(session.page)

        return await self.manager._run_action(session, "go_forward", {}, operation)

    async def execute_decision(
        self,
        session_id: str,
        decision: BrowserActionDecision,
        *,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        approval = await self.require_decision_approval(
            session_id,
            decision,
            approval_id=approval_id,
        )
        session.pending_witness_context = {
            "risk_category": decision.risk_category,
            "approval_id": approval_id or (approval.id if approval is not None else None),
            "approval_status": "approved" if approval_id or approval is not None else None,
            "runtime_requires_approval": approval is not None or approval_id is not None,
            "sensitive_input": bool(getattr(decision, "sensitive", False)),
        }
        try:
            if decision.action == "navigate":
                result = await self.manager.navigate(session_id, decision.url or "")
            elif decision.action == "click":
                result = await self.manager.click(
                    session_id,
                    selector=decision.selector,
                    element_id=decision.element_id,
                    x=decision.x,
                    y=decision.y,
                )
            elif decision.action == "hover":
                result = await self.manager.hover(
                    session_id,
                    selector=decision.selector,
                    element_id=decision.element_id,
                    x=decision.x,
                    y=decision.y,
                )
            elif decision.action == "select_option":
                result = await self.manager.select_option(
                    session_id,
                    selector=decision.selector,
                    element_id=decision.element_id,
                    value=decision.value,
                    label=decision.label,
                    index=decision.index,
                )
            elif decision.action == "type":
                result = await self.manager.type(
                    session_id,
                    selector=decision.selector,
                    element_id=decision.element_id,
                    text=decision.text or "",
                    clear_first=decision.clear_first,
                    sensitive=decision.sensitive,
                )
            elif decision.action == "press":
                result = await self.manager.press(session_id, decision.key or "")
            elif decision.action == "scroll":
                result = await self.manager.scroll(session_id, decision.delta_x, decision.delta_y)
            elif decision.action == "wait":
                result = await self.manager.wait(session_id, decision.wait_ms)
            elif decision.action == "reload":
                result = await self.manager.reload(session_id)
            elif decision.action == "go_back":
                result = await self.manager.go_back(session_id)
            elif decision.action == "go_forward":
                result = await self.manager.go_forward(session_id)
            elif decision.action == "upload":
                result = await self.manager.upload(
                    session_id,
                    selector=decision.selector,
                    element_id=decision.element_id,
                    file_path=decision.file_path or "",
                    approved=False,
                    approval_id=approval_id,
                )
                return result
            else:  # pragma: no cover - guarded by schema
                raise ValueError(f"Unsupported action: {decision.action}")

            if approval is not None:
                await self.manager.approvals.mark_executed(approval.id)
            return result
        finally:
            session.pending_witness_context = None

    async def require_decision_approval(
        self,
        session_id: str,
        decision: BrowserActionDecision,
        *,
        approval_id: str | None,
        fallback_reason: str | None = None,
        approval_kind: ApprovalKind | None = None,
    ):
        kind = approval_kind or self.approval_kind_for_decision(decision)
        if kind is None:
            return None
        if approval_id:
            return await self.manager.approvals.require_approved(
                approval_id=approval_id,
                session_id=session_id,
                kind=kind,
                action=decision,
            )

        session = await self.manager.get_session(session_id)
        approval = await self.manager.approvals.create_or_reuse_pending(
            session_id=session_id,
            kind=kind,
            reason=fallback_reason or decision.reason,
            action=decision,
            observation=await self._approval_observation(session),
        )
        await self.manager._record_witness_receipt(
            session,
            event_type="approval",
            status="pending",
            action="approval_requested",
            action_class="control",
            risk_category=decision.risk_category,
            approval=WitnessApproval(
                required=True,
                approval_id=approval.id,
                status=approval.status,
                reason=approval.reason,
            ),
            target={
                "kind": approval.kind,
                "action": decision.action,
                "selector": decision.selector,
                "element_id": decision.element_id,
            },
            metadata={"reason": approval.reason},
        )
        _events.emit_approval(session_id, approval.id, approval.kind, approval.status, approval.reason)
        if self.manager.settings.approval_webhook_url:
            spawn_background_task(
                dispatch_approval_event(
                    approval,
                    webhook_url=self.manager.settings.approval_webhook_url,
                    webhook_secret=self.manager.settings.approval_webhook_secret,
                )
            )
        raise ApprovalRequiredError(approval)

    async def require_governed_approval(
        self,
        session_id: str,
        decision: BrowserActionDecision,
        *,
        approval_id: str | None,
    ):
        kind = self.governed_approval_kind_for_decision(decision)
        if kind is None:
            return None
        reason = (
            "Governed workflow requires operator approval before executing "
            f"{decision.risk_category or 'write'} action {decision.action!r}."
        )
        return await self.require_decision_approval(
            session_id,
            decision,
            approval_id=approval_id,
            fallback_reason=reason,
            approval_kind=kind,
        )

    async def _approval_observation(self, session: "BrowserSession") -> dict[str, Any]:
        return {
            "url": session.page.url,
            "title": await session.page.title(),
            "takeover_url": self.manager._current_takeover_url(session),
            "remote_access": self.manager.remote_access.session_info(session),
            "isolation": self.manager.session_lifecycle.isolation_payload(session),
            "auth_state": self.manager.auth_profiles.session_auth_state_info(session),
            "last_action": session.last_action,
        }

    async def settle(self, page: "Page") -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=min(self.manager.settings.action_timeout_ms, 5000))
        except Exception as exc:
            # Busy pages legitimately never reach networkidle; proceed anyway.
            logger.debug("settle: networkidle not reached: %s", exc)
        await page.wait_for_timeout(250)

    async def run_action(
        self,
        session: "BrowserSession",
        action_name: str,
        target: dict[str, Any],
        operation: Any,
    ) -> dict[str, Any]:
        return await self.manager.action_pipeline.run(
            ActionRunContext(
                manager=self.manager,
                session=session,
                action_name=action_name,
                target=target,
                operation=operation,
            )
        )

    @staticmethod
    def text_target_payload(
        target: dict[str, Any],
        text: str,
        *,
        clear_first: bool,
        sensitive: bool,
        preview_chars: int,
    ) -> dict[str, Any]:
        payload = {**target, "clear_first": clear_first}
        if sensitive:
            payload["text_redacted"] = True
        else:
            payload["text_preview"] = text[:preview_chars]
        return payload

    async def locator_is_sensitive_input(self, locator: Any) -> bool:
        try:
            attributes = {
                "type": await locator.get_attribute("type"),
                "name": await locator.get_attribute("name"),
                "id": await locator.get_attribute("id"),
                "autocomplete": await locator.get_attribute("autocomplete"),
                "placeholder": await locator.get_attribute("placeholder"),
                "aria_label": await locator.get_attribute("aria-label"),
            }
        except Exception:
            return False
        return self.attributes_are_sensitive(attributes)

    @staticmethod
    def attributes_are_sensitive(attributes: dict[str, Any]) -> bool:
        input_type = (attributes.get("type") or "").strip().lower()
        if input_type == "password":
            return True

        autocomplete = (attributes.get("autocomplete") or "").strip().lower()
        if autocomplete in {"current-password", "new-password", "one-time-code"}:
            return True

        haystack = " ".join(str(value or "") for value in attributes.values()).lower()
        return bool(re.search(r"password|passcode|otp|one[- ]time|verification|token|secret|2fa|mfa", haystack))

    async def locator_center(self, locator: Any) -> tuple[float, float] | None:
        try:
            box = await locator.bounding_box()
        except Exception:
            return None
        if not box:
            return None
        return (float(box["x"] + box["width"] / 2), float(box["y"] + box["height"] / 2))

    async def pace_delay(self, pace: str) -> None:
        """A short pause between one action and the next, mimicking the beat a real
        person takes to look at the page before their next move. Skipped entirely
        for `pace="fast"`, which the owner reserves for when he explicitly asks to
        hurry (see the `pace` argument on click/type/hover/scroll)."""
        if pace == "fast":
            return
        await asyncio.sleep(random.uniform(0.4, 1.5))

    async def move_mouse_human_like(
        self, session: "BrowserSession", x: float, y: float, *, fast: bool = False,
    ) -> None:
        if fast:
            await session.page.mouse.move(x, y)
            session.mouse_position = (x, y)
            return
        start = session.mouse_position
        if start is None:
            start = (
                self.manager.settings.default_viewport_width / 2 + random.randint(-120, 120),
                self.manager.settings.default_viewport_height / 2 + random.randint(-80, 80),
            )
            await session.page.mouse.move(start[0], start[1])
            session.mouse_position = start

        start_x, start_y = start
        control_1 = (
            start_x + (x - start_x) * random.uniform(0.2, 0.4) + random.randint(-80, 80),
            start_y + (y - start_y) * random.uniform(0.1, 0.5) + random.randint(-80, 80),
        )
        control_2 = (
            start_x + (x - start_x) * random.uniform(0.6, 0.85) + random.randint(-60, 60),
            start_y + (y - start_y) * random.uniform(0.5, 0.9) + random.randint(-60, 60),
        )
        steps = random.randint(18, 34)
        for step in range(1, steps + 1):
            t = step / steps
            inv = 1 - t
            px = inv**3 * start_x + 3 * inv * inv * t * control_1[0] + 3 * inv * t * t * control_2[0] + t**3 * x
            py = inv**3 * start_y + 3 * inv * inv * t * control_1[1] + 3 * inv * t * t * control_2[1] + t**3 * y
            await session.page.mouse.move(px, py)
            await asyncio.sleep(random.uniform(0.004, 0.018))
        session.mouse_position = (x, y)

    async def click_human_like(
        self, session: "BrowserSession", x: float, y: float, *, fast: bool = False,
    ) -> None:
        if fast:
            await self.move_mouse_human_like(session, x, y, fast=True)
            await session.page.mouse.down()
            await session.page.mouse.up()
            session.mouse_position = (x, y)
            return
        jitter_x = x + random.uniform(-2.5, 2.5)
        jitter_y = y + random.uniform(-2.5, 2.5)
        await self.move_mouse_human_like(session, jitter_x, jitter_y)
        await asyncio.sleep(random.uniform(0.03, 0.12))
        await session.page.mouse.down()
        await asyncio.sleep(random.uniform(0.02, 0.08))
        await session.page.mouse.up()
        session.mouse_position = (jitter_x, jitter_y)

    async def focus_locator(self, session: "BrowserSession", locator: Any, *, fast: bool = False) -> None:
        coords = await self.locator_center(locator)
        if coords is None:
            await locator.click()
        else:
            await self.click_human_like(session, coords[0], coords[1], fast=fast)
        if not fast:
            await asyncio.sleep(0.05 + random.random() * 0.1)

    async def type_text_human_like(self, page: "Page", text: str, *, fast: bool = False) -> None:
        if fast:
            await page.keyboard.type(text)
            return
        for index, char in enumerate(text):
            await page.keyboard.type(char)
            delay_ms = random.randint(
                self.manager.settings.human_typing_min_delay_ms,
                self.manager.settings.human_typing_max_delay_ms,
            )
            if index > 0 and index % random.randint(6, 12) == 0:
                delay_ms += random.randint(180, 600)
            await asyncio.sleep(delay_ms / 1000)

    async def first_visible_locator(self, page: "Page", selectors: list[str]) -> tuple[Any, str] | None:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() > 0 and await locator.is_visible():
                    return locator, selector
            except Exception:
                continue
        return None

    async def maybe_handle_totp(self, session: "BrowserSession") -> dict[str, Any] | None:
        if not session.totp_secret:
            return None
        if pyotp is None:
            raise BrowserActionError(
                "TOTP support is not installed in this controller runtime",
                action="totp_fill",
                code="totp_unavailable",
                retryable=False,
                details={"url": session.page.url},
            )
        selectors = [
            'input[autocomplete="one-time-code"]',
            'input[inputmode="numeric"][maxlength="6"]',
            'input[name*="otp" i]',
            'input[name*="code" i]',
            'input[id*="otp" i]',
            'input[id*="code" i]',
            'input[aria-label*="code" i]',
            'input[placeholder*="code" i]',
        ]
        located = await self.first_visible_locator(session.page, selectors)
        if located is None:
            return None

        locator, selector = located
        code = pyotp.TOTP(session.totp_secret).now()
        await self.focus_locator(session, locator)
        try:
            await locator.fill("")
        except Exception:
            await session.page.keyboard.press("Control+a")
            await session.page.keyboard.press("Delete")
        await self.type_text_human_like(session.page, code)
        submit = await self.first_visible_locator(
            session.page,
            [
                'button[type="submit"]',
                '[aria-label*="verify" i][role="button"]',
                'button:has-text("Verify")',
                'button:has-text("Continue")',
                'button:has-text("Next")',
                'button:has-text("Submit")',
            ],
        )
        if submit is not None:
            coords = await self.locator_center(submit[0])
            if coords is None:
                await submit[0].click()
            else:
                await self.click_human_like(session, coords[0], coords[1])
        await self.manager._settle(session.page)
        return {"selector": selector, "code_length": len(code)}

    def approval_kind_for_decision(self, decision: BrowserActionDecision) -> ApprovalKind | None:
        if decision.action == "upload":
            return "upload" if self.manager.settings.require_approval_for_uploads else None
        if decision.risk_category in {"post", "payment", "account_change", "destructive"}:
            return decision.risk_category
        return None

    @staticmethod
    def governed_approval_kind_for_decision(decision: BrowserActionDecision) -> ApprovalKind | None:
        if decision.risk_category == "read":
            return None
        if decision.action == "upload" or decision.risk_category == "upload":
            return "upload"
        if decision.risk_category in {"post", "payment", "account_change", "destructive"}:
            return decision.risk_category
        return "write"

    @staticmethod
    def action_class(action_name: str) -> str:
        if action_name in {
            "navigate",
            "hover",
            "scroll",
            "wait",
            "reload",
            "go_back",
            "go_forward",
        }:
            return "read"
        return "write"

    @staticmethod
    def action_verification(
        action_name: str,
        target: dict[str, Any],
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> dict[str, Any]:
        signals: list[str] = []
        if before.get("url") != after.get("url"):
            signals.append("url_changed")
        if before.get("title") != after.get("title"):
            signals.append("title_changed")
        if before.get("active_element") != after.get("active_element"):
            signals.append("active_element_changed")
        if before.get("text_excerpt") != after.get("text_excerpt"):
            signals.append("text_excerpt_changed")

        before_counts = (before.get("dom_outline") or {}).get("counts") or {}
        after_counts = (after.get("dom_outline") or {}).get("counts") or {}
        if before_counts != after_counts:
            signals.append("dom_counts_changed")

        before_accessibility = (before.get("accessibility_outline") or {}).get("focused")
        after_accessibility = (after.get("accessibility_outline") or {}).get("focused")
        if before_accessibility != after_accessibility:
            signals.append("accessibility_focus_changed")

        interacted_element = target.get("element_id")
        selector = target.get("selector")
        interactables = after.get("interactables") or []
        target_seen_after = None
        if interacted_element:
            target_seen_after = any(item.get("element_id") == interacted_element for item in interactables)
        elif selector:
            target_seen_after = any(item.get("selector_hint") == selector for item in interactables)

        if target_seen_after is True:
            signals.append("target_still_visible")
        elif target_seen_after is False:
            signals.append("target_no_longer_visible")

        verified = bool(signals)
        if action_name == "navigate":
            verified = "url_changed" in signals or "title_changed" in signals
        elif action_name in {"go_back", "go_forward"}:
            verified = "url_changed" in signals or "title_changed" in signals
        elif action_name in {
            "click",
            "press",
            "scroll",
        }:
            verified = bool(
                {
                    "url_changed",
                    "title_changed",
                    "active_element_changed",
                    "text_excerpt_changed",
                    "accessibility_focus_changed",
                }
                & set(signals)
            )
        elif action_name == "hover":
            verified = (
                bool({"active_element_changed", "text_excerpt_changed", "accessibility_focus_changed"} & set(signals))
                or target_seen_after is not None
            )
        elif action_name in {"type", "select_option"}:
            verified = bool(
                {"active_element_changed", "text_excerpt_changed", "accessibility_focus_changed"} & set(signals)
            )
        elif action_name in {"wait", "reload"}:
            verified = True
        elif action_name == "upload":
            verified = True

        return {
            "verified": verified,
            "signals": signals,
            "target_seen_after": target_seen_after,
        }

    @staticmethod
    def resolve_target(
        *,
        selector: str | None = None,
        element_id: str | None = None,
        x: float | None = None,
        y: float | None = None,
    ) -> dict[str, Any]:
        if element_id:
            return {
                "mode": "selector",
                "element_id": element_id,
                "selector": f'[data-operator-id="{element_id}"]',
            }
        if selector:
            return {"mode": "selector", "selector": selector}
        if x is not None and y is not None:
            return {"mode": "coordinates", "x": x, "y": y}
        raise ValueError("Provide selector, element_id, or x+y coordinates")
