from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from ... import events as _events
from ...action_errors import (
    ERROR_PAGE_SOURCE_SHADOW,
    BrowserActionError,
    ErrorPageSourceError,
    SessionNotFoundError,
)
from ...action_records import wait_for_pending_record
from ...auth_policy import has_named_credentials
from ...browser_scripts import apply_stealth, cap_title
from ...models import SessionRecord, SessionStatus
from ...session_ownership import may_use
from ...utils import UTC, utc_now
from ..launch_flags import STORAGE_PARTITIONING_ON
from ..page_read import PageRead
from ..request_gate import RequestGateUnavailable, navigation_blocked_error, stopped_by_gate
from ..request_gates import CHROME_ERROR_SCHEME
from .session_creation import SessionCreation, new_session_id

if TYPE_CHECKING:
    from ...browser_manager import BrowserSession

logger = logging.getLogger(__name__)

# page.title() and page.evaluate() have no timeout, and a page running a script
# in an endless loop never answers them. These bound what the session list and
# close_session wait for, so one such page cannot hang either of them.
PAGE_TITLE_TIMEOUT_SECONDS = 2.0
# How long close_session waits for an action to release the session before it
# closes the browser context out from under it.
CLOSE_LOCK_WAIT_SECONDS = 10.0
# Each teardown step (trace, context, isolated runtime) gets at most this long.
TEARDOWN_STEP_TIMEOUT_SECONDS = 30.0
# Chrome's name for why a load failed (net::ERR_CONNECTION_REFUSED), the only part of Playwright's text an
# unloaded page's error repeats: the rest names the URL, a redirect's too, and carries a call log. Only where
# Playwright puts it ("Page.goto: net::ERR_X at <url>"), first on the line: a token inside a quoted URL is the page's.
_NET_ERROR = re.compile(r"(?:[A-Za-z]+\.[A-Za-z]+: )?(net::ERR_[A-Z0-9_]+)(?= at |$)")
# What failed to load, and what became of it, by the action that loaded it (_goto_or_blocked's callers).
_LOAD_FAILED = {
    "create_session": "The session's start_url did not load ({reason}), so the session was closed. Check that it "
    "answers, or create the session without one and navigate it.",
    "enable_shadow_browse": "The page did not load in the shadow browser ({reason}), so the shadow browser was "
    "closed. Try again, or navigate the session first.",
}
_TIMED_OUT = re.compile(r"Timeout \d+ms exceeded")


def _load_failed(exc: PlaywrightError, *, action: str) -> BrowserActionError:
    """A page the controller itself loaded (a start_url, shadow browse's page) that failed for a reason of its own."""
    text = getattr(exc, "message", None) or str(exc)
    first_line = text.splitlines()[0] if text else ""
    net_error = _NET_ERROR.match(first_line)
    if net_error:
        reason = net_error.group(1)
    elif isinstance(exc, PlaywrightTimeoutError) or _TIMED_OUT.search(first_line):
        reason = "it timed out"
    else:
        reason = "the browser could not open it"
    # The operator's log keeps what the agent is not told: a failure that is no load failure is the one to look at.
    level = logging.INFO if reason != "the browser could not open it" else logging.WARNING
    logger.log(level, "%s: the page did not load: %s", action, first_line[:300])
    return BrowserActionError(
        _LOAD_FAILED[action].format(reason=reason), action=action, status_code=400, retryable=True
    )


def _on_error_page(page: Any) -> bool:
    return str(getattr(page, "url", "") or "").startswith(CHROME_ERROR_SCHEME)


