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

# A human-paced mouse path / scroll is many small input events. On a tab the
# renderer is not painting (an employee's own background tab -- the owner's
# screen shows another tab), Chrome aligns every mousemove/wheel with an
# animation frame that never comes on time, so each event's ack takes ~1 s and
# a 18-34 step path turned one click into 28-44 s (2026-09-25, AI Studio in
# Emad's tab). Once one step is this slow, or the whole gesture has used its
# budget, the rest of the gesture goes out as a single event.
SLOW_INPUT_STEP_SECONDS = 0.25
HUMAN_GESTURE_BUDGET_SECONDS = 1.5

# How many matches of an ambiguous selector are actually probed (visibility +
# hit-test) before giving up and falling back to the historical `.first`. An
# LLM-authored text/CSS selector rarely matches more than a handful of
# elements; bounding this keeps a pathological selector (e.g. "button") cheap.
MAX_AMBIGUOUS_CANDIDATES = 8


# Hit-test a candidate point before pressing on it: a real user's pointer lands on
# whatever is actually drawn there, and an overlay that intercepts the click is a
# real (and common) reason a click silently does nothing. Loosely handles shadow
# DOM by walking the composed tree from the hit node back up to `el`.
HIT_TEST_SCRIPT = """(el, point) => {
  const { x, y } = point;
  const hit = document.elementFromPoint(x, y);
  if (!hit) return false;
  if (hit === el || el.contains(hit)) return true;
  // A styled checkbox/radio/input covered by its own <label>: clicking the label
  // is exactly what a person does, and it acts on the control.
  const label = hit.closest ? hit.closest('label') : null;
  if (label && label.control === el) return true;
  let node = hit;
  const seen = new Set();
  while (node && !seen.has(node)) {
    seen.add(node);
    if (node === el) return true;
    if (node.assignedSlot === el) return true;
    const root = typeof node.getRootNode === 'function' ? node.getRootNode() : null;
    if (node.parentElement) {
      node = node.parentElement;
    } else if (root && root.host) {
      node = root.host;
    } else {
      node = null;
    }
  }
  return false;
}"""

# Whether focus actually landed on the element (or something inside it) after a
# click, for focus_locator's fallback to a plain .focus() call.
FOCUS_CHECK_SCRIPT = """(el) => {
  const active = document.activeElement;
  if (!active) return false;
  return active === el || el.contains(active);
}"""


