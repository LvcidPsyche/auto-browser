"""JavaScript dialogs (alert / confirm / prompt / beforeunload) and popups.

Before this service nothing listened for ``page.on("dialog")``, and Playwright's
rule for a page with no dialog listener is to dismiss every dialog at once
(beforeunload is accepted). On a persistent profile that the owner drives by
hand through the live view, that meant every confirm/prompt a site showed HIM
was silently cancelled before he could see it -- a verification or "are you
sure?" prompt simply never appeared.

Now every page carries a listener, and the rule is:

* A dialog that opens while an agent action is running (or within
  ``AGENT_DIALOG_GRACE_SECONDS`` after it) belongs to that action:
  ``alert`` and ``beforeunload`` are accepted, a ``confirm`` is accepted
  unless its text looks destructive or money-related (delete, cancel, pay...),
  and a ``prompt`` is left open -- the agent answers it with the ``dialog``
  action, because we never invent the text.
* Any other dialog -- the owner's own browsing -- is left open, untouched, on
  his screen. Nothing is dismissed behind his back.
* An open dialog is surfaced to the agent: an observation returns it as
  ``open_dialog`` (without touching the blocked page), and any other agent
  action on that tab fails fast with code ``dialog_open`` (HTTP 423) instead
  of hanging until the action timeout. The agent resolves it with
  ``POST /sessions/{id}/actions/dialog`` (accept / dismiss / prompt text).
* Every dialog is recorded in ``session.dialog_log`` (text, type, what
  happened) so the agent can report what the site asked.

Popups: a window a page opens (``window.open`` -- the "Continue with Google"
account chooser, OAuth consent) during an agent action becomes the active tab
at the end of that action, and when the active tab closes (the popup closing
itself after sign-in) the tab that opened it becomes active again. Without
this the agent kept acting on a closed page and every call failed.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from ...action_errors import BrowserActionError
from ...utils import spawn_background_task, utc_now

if TYPE_CHECKING:
    from playwright.async_api import Dialog, Page

    from ...browser_manager import BrowserSession

logger = logging.getLogger(__name__)

DIALOG_LOG_LIMIT = 20
MESSAGE_LIMIT = 500
LIVENESS_PROBE_SECONDS = 1.0

# A confirm whose text matches this is never auto-accepted, even inside an
# agent action: deleting, cancelling, paying, leaving an account, sending money.
RISKY_CONFIRM = re.compile(
    r"delete|remove|erase|destroy|discard|cancel|unsubscribe|deactivate|terminate|"
    r"close (?:your |this )?account|pay\b|payment|purchase|buy\b|charge|subscribe|transfer|"
    r"حذف|احذف|امسح|مسح|إلغاء|الغاء|إيقاف|ادفع|دفع|شراء|اشتراك|تحويل|قفل الحساب",
    re.IGNORECASE,
)


def decide(dialog_type: str, message: str, *, agent_flow: bool) -> str:
    """``accept`` or ``leave_open`` -- pure, unit-tested."""
    if not agent_flow:
        return "leave_open"
    if dialog_type in {"alert", "beforeunload"}:
        return "accept"
    if dialog_type == "confirm":
        return "leave_open" if RISKY_CONFIRM.search(message or "") else "accept"
    return "leave_open"  # prompt: the agent must supply the text itself


def in_agent_flow(session: "BrowserSession") -> bool:
    return session.agent_action_depth > 0 or time.monotonic() < session.agent_dialog_grace_until


class BrowserDialogService:
    def __init__(self, manager: Any) -> None:
        self.manager = manager

    # --- listeners ---------------------------------------------------------------------

    def attach(self, page: "Page", session: "BrowserSession") -> None:
        page.on("dialog", lambda dialog: spawn_background_task(self._on_dialog(session, page, dialog)))
        page.on("popup", lambda popup: self._on_popup(session, page, popup))

    def _on_popup(self, session: "BrowserSession", opener: "Page", popup: "Page") -> None:
        try:
            session.popup_openers[popup] = opener
        except TypeError:  # pragma: no cover - a non-weakref-able test double
            pass
        if in_agent_flow(session):
            session.pending_popup = popup

    async def _on_dialog(self, session: "BrowserSession", page: "Page", dialog: "Dialog") -> None:
        dialog_type = str(getattr(dialog, "type", "") or "")
        message = str(getattr(dialog, "message", "") or "")[:MESSAGE_LIMIT]
        agent_flow = in_agent_flow(session)
        record: dict[str, Any] = {
            "type": dialog_type,
            "message": message,
            "default_value": str(getattr(dialog, "default_value", "") or "")[:MESSAGE_LIMIT],
            "url": getattr(page, "url", ""),
            "opened_at": utc_now(),
            "opened_during": "agent_action" if agent_flow else "owner_or_site",
            "outcome": "open",
        }
        self._log(session, record)
        if decide(dialog_type, message, agent_flow=agent_flow) == "accept":
            try:
                await dialog.accept()
                record["outcome"] = "auto_accepted"
            except Exception as exc:  # already closed (e.g. the owner answered it)
                logger.debug("auto-accept of %s dialog failed: %s", dialog_type, exc)
                record["outcome"] = "closed"
            return
        try:
            session.open_dialogs[page] = (dialog, record)
        except TypeError:  # pragma: no cover - a non-weakref-able test double
            logger.debug("page %r cannot hold an open dialog record", page)

    @staticmethod
    def _log(session: "BrowserSession", record: dict[str, Any]) -> None:
        session.dialog_log.append(record)
        if len(session.dialog_log) > DIALOG_LOG_LIMIT:
            del session.dialog_log[: len(session.dialog_log) - DIALOG_LOG_LIMIT]

    # --- open-dialog state ---------------------------------------------------------------

    async def open_dialog(self, session: "BrowserSession", page: "Page | None" = None) -> dict[str, Any] | None:
        """The still-open dialog on ``page`` (default: the active tab), or None.

        A dialog the owner answered by hand in the live view closes without any
        Playwright event, so an entry is confirmed by a cheap probe: evaluating
        anything on a page blocks while a dialog is up, and returns once it is gone.
        """
        target = page if page is not None else session.page
        try:
            entry = session.open_dialogs.get(target)
        except TypeError:
            return None
        if entry is None:
            return None
        _dialog, record = entry
        try:
            await asyncio.wait_for(target.evaluate("1"), timeout=LIVENESS_PROBE_SECONDS)
        except asyncio.TimeoutError:
            return dict(record)
        except Exception:
            # A closed page, or a navigation mid-probe: the dialog went with it.
            pass
        session.open_dialogs.pop(target, None)
        if record.get("outcome") == "open":
            record["outcome"] = "closed"
        return None

    async def raise_if_open(self, session: "BrowserSession", action_name: str) -> None:
        record = await self.open_dialog(session)
        if record is None:
            return
        raise BrowserActionError(
            "A dialog is open on this tab; answer it with the dialog action first.",
            code="dialog_open",
            action=action_name,
            status_code=423,
            retryable=True,
            url=getattr(session.page, "url", None),
            details={"dialog": record},
        )

    async def handle(self, session_id: str, *, accept: bool, prompt_text: str | None = None) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        async with session.lock:
            page = session.page
            try:
                entry = session.open_dialogs.get(page)
            except TypeError:
                entry = None
            if entry is None:
                return {"handled": False, "reason": "no_open_dialog", "recent_dialogs": session.dialog_log[-5:]}
            dialog, record = entry
            try:
                if accept:
                    if prompt_text is not None and record.get("type") == "prompt":
                        await dialog.accept(prompt_text)
                    else:
                        await dialog.accept()
                else:
                    await dialog.dismiss()
            except Exception as exc:
                session.open_dialogs.pop(page, None)
                record["outcome"] = "closed"
                logger.debug("dialog already closed when handled: %s", exc)
                return {"handled": False, "reason": "already_closed", "dialog": dict(record)}
            session.open_dialogs.pop(page, None)
            record["outcome"] = "accepted" if accept else "dismissed"
            record["handled_at"] = utc_now()
            await self.manager.audit.append(
                event_type="browser_action",
                status="ok",
                action="dialog",
                session_id=session.id,
                details={"type": record.get("type"), "accepted": accept},
            )
            try:
                await self.manager._settle(page)
            except Exception:
                pass
            return {"handled": True, "dialog": dict(record), "url": getattr(session.page, "url", "")}

    # --- active tab healing / popup follow ----------------------------------------------

    def heal_active_page(self, session: "BrowserSession") -> bool:
        """If the active tab has closed, make its opener (or the newest surviving
        tab) active again. Returns True when the active tab changed."""
        page = session.page
        is_closed = getattr(page, "is_closed", None)
        if not callable(is_closed) or is_closed() is not True:
            return False
        try:
            opener = session.popup_openers.get(page)
        except TypeError:
            opener = None
        candidates: list[Any] = []
        try:
            candidates = [p for p in self.manager.tabs.pages(session) if p is not page and p.is_closed() is False]
        except Exception:
            candidates = []
        replacement = None
        if opener is not None and opener in candidates:
            replacement = opener
        elif candidates:
            replacement = candidates[-1]
        if replacement is None:
            return False
        session.page = replacement
        self.manager._attach_page_listeners(replacement, session)
        return True

    async def follow_popup(self, session: "BrowserSession") -> dict[str, Any] | None:
        """Make the popup an agent action just opened the active tab."""
        popup = session.pending_popup
        session.pending_popup = None
        if popup is None or popup is session.page:
            return None
        try:
            if popup.is_closed():
                return None
            self.manager._attach_page_listeners(popup, session)
            try:
                await popup.wait_for_load_state("domcontentloaded", timeout=10_000)
            except Exception as exc:
                logger.debug("popup did not finish loading: %s", exc)
            session.page = popup
            if hasattr(popup, "bring_to_front"):
                await popup.bring_to_front()
        except Exception as exc:
            logger.debug("could not follow popup: %s", exc)
            return None
        return {"followed_popup": True, "url": getattr(popup, "url", "")}