class BrowserSessionService(SessionCreation):
    """Encapsulates live session lifecycle and durable session summaries."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager
        # Creates past the limit check but not yet in manager.sessions. The
        # check and the insert are separated by browser startup and page
        # loads, so without counting these, concurrent creates all passed the
        # check and MAX_SESSIONS was exceeded (with the default of 1, two
        # sessions shared the one visible desktop).
        self._creating = 0

    async def list(self) -> list[dict[str, Any]]:
        session_map = {
            record.id: record.model_dump()
            for record in await self.manager.session_store.list()
            if may_use(record.owner)
        }
        # A snapshot: summaries await, and a session created or closed meanwhile
        # changed the dict mid-iteration ("dictionary changed size").
        for session in list(self.manager.sessions.values()):
            if not may_use(session.owner):
                continue
            summary = await self.manager._session_summary(session)
            session_map[summary["id"]] = summary
        return sorted(
            session_map.values(),
            key=lambda item: (item.get("created_at") or "", item.get("id") or ""),
            reverse=True,
        )

    def prepare_dirs(self, session_id: str) -> tuple[Path, Path, Path]:
        artifact_dir = self.manager.artifacts.prepare_session_dir(session_id)
        auth_dir = self.auth_root(session_id)
        upload_dir = self.upload_root(session_id)
        auth_dir.mkdir(parents=True, exist_ok=True)
        upload_dir.mkdir(parents=True, exist_ok=True)
        return artifact_dir, auth_dir, upload_dir

    async def get(self, session_id: str) -> "BrowserSession":
        session = self.manager.sessions.get(session_id)
        if session is None:
            status, reason = await self._recorded_end(session_id)
            raise SessionNotFoundError(session_id, status=status, reason=reason)
        if not may_use(session.owner):
            # Another operator's session answers as if it did not exist.
            raise SessionNotFoundError(session_id)
        return session

    async def get_record(self, session_id: str) -> dict[str, Any]:
        session = self.manager.sessions.get(session_id)
        if session is not None:
            if not may_use(session.owner):
                raise SessionNotFoundError(session_id)
            return await self.manager._session_summary(session)
        try:
            record = await self.manager.session_store.get(session_id)
        except KeyError:
            raise SessionNotFoundError(session_id) from None
        if not may_use(record.owner):
            raise SessionNotFoundError(session_id)
        return record.model_dump()

    async def find_owner(self, session_id: str) -> tuple[bool, str | None]:
        """(known, owner) for a session, live or recorded; known is False when it is neither."""
        session = self.manager.sessions.get(session_id)
        if session is not None:
            return True, session.owner
        try:
            record = await self.manager.session_store.get(session_id)
        except KeyError:
            return False, None
        return True, record.owner

    async def owner_of(self, session_id: str) -> str | None:
        """Who owns a session, live or recorded; None when unowned or unknown."""
        return (await self.find_owner(session_id))[1]

    async def accessible(self, session_id: str) -> bool:
        """Whether the current operator may use what belongs to this session id.

        For entry points that address a session by id but never resolve it
        through get(): diagnostics that read its files, approvals, agent jobs.
        Under named credentials an id with no live session and no record is
        refused as well, the rule /artifacts applies: it has no owner to check,
        and answering it otherwise told another operator's session from none.
        """
        known, owner = await self.find_owner(session_id)
        return may_use(owner) and (known or not has_named_credentials(self.manager.settings))

    async def ensure_accessible(self, session_id: str) -> None:
        """Refuse a session the current operator may not use (see accessible), as if it did not exist."""
        if not await self.accessible(session_id):
            raise SessionNotFoundError(session_id)

    async def _recorded_end(self, session_id: str) -> tuple[str | None, str | None]:
        """Status, and the controller's own close reason, of a session that is not live, from its record, if any.

        Only consulted on the failure path, to tell a caller *why* the id does
        not work: closed, interrupted, or never existed.
        """
        try:
            record = await self.manager.session_store.get(session_id)
        except Exception:  # message enrichment only; any store failure means "unknown"
            logger.debug("no readable record for session %s", session_id, exc_info=True)
            return None, None
        return (record.status, record.close_reason) if may_use(record.owner) else (None, None)

    async def close(self, session_id: str, *, reason: str | None = None) -> dict[str, Any]:
        """Close a session; ``reason`` is the controller's own (SessionRecord.close_reason), the next call names it.

        The teardown runs to its end even when the caller is cancelled (a shutdown, or a cancel of the task that
        awaited this; a REST client that disconnects does not cancel it): a
        half-done close left the audit event, the witness receipt and the stored record unwritten. ``closed`` in
        the answer is True (the session is gone); ``teardown`` is "partial" when a step failed, and ``failed_steps``
        names those steps ("complete" and an empty list otherwise).
        """
        session = await self.manager.get_session(session_id)
        # One protected unit from here: the busy path of _lock_for_close already closes the context, so a cancel
        # between it and _close_locked left a registered session with no close record, audit event or receipt.
        # Every wait inside is bounded.
        return await self._run_to_completion(self._close(session, reason))

    async def _close(self, session: "BrowserSession", reason: str | None) -> dict[str, Any]:
        locked = await self._lock_for_close(session)
        try:
            if self.manager.sessions.get(session.id) is not session:
                # Closed by a concurrent call while this one waited for the
                # lock. Carrying on released the tunnel and runtime twice and
                # wrote a second close to the audit log and witness chain.
                raise SessionNotFoundError(session.id, status="closed")
            # A cancelled action's record may still be finishing behind it: the close is recorded after it.
            await wait_for_pending_record(session)
            return await self._close_locked(session, reason)
        finally:
            if locked:
                session.lock.release()

    @staticmethod
    async def _run_to_completion(work: Any) -> Any:
        """Await ``work`` as its own task, which a cancel aimed at the caller does not stop; the cancel is raised
        once the work has finished (as the tunnel release does)."""
        task = asyncio.create_task(work)
        cancelled: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancelled = exc
        result = task.result()
        if cancelled is not None:
            raise cancelled
        return result

    async def _close_locked(self, session: "BrowserSession", reason: str | None) -> dict[str, Any]:
        session_id = session.id
        failed_steps: list[str] = []
        try:
            if session.tunnel is not None:
                await self._teardown_step(
                    session_id,
                    "release session tunnel",
                    lambda: self.manager.tunnel_broker.release(session.tunnel),
                    failed_steps,
                )
            try:
                summary = await self.manager._session_summary(session, status="closed", live=False)
            except BaseException:
                # The session is closing all the same: its record says so, with the reason the next call names.
                await self._persist_closed_without_summary(session, reason)
                raise
            finally:
                # Released even when the summary fails: the context must not outlive the session.
                await self._release_resources(session, failed_steps)
        finally:
            # Whatever teardown step failed, the session is gone. It used to
            # stay registered when context.close() or the runtime release
            # raised (a crashed browser, a docker error), every retry failed
            # the same way, and it held a MAX_SESSIONS slot until restart.
            self.manager.sessions.pop(session_id, None)
            _events.emit_session(session_id, "closed")
        if self.manager._session_closed_hook is not None:
            try:
                await self.manager._session_closed_hook(session_id)
            except Exception as exc:
                logger.warning("session closed hook failed for %s: %s", session_id, exc)
        status = "partial" if failed_steps else "ok"
        details: dict[str, Any] = {
            "trace_path": str(session.trace_path),
            "isolation_mode": session.isolation_mode,
            "browser_node": session.browser_node_name,
        }
        if failed_steps:
            details["failed_steps"] = list(failed_steps)
        await self.manager.audit.append(
            event_type="session_closed",
            status=status,
            action="close_session",
            session_id=session.id,
            details=details,
        )
        await self.manager._record_witness_receipt(
            session,
            event_type="session",
            status=status,
            action="close_session",
            action_class="control",
            metadata=dict(details),
        )
        # The hosted copy of the close receipt goes in the background; the closed record shows how it went (bounded).
        await self.manager.witness_bridge.wait_for_delivery(session.id)
        summary["witness_remote"] = session.witness_remote_state.model_dump()
        summary["close_reason"] = reason
        await self.manager.session_store.upsert(SessionRecord.model_validate(summary))
        return {
            # The session is gone by now, whatever failed: `closed` says so. A step that failed is named in
            # failed_steps and makes the teardown partial (the audit event and receipt say partial too).
            "closed": True,
            "teardown": "partial" if failed_steps else "complete",
            "failed_steps": failed_steps,
            "trace_path": str(session.trace_path),
            "session": summary,
        }

    async def _persist_closed_without_summary(self, session: "BrowserSession", reason: str | None) -> None:
        store = self.manager.session_store
        try:
            try:
                record = await store.get(session.id)
            except (KeyError, ValueError):
                # No record, or one that cannot be parsed (W8-2): the closed record is built from the session and
                # written over it. An OSError still reaches the handler below.
                record = SessionRecord(
                    id=session.id,
                    name=session.name,
                    created_at=session.created_at.isoformat(),
                    updated_at=session.created_at.isoformat(),
                    status="closed",
                    current_url="",
                    title="",
                    artifact_dir=str(session.artifact_dir),
                    takeover_url=session.takeover_url,
                    remote_access={},
                    owner=getattr(session, "owner", None),
                )
            await store.upsert(
                record.model_copy(
                    update={"status": "closed", "live": False, "close_reason": reason, "updated_at": utc_now()}
                )
            )
        except Exception as exc:
            logger.error("could not record session %s as closed: %s", session.id, f"{type(exc).__name__}: {exc}"[:200])

    async def _lock_for_close(self, session: "BrowserSession") -> bool:
        """Take the session lock for closing, even from an action stuck on its page.

        An action waiting on a page that never answers holds the lock forever.
        Closing the browser context fails that wait, which releases the lock.
        Returns whether the lock is held; close goes ahead without it rather
        than leave the session — and its MAX_SESSIONS slot — stuck until restart.
        """
        try:
            await asyncio.wait_for(session.lock.acquire(), timeout=CLOSE_LOCK_WAIT_SECONDS)
            return True
        except TimeoutError:
            logger.warning("session %s is busy; closing its browser context so it can be closed", session.id)
        await self._teardown_step(session.id, "close a stuck browser context", session.context.close)
        try:
            await asyncio.wait_for(session.lock.acquire(), timeout=CLOSE_LOCK_WAIT_SECONDS)
            return True
        except TimeoutError:
            logger.warning("session %s is still busy after its context closed; closing it anyway", session.id)
            return False

    async def _release_resources(self, session: "BrowserSession", failed_steps: list[str]) -> None:
        """Tear down everything a live session holds, each step independently.

        A failing step is logged and the rest still run, so one broken resource
        does not strand the others (the runtime container, the tunnel process).
        The label of each step that failed is appended to ``failed_steps``.
        """
        await self._teardown_step(
            session.id,
            "stop trace recording",
            lambda: self.manager.observation.stop_trace_recording(session),
            failed_steps,
        )
        if session.network_inspector is not None:
            session.network_inspector.detach()
            session.network_inspector = None
        # The window-birth watcher is per-browser and shared with other sessions on it; drop only this
        # session's context mapping (the watcher itself is released with its browser).
        self.manager.window_births.forget_session(session)
        await self._teardown_step(session.id, "close browser context", session.context.close, failed_steps)
        if session.browser is not None and session.owns_browser:
            if not await self._close_owned_browser(session.browser, session.id):
                failed_steps.append("close isolated browser")
        if session.runtime is not None:
            await self._teardown_step(
                session.id,
                "release isolated runtime",
                lambda: self.manager.runtime_provisioner.release(session.runtime),
                failed_steps,
            )

    async def _close_owned_browser(self, browser: Any, session_id: str) -> bool:
        """Close a browser the session owns, then release its gate; False when the close failed. Until the close has
        succeeded the gate stays attached, still gating whatever runs there, and the close taking the gate's session
        down is no loss. The steps around the close are bounded and logged and never raise (R5C-11): a failed create's
        or shadow browse's rollback runs this, and the rest of it, and its caller's own error, came after."""
        gates = self.manager.request_gates
        self._mark_gate_ending(browser, session_id, True)
        try:
            await asyncio.wait_for(browser.close(), timeout=TEARDOWN_STEP_TIMEOUT_SECONDS)
        except Exception as exc:
            self._mark_gate_ending(browser, session_id, False)
            logger.error(
                "the isolated browser of session %s did not close (%s): its pre-request gate stays attached",
                session_id,
                f"{type(exc).__name__}: {exc}"[:200],
            )
            return False
        await self._teardown_step(
            session_id, "release the pre-request gate of its browser", lambda: gates.release(browser)
        )
        await self._teardown_step(
            session_id,
            "release the window-birth watcher of its browser",
            lambda: self.manager.window_births.release(browser),
        )
        return True

    def _mark_gate_ending(self, browser: Any, session_id: str, ending: bool) -> None:
        """request_gates.ending, logged instead of raised: it only marks the gate, and the close goes on either way."""
        try:
            self.manager.request_gates.ending(browser, ending)
        except Exception as exc:
            logger.warning(
                "could not mark the pre-request gate of session %s's browser as %s: %s",
                session_id,
                "ending" if ending else "still in use",
                exc,
            )

    @staticmethod
    async def _teardown_step(session_id: str, label: str, step: Any, failed_steps: list[str] | None = None) -> bool:
        """Run one teardown step, bounded; on failure log it and append ``label`` to ``failed_steps``. Used by
        close_session's teardown and by a failed create's rollback, whose session may not exist yet."""
        try:
            await asyncio.wait_for(step(), timeout=TEARDOWN_STEP_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning("gave up waiting to %s for session %s", label, session_id)
        except Exception as exc:
            logger.warning("failed to %s for session %s: %s", label, session_id, exc)
        else:
            return True
        if failed_steps is not None:
            failed_steps.append(label)
        return False

    async def fork(
        self,
        session_id: str,
        *,
        name: str | None = None,
        start_url: str | None = None,
        proxy_persona: str | None = None,
        user_agent: str | None = None,
        protection_mode: str | None = None,
    ) -> dict[str, Any]:
        """Fork a session: clone cookies + localStorage state into a new session.

        The fork keeps the source's proxy persona, User-Agent, protection mode and TOTP settings, as create
        would have set them; a value the caller names wins. A source created with a raw request proxy (not a
        persona) stored none to carry, so its fork gets the deployment default.
        """
        session = await self.manager.get_session(session_id)
        # Refused on the limit before the source's cookies are written to disk; create_session keeps the
        # authoritative check, which holds against two forks racing.
        self.manager._check_session_limit()
        async with session.lock:
            # As shadow browse: without a start_url the fork opens the page's URL with the session's
            # cookies, and a refused page chose that URL, so a refused page is not forked onto it and the
            # URL is recorded as shown. A start_url the caller names is judged by create_session.
            # Chrome's error page is no URL to open either: like about:blank and data:, it forks with no start URL
            # (the scheme fallback below). It is still judged as a page: chrome-error is an allowed scheme, so an
            # unrefused one passes, and a refused one answers 403 as about:blank in the same state does (B3-6).
            if start_url is None:
                self.manager._assert_page_allowed(session)
            current_url = self.manager._shown_url(session.page)
            # Export through AuthStateManager so the state file is encrypted
            # at rest whenever an encryption key is configured.
            fork_auth_path = session.auth_dir / f"fork_{uuid4().hex[:8]}.json"
            self.manager.auth_profiles.mark_session_auth_dir(session.auth_dir, session.owner)
            auth_info = await self.manager.auth_state.write_storage_state(session.context, fork_auth_path)

        # A blank or data: page is no URL create_session can open (it refuses every non-http(s) scheme):
        # the fork starts on a blank page with the source's cookies instead.
        start_url = start_url or (current_url if current_url.lower().startswith(("http://", "https://")) else None)
        # Create the new session using the forked state
        try:
            forked = await self.manager.create_session(
                name=name or f"fork-of-{session.name}",
                start_url=start_url,
                storage_state_path=auth_info["path"],
                proxy_persona=proxy_persona or session.proxy_persona,
                user_agent=user_agent or session.user_agent,
                protection_mode=protection_mode or session.protection_mode,
                totp_secret=session.totp_secret,
                totp_hosts=list(session.totp_hosts) or None,
            )
        finally:
            # The new context has read the state by now, or never will: a copy left
            # behind is the source session's cookies, plaintext without a key, on
            # disk long after both sessions close.
            Path(auth_info["path"]).unlink(missing_ok=True)
        forked["forked_from"] = session_id
        await self.manager.audit.append(
            event_type="session_forked",
            status="ok",
            action="fork_session",
            session_id=session_id,
            details={"new_session_id": forked["id"], "start_url": start_url},
        )
        return forked

    async def enable_shadow_browse(self, session_id: str) -> dict[str, Any]:
        """Switch a session to headed (visible) mode for debugging.

        Because Playwright cannot flip headless→headed mid-session, this:
        1. Exports state (cookies + storage) from the running session
        2. Launches a new LOCAL headed Chromium process
        3. Creates a new BrowserSession with that state and the same URL
        4. Returns the new session's info (the old session keeps running)

        The caller is expected to close the original session when done debugging.

        A shadow session is exempt from MAX_SESSIONS, unlike a fork: it is a debugging copy, off by default
        (SHADOW_BROWSE_ENABLED), and it runs in the controller's own local browser, not on the shared browser node
        the limit protects.
        """
        manager = self.manager
        if not manager.settings.shadow_browse_enabled:
            raise RuntimeError("Shadow browsing is disabled (SHADOW_BROWSE_ENABLED=false)")
        if manager.settings.session_isolation_mode == "docker_ephemeral":
            # The shadow is a local headed Chromium in the controller's own process tree: under docker_ephemeral
            # it would run outside the isolation every session is promised (own container, network, profile).
            raise RuntimeError(
                "Shadow browsing is not available under SESSION_ISOLATION_MODE=docker_ephemeral: its headed "
                "Chromium would run outside the session isolation."
            )
        if manager.playwright is None:
            raise RuntimeError("Playwright not started")
        # Chromium refuses to start sandboxed as root; refuse rather than drop the sandbox.
        # Windows has no geteuid, and no root to refuse.
        geteuid = getattr(os, "geteuid", None)
        if geteuid is not None and geteuid() == 0:
            raise RuntimeError(
                "Shadow browsing launches a sandboxed headed Chromium, and Chromium cannot run sandboxed as root. "
                "Run the controller as a regular user to use shadow browsing."
            )

        session = await manager.get_session(session_id)
        async with session.lock:
            # The shadow opens the page's URL in a second browser: a refused page is not read
            # (its URL is the refused page's choice), and only an allowed http(s) URL is opened.
            if _on_error_page(session.page):
                # Before the state is exported or a browser launched: Chrome's error page is no URL to open.
                raise ErrorPageSourceError(ERROR_PAGE_SOURCE_SHADOW)
            manager._assert_page_allowed(session)
            current_url = manager._shown_url(session.page)
            if current_url != "about:blank":
                manager._assert_url_allowed(current_url)
            # In-memory export: shadow state never touches disk.
            storage_state = await session.context.storage_state()

        from ...browser_manager import BrowserSession

        shadow_session_id = new_session_id()
        context_kwargs: dict[str, Any] = {
            "viewport": {
                "width": manager.settings.default_viewport_width,
                "height": manager.settings.default_viewport_height,
            },
            "accept_downloads": True,
            "storage_state": storage_state,
        }

        # A local headed browser process, sandboxed: shadow browse is a host-run feature (the
        # controller image has no Chromium or display), and on a host the sandbox works.
        try:
            headed_browser = await manager.playwright.chromium.launch(
                headless=False,
                chromium_sandbox=True,
                args=[STORAGE_PARTITIONING_ON],
            )
        except Exception as exc:
            # Chromium's text can name host paths, and over MCP this message reaches the agent: it is logged here.
            logger.warning("shadow browse: the sandboxed headed Chromium did not start: %s", exc)
            raise RuntimeError(
                "Shadow browsing could not start its sandboxed headed Chromium. On Linux the sandbox needs "
                "unprivileged user namespaces (sysctl kernel.unprivileged_userns_clone=1, or an AppArmor profile "
                "that allows them where Ubuntu restricts them), and headed mode needs a display. The controller "
                "log has Chromium's own error."
            ) from exc
        context = None
        shadow_session = None
        try:
            # Made once the browser is up, inside the guarded block: any failure from here removes them again.
            artifact_dir, auth_dir, upload_dir = self.prepare_dirs(shadow_session_id)
            # Launched by the controller (not an attach mode), and gated before its first context.
            await manager.request_gates.attach(headed_browser, attach_mode=False)
            gate = await manager.request_gates.require(headed_browser)
            context = await headed_browser.new_context(**context_kwargs)
            # On the context, as in create_session: every tab and popup needs both.
            context.set_default_timeout(manager.settings.action_timeout_ms)
            if manager.settings.stealth_enabled:
                await apply_stealth(context)
            page = await context.new_page()
            gate_context_id = await gate.register_context(context, page) if gate is not None else None
            if gate is not None:
                await manager.request_gates.bypass_service_workers(page)

            shadow_session = BrowserSession(
                id=shadow_session_id,
                name=f"shadow-{session.name}",
                created_at=datetime.now(UTC),
                context=context,
                page=page,
                artifact_dir=artifact_dir,
                auth_dir=auth_dir,
                upload_dir=upload_dir,
                takeover_url=manager.settings.takeover_url,
                trace_path=artifact_dir / "trace.zip",
                browser=headed_browser,
                owns_browser=True,
                headless=False,
                # Same logins as the source, so the same operator and nobody else.
                owner=session.owner,
                gate_context_id=gate_context_id,
            )
            manager._attach_page_listeners(page, shadow_session)
            # As in create_session: popups need the listeners too, the page guard among them.
            manager.diagnostics.watch_context(context, shadow_session)
            await manager.window_births.watch(shadow_session, context, page, context_id=gate_context_id)
            manager.sessions[shadow_session_id] = shadow_session

            if current_url != "about:blank":
                await self._goto_or_blocked(headed_browser, page, current_url, action="enable_shadow_browse")
            if gate is not None and not gate.live:
                # Lost before the session was in manager.sessions, where containment looks: refused, as in create.
                raise RequestGateUnavailable(
                    "The pre-request gate detached from the shadow browser while it was starting, so it was closed. "
                    "Enable shadow browsing again."
                )
            await manager._persist_session(shadow_session, status="active")
        except BaseException:
            # BaseException so a cancelled shadow browse (the caller gave up, shutdown) also closes the browser.
            manager.sessions.pop(shadow_session_id, None)
            if shadow_session is not None:
                # As create's rollback: a session the window-birth watcher registered is forgotten with it.
                manager.window_births.forget_session(shadow_session)
            # One protected unit, as cleanup_failed's (W9-11): a cancel at any step left the browser, the directories
            # or a record the persist already wrote reading active; the cancel is raised once the rollback is done.
            try:
                await self._run_to_completion(
                    self._roll_back_shadow(context, headed_browser, shadow_session, shadow_session_id)
                )
            except Exception as exc:  # the error to raise is the one that started the rollback (R5C-11)
                logger.error("the rollback of shadow session %s failed: %s", shadow_session_id, exc)
            raise

        await manager.audit.append(
            event_type="shadow_browse_started",
            status="ok",
            action="enable_shadow_browse",
            session_id=session_id,
            details={"shadow_session_id": shadow_session_id, "url": current_url},
        )
        return {
            "shadow_session_id": shadow_session_id,
            "original_session_id": session_id,
            "url": current_url,
            "headless": False,
            "note": "Headed Chrome launched. Close the original session when done debugging.",
        }

    async def _roll_back_shadow(
        self, context: Any, headed_browser: Any, shadow_session: "BrowserSession | None", shadow_session_id: str
    ) -> None:
        """enable_shadow_browse's rollback, run to completion by its caller."""
        # As create's rollback: the context first, then the browser, and its gate only once that has closed.
        if context is not None:
            # Bounded as each teardown step is: a stalled close must not keep the original error from
            # returning, nor the browser from being closed.
            try:
                await asyncio.wait_for(context.close(), timeout=TEARDOWN_STEP_TIMEOUT_SECONDS)
            except Exception as exc:
                logger.warning("failed to close the shadow context during rollback: %s", f"{type(exc).__name__}: {exc}")
        await self._close_owned_browser(headed_browser, shadow_session_id)
        # The directories, and the record a persist may already have written, closed as create_failed.
        await self._forget_failed_session_state(shadow_session, shadow_session_id)

    async def _goto_or_blocked(self, browser: Any, page: Any, url: str, *, action: str) -> None:
        """The controller's own navigation outside the action pipeline, then the page's settle, a stop by the gate
        mapped to the block it is: the goto's own failure (R6's rule), or a navigation the page made once loaded
        (a meta refresh, a script) that the gate stopped while it settled."""
        gates = self.manager.request_gates
        gate = gates.get(browser)
        if gates.enabled() and (gate is None or not gate.live):
            # Before the navigation, not after: on a lost gate it would go out unjudged.
            raise RequestGateUnavailable(
                "The pre-request gate detached from the browser while the session was starting, so it was closed. "
                "Try again."
            )
        since = gates.mark(browser)
        try:
            with self.manager.page_provenance.controller_navigation(page) as navigation:
                navigation.response = await page.goto(url, wait_until="domcontentloaded")
        except PlaywrightError as exc:
            if not stopped_by_gate(exc):
                # A load that failed (refused, unresolved, timed out): retryable, as navigate's own failure, and
                # typed, so the route answers it rather than with a 500. Never a block (R6's rule).
                raise _load_failed(exc, action=action) from exc
            stopped = await gates.blocks_on_page(browser, page, since=since)
            raise navigation_blocked_error(stopped[-1] if stopped else None, action=action) from exc
        await self.manager._settle(page)
        stopped = await gates.blocks_on_page(browser, page, since=since)
        if stopped:
            # The page loaded, then left for a refused host: it now shows Chrome's error page for that block.
            raise navigation_blocked_error(stopped[-1], action=action)

    async def _page_snapshot(self, session: "BrowserSession") -> tuple[str, str, bool]:
        page = session.page
        is_closed = getattr(page, "is_closed", None)
        if callable(is_closed) and is_closed():
            return "", "", False
        # Every session summary carries the title: a page ALLOWED_HOSTS refuses
        # (see _page_refused) reports its URL, not what it shows.
        if self.manager._page_refused(page):
            return self.manager._shown_url(page), "", True
        read = PageRead(self.manager, session)
        try:
            title = await asyncio.wait_for(page.title(), timeout=PAGE_TITLE_TIMEOUT_SECONDS)
        except TimeoutError:
            # Live, but its renderer is not answering (a script in an endless
            # loop). Without a bound this hung every listing and every close. It may
            # have moved meanwhile, so its URL is shown as for any other read.
            return self.manager._shown_url(page), "", True
        except PlaywrightError as exc:
            logger.debug("page snapshot failed for session %s: %s", session.id, exc)
            return "", "", False
        # And after the read: the page can move while its title is read, and come back.
        if read.moved():
            return self.manager._shown_url(page), "", True
        return page.url, cap_title(title), True

    async def summary(
        self,
        session: "BrowserSession",
        *,
        status: SessionStatus = "active",
        live: bool = True,
    ) -> dict[str, Any]:
        current_url, title, page_live = await self._page_snapshot(session)
        if not page_live and status == "active":
            status = "interrupted"
            live = False
        return {
            "id": session.id,
            "name": session.name,
            "created_at": session.created_at.isoformat(),
            "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "status": status,
            "live": live,
            "current_url": current_url,
            "title": title,
            "artifact_dir": str(session.artifact_dir),
            "takeover_url": self.manager._current_takeover_url(session),
            "remote_access": self.manager.remote_access.session_info(session),
            "isolation": self.isolation_payload(session),
            "auth_state": self.manager.auth_profiles.session_auth_state_info(session),
            "downloads": session.downloads[-20:],
            "last_action": session.last_action,
            "trace_path": str(session.trace_path),
            "proxy_persona": session.proxy_persona,
            "protection_mode": session.protection_mode,
            "witness_remote": session.witness_remote_state.model_dump(),
            "owner": session.owner,
        }

    async def get_summary(self, session_id: str) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        return await self.manager._session_summary(session)

    async def persist(self, session: "BrowserSession", *, status: SessionStatus) -> None:
        # A persist that lands after close() has stored "closed" would put the session back to active: an
        # active record of a session that is gone. Checked before the summary and again after it, which awaits.
        if status == "active" and self.manager.sessions.get(session.id) is not session:
            return
        summary = await self.manager._session_summary(
            session,
            status=status,
            live=status == "active",
        )
        if status == "active" and self.manager.sessions.get(session.id) is not session:
            return
        await self.manager.session_store.upsert(SessionRecord.model_validate(summary))

    def isolation_payload(self, session: "BrowserSession") -> dict[str, Any]:
        payload: dict[str, Any] = {
            "mode": session.isolation_mode,
            "browser_node": session.browser_node_name,
            "shared_takeover_surface": session.shared_takeover_surface,
            "shared_browser_process": session.shared_browser_process,
            "max_live_sessions_per_browser_node": session.max_live_sessions_per_browser_node,
            "state_roots": {
                "artifact_dir": str(session.artifact_dir),
                "auth_dir": str(session.auth_dir),
                "upload_dir": str(session.upload_dir),
            },
        }
        if session.runtime is not None:
            payload["runtime"] = {
                "container_id": session.runtime.container_id,
                "container_name": session.runtime.container_name,
                "network": session.runtime.network_name,
                "profile_dir": str(session.runtime.profile_dir),
                "downloads_dir": str(session.runtime.downloads_dir),
                "ws_endpoint_file": str(session.runtime.ws_endpoint_file),
                "novnc_port": session.runtime.novnc_port,
            }
        return payload

    async def maybe_provision_tunnel(self, session: "BrowserSession") -> None:
        manager = self.manager
        if session.isolation_mode != "docker_ephemeral" or session.runtime is None:
            return
        if not manager.tunnel_broker.enabled:
            return
        if session.runtime.novnc_port is None or not manager.remote_access.takeover_url_is_local_only(
            session.takeover_url
        ):
            return
        try:
            session.tunnel = await manager.tunnel_broker.provision(
                session.id,
                local_host=session.runtime.tunnel_local_host,
                local_port=session.runtime.tunnel_local_port,
            )
            session.tunnel_error = None
        except Exception as exc:
            session.tunnel = None
            session.tunnel_error = "isolated tunnel provisioning failed"
            logger.warning("failed to provision isolated tunnel for session %s: %s", session.id, exc)

    @staticmethod
    def auth_root_for(base_root: str, session_id: str) -> Path:
        return Path(base_root).resolve() / session_id

    @staticmethod
    def upload_root_for(base_root: str, session_id: str) -> Path:
        return Path(base_root).resolve() / session_id

    def auth_root(self, session_id: str) -> Path:
        return self.auth_root_for(self.manager.settings.auth_root, session_id)

    def upload_root(self, session_id: str) -> Path:
        return self.upload_root_for(self.manager.settings.upload_root, session_id)