# An in-page HTML dialog/modal (ChatGPT's cookie banner, welcome/memory modal,
# any `role="dialog"` overlay) blocks whatever is behind it exactly like a
# native `window.confirm` does, but nothing about it shows up in
# BrowserDialogService (that only watches `page.on("dialog")`, which never
# fires for these). Finds the topmost visible one and describes it -- title
# and visible button labels -- so a refused click can say *why* instead of
# "Another element covers the target".
OPEN_HTML_DIALOG_SCRIPT = """() => {
  const isVisible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  };
  const candidates = [...document.querySelectorAll('[role="dialog"], [aria-modal="true"], dialog[open]')]
    .filter(isVisible);
  if (!candidates.length) return null;
  // A stack of dialogs is rare; when it happens the most recently opened one
  // is usually last in DOM order and visually on top.
  const el = candidates[candidates.length - 1];
  const labelledBy = el.getAttribute('aria-labelledby');
  const title = el.getAttribute('aria-label')
    || (labelledBy && document.getElementById(labelledBy) ? document.getElementById(labelledBy).innerText : '')
    || (el.querySelector('h1,h2,h3,[role="heading"]') || {}).innerText
    || '';
  const buttons = [...el.querySelectorAll('button, [role="button"]')]
    .filter(isVisible)
    .map((b) => (b.innerText || b.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim())
    .filter(Boolean)
    .slice(0, 6);
  if (!el.dataset.operatorId) {
    el.dataset.operatorId = `op-${Math.random().toString(36).slice(2, 10)}`;
  }
  return {
    selector_hint: `[data-operator-id="${el.dataset.operatorId}"]`,
    title: String(title).replace(/\\s+/g, ' ').trim().slice(0, 160),
    buttons,
  };
}"""


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
                locator = await self.resolve_candidate_locator(session.page, target["selector"], target)
                await locator.scroll_into_view_if_needed()
                await self.pointer_click_locator(session, locator, fast=fast, target=target)
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
                locator = await self.resolve_candidate_locator(session.page, target["selector"], target)
                await locator.scroll_into_view_if_needed()
                await self.pointer_hover_locator(session, locator, fast=fast, target=target)
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
            locator = await self.resolve_candidate_locator(session.page, target["selector"], target)
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
            locator = await self.resolve_candidate_locator(session.page, target["selector"], target)
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
                loop = asyncio.get_running_loop()
                started = loop.time()
                for step in range(steps):
                    if step == steps - 1:
                        chunk_x, chunk_y = remaining_x, remaining_y
                    else:
                        fraction = random.uniform(0.15, 0.35)
                        chunk_x, chunk_y = remaining_x * fraction, remaining_y * fraction
                    step_started = loop.time()
                    await session.page.mouse.wheel(chunk_x, chunk_y)
                    remaining_x -= chunk_x
                    remaining_y -= chunk_y
                    now = loop.time()
                    if step < steps - 1 and (
                        now - step_started > SLOW_INPUT_STEP_SECONDS
                        or now - started > HUMAN_GESTURE_BUDGET_SECONDS
                    ):
                        # Background tab: the rest in one wheel event.
                        await session.page.mouse.wheel(remaining_x, remaining_y)
                        break
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
        loop = asyncio.get_running_loop()
        started = loop.time()
        for step in range(1, steps + 1):
            t = step / steps
            inv = 1 - t
            px = inv**3 * start_x + 3 * inv * inv * t * control_1[0] + 3 * inv * t * t * control_2[0] + t**3 * x
            py = inv**3 * start_y + 3 * inv * inv * t * control_1[1] + 3 * inv * t * t * control_2[1] + t**3 * y
            step_started = loop.time()
            await session.page.mouse.move(px, py)
            now = loop.time()
            if step < steps and (
                now - step_started > SLOW_INPUT_STEP_SECONDS
                or now - started > HUMAN_GESTURE_BUDGET_SECONDS
            ):
                # The tab is not painting (see SLOW_INPUT_STEP_SECONDS): land on
                # the target in one move instead of paying ~1 s per step.
                await session.page.mouse.move(x, y)
                break
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

    @staticmethod
    def _random_point_in_box(box: dict[str, Any]) -> tuple[float, float]:
        """A point inside the box that is not its exact center -- e.g. x = box.x +
        box.w * uniform(0.3, 0.7) -- clamped so a sliver of an element (a few px
        wide) still lands at least 1px inside instead of on its edge."""
        width = float(box["width"])
        height = float(box["height"])
        margin = 1.0

        px = width * random.uniform(0.3, 0.7)
        if width > 2 * margin:
            px = min(max(px, margin), width - margin)
        else:
            px = width / 2

        py = height * random.uniform(0.3, 0.7)
        if height > 2 * margin:
            py = min(max(py, margin), height - margin)
        else:
            py = height / 2

        return (float(box["x"]) + px, float(box["y"]) + py)

    @staticmethod
    def _locator_is_in_iframe(locator: Any) -> bool:
        """Best-effort only: Playwright's public Locator API has no supported way to
        ask "are you inside a child frame". elementFromPoint uses main-frame viewport
        coordinates, which bounding_box() also reports for a main-frame element, so
        when we cannot tell we default to False (run the hit-test as normal) rather
        than silently skipping it."""
        try:
            frame = getattr(getattr(locator, "_impl_obj", None), "_frame", None)
            if frame is None:
                return False
            parent_frame = getattr(frame, "parent_frame", None)
            return parent_frame is not None
        except Exception:
            return False

    async def _hit_test(self, locator: Any, point: tuple[float, float]) -> bool:
        # Deliberately not try/except here: a failure (e.g. no `evaluate` on a test
        # double, or a genuinely detached element) is part of "the pointer path
        # raised" and is handled by the caller's fallback to locator.click()/hover().
        return bool(await locator.evaluate(HIT_TEST_SCRIPT, {"x": point[0], "y": point[1]}))

    async def _open_html_dialog(self, page: "Page") -> dict[str, Any] | None:
        """The topmost visible in-page dialog/modal (`role="dialog"`,
        `aria-modal="true"`, `<dialog open>`) -- see OPEN_HTML_DIALOG_SCRIPT.
        None on any failure (a detached page, a page with no such element)."""
        try:
            return await page.evaluate(OPEN_HTML_DIALOG_SCRIPT)
        except Exception:
            return None

    async def _locator_in_dialog(self, locator: Any, dialog_selector_hint: str) -> bool:
        try:
            return bool(await locator.evaluate("(el, sel) => !!el.closest(sel)", dialog_selector_hint))
        except Exception:
            return False

    async def resolve_candidate_locator(self, page: "Page", selector: str, target: dict[str, Any]) -> Any:
        """`page.locator(selector)` may match more than one element -- an LLM-built
        text/CSS selector ("تسجيل الدخول") is not guaranteed unique the way a
        `data-operator-id` is. `.first` alone always takes whichever match is
        first in DOM order, covered or not, in an open dialog or not: a cookie
        banner over a header duplicate made every click on it fail forever even
        though a second, perfectly clickable match existed on the page.

        Picks, in order: a visible+enabled+hit-testable match inside the
        topmost open HTML dialog (an open dialog is exactly where the next
        real click belongs); otherwise the first visible+enabled+hit-testable
        match in DOM order. Records the pick on `target` for the audit trail.
        Falls back to `.first` (unresolved) when there is exactly one match,
        or when none of the probed candidates come out clickable -- the
        existing click_intercepted / dialog-aware error paths then fire with a
        real, reproducible target instead of silently guessing."""
        group = page.locator(selector)
        try:
            count = await group.count()
        except Exception:
            return group.first
        if count <= 1:
            return group.first

        dialog = await self._open_html_dialog(page)
        fallback: tuple[int, Any] | None = None
        for index in range(min(count, MAX_AMBIGUOUS_CANDIDATES)):
            candidate = group.nth(index)
            try:
                if not await candidate.is_visible() or await candidate.is_disabled():
                    continue
                box = await candidate.bounding_box()
            except Exception:
                continue
            if not box:
                continue
            try:
                hit = await self._hit_test(candidate, self._random_point_in_box(box))
            except Exception:
                hit = False
            if not hit:
                continue
            if fallback is None:
                fallback = (index, candidate)
            if dialog and await self._locator_in_dialog(candidate, dialog["selector_hint"]):
                self._record_ambiguous_pick(target, count, index, "inside_open_dialog", selector)
                return candidate
        if fallback is not None:
            index, candidate = fallback
            self._record_ambiguous_pick(target, count, index, "first_visible_in_viewport", selector)
            return candidate
        target["ambiguous_matches"] = count
        return group.first

    @staticmethod
    def _record_ambiguous_pick(
        target: dict[str, Any], count: int, index: int, reason: str, selector: str,
    ) -> None:
        target["ambiguous_matches"] = count
        target["ambiguous_picked_index"] = index
        target["ambiguous_pick_reason"] = reason
        logger.info(
            "resolve_candidate_locator: %d matches for %r, picked #%d (%s)",
            count, selector, index, reason,
        )

    async def _click_blocked_error(self, locator: Any, target: dict[str, Any]) -> BrowserActionError:
        """The `click_intercepted` a hit-test failure raises, enriched: when an
        HTML dialog is open on the page and the target itself is not inside
        it, the dialog is almost always what is actually in the way -- say so
        (its title and visible buttons) instead of the generic "another
        element covers the target", so the employee knows to handle the
        dialog first rather than retry the same click."""
        try:
            page = locator.page
        except Exception:
            page = None
        dialog = await self._open_html_dialog(page) if page is not None else None
        if dialog is not None and not await self._locator_in_dialog(locator, dialog["selector_hint"]):
            title = dialog.get("title") or "(untitled)"
            buttons = dialog.get("buttons") or []
            suffix = f" -- visible buttons: {', '.join(buttons)}" if buttons else ""
            message = f"{title}{suffix}"
            return BrowserActionError(
                f"A dialog is open and is blocking this target: {message}",
                code="dialog_blocking",
                action="click",
                status_code=400,
                retryable=True,
                # `type`/`message` match the shape the native-dialog `dialog_open`
                # error already uses (see BrowserDialogService) so anything
                # relaying "a dialog is on the page" generically (the approval
                # broker's _relayed_error_detail) already knows how to carry
                # this one through with no further changes.
                details={
                    "selector": target.get("selector"),
                    "dialog": {**dialog, "type": "html_dialog", "message": message},
                },
            )
        return BrowserActionError(
            "Another element covers the target",
            code="click_intercepted",
            action="click",
            status_code=400,
            retryable=True,
            details={"selector": target.get("selector")},
        )

    async def _resolve_click_point(
        self, locator: Any, box: dict[str, Any], target: dict[str, Any],
    ) -> tuple[float, float]:
        if self._locator_is_in_iframe(locator):
            # bounding_box() is already page-relative for a same-frame element, but
            # elementFromPoint on the top document cannot see into a child frame --
            # skip the hit-test rather than raise a false click_intercepted.
            return self._random_point_in_box(box)

        candidate = self._random_point_in_box(box)
        if await self._hit_test(locator, candidate):
            return candidate
        for _ in range(3):
            candidate = self._random_point_in_box(box)
            if await self._hit_test(locator, candidate):
                return candidate

        center = (float(box["x"] + box["width"] / 2), float(box["y"] + box["height"] / 2))
        if await self._hit_test(locator, center):
            return center

        raise await self._click_blocked_error(locator, target)

    async def pointer_click_locator(
        self, session: "BrowserSession", locator: Any, *, fast: bool, target: dict[str, Any],
    ) -> None:
        """The real human pointer path for a click on a resolved locator: a random
        point inside the box (not its center), hit-tested so an overlay covering the
        target is caught instead of silently clicking through it, moved to along the
        existing bezier path, with a hover dwell before the press. Any failure in
        that path (other than our own click_intercepted) falls back to a plain
        locator.click(); `target["pointer"]` records which one actually ran."""
        try:
            box = await locator.bounding_box()
        except Exception as exc:
            logger.info("click: bounding_box failed (%s); falling back to locator.click()", exc)
            await locator.click()
            target["pointer"] = "element_click"
            return
        if not box:
            logger.info("click: no bounding box for target; falling back to locator.click()")
            await locator.click()
            target["pointer"] = "element_click"
            return

        try:
            point = await self._resolve_click_point(locator, box, target)
        except BrowserActionError:
            raise
        except Exception as exc:
            logger.info("click: pointer path failed (%s); falling back to locator.click()", exc)
            await locator.click()
            target["pointer"] = "element_click"
            return

        pressed = False
        try:
            if fast:
                await session.page.mouse.move(point[0], point[1])
                session.mouse_position = point
            else:
                await self.move_mouse_human_like(session, point[0], point[1])
                # Hover dwell: a small wiggle and back so the page gets
                # mouseover/mouseenter/pointermove before the press, not a press
                # that lands with no prior hover at all. The press itself lands on
                # the hit-tested point, never on the wiggle.
                wiggle = (point[0] + random.uniform(-2, 2), point[1] + random.uniform(-2, 2))
                await session.page.mouse.move(*wiggle)
                await asyncio.sleep(random.uniform(0.08, 0.25))
                await session.page.mouse.move(point[0], point[1])
            session.mouse_position = point
            pressed = True
            await session.page.mouse.down()
            if not fast:
                await asyncio.sleep(random.uniform(0.05, 0.14))
            await session.page.mouse.up()
            target["pointer"] = "mouse"
            target["x"], target["y"] = point
        except Exception as exc:
            if pressed:
                # The press may already have reached the page: a second, element-level
                # click could act twice (submit twice, toggle back). Report instead.
                raise
            logger.info("click: mouse move failed (%s); falling back to locator.click()", exc)
            await locator.click()
            target["pointer"] = "element_click"

    async def pointer_hover_locator(
        self, session: "BrowserSession", locator: Any, *, fast: bool, target: dict[str, Any],
    ) -> None:
        """Same random-point-in-box + curved move + dwell as the click path, minus
        the hit-test (no intercepted-click concept for a hover) and the press."""
        try:
            box = await locator.bounding_box()
        except Exception as exc:
            logger.info("hover: bounding_box failed (%s); falling back to locator.hover()", exc)
            await locator.hover()
            return
        if not box:
            logger.info("hover: no bounding box for target; falling back to locator.hover()")
            await locator.hover()
            return

        point = self._random_point_in_box(box)
        try:
            if fast:
                await session.page.mouse.move(point[0], point[1])
                session.mouse_position = point
            else:
                await self.move_mouse_human_like(session, point[0], point[1])
                wiggle = (point[0] + random.uniform(-3, 3), point[1] + random.uniform(-3, 3))
                await session.page.mouse.move(*wiggle)
                session.mouse_position = wiggle
                await asyncio.sleep(random.uniform(0.08, 0.25))
            target["x"], target["y"] = point
        except Exception as exc:
            logger.info("hover: mouse sequence failed (%s); falling back to locator.hover()", exc)
            await locator.hover()

    async def focus_locator(self, session: "BrowserSession", locator: Any, *, fast: bool = False) -> None:
        target: dict[str, Any] = {}
        try:
            await self.pointer_click_locator(session, locator, fast=fast, target=target)
        except BrowserActionError as exc:
            if exc.code != "click_intercepted":
                raise
            # A field under a floating label / placeholder overlay: focus it directly
            # (checked below) rather than refusing to type.
            logger.info("focus_locator: field is covered; focusing it directly")
        if not fast:
            await asyncio.sleep(0.05 + random.random() * 0.1)
        try:
            focused = await locator.evaluate(FOCUS_CHECK_SCRIPT)
        except Exception:
            focused = False
        if not focused:
            try:
                await locator.focus()
            except Exception as exc:
                logger.info("focus_locator: fallback locator.focus() failed: %s", exc)

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
