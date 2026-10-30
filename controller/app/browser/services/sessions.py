from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from playwright.async_api import Error as PlaywrightError

from ... import events as _events
from ...action_errors import SessionNotFoundError
from ...auth_policy import has_named_credentials
from ...browser_scripts import apply_stealth
from ...models import SessionRecord, SessionStatus, resolve_totp_hosts
from ...network_inspector import NetworkInspector
from ...session_ownership import may_use, verified_operator
from ...url_safety import refuse_another_navigation_host
from ...utils import UTC
from ..launch_flags import STORAGE_PARTITIONING_ON
from ..page_read import PageRead
from ..request_gate import RequestGateUnavailable, navigation_blocked_error, stopped_by_gate

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext

    from ...browser_manager import BrowserSession
    from ...session_isolation import IsolatedBrowserRuntime

logger = logging.getLogger(__name__)

# A session id is this many lowercase hex digits of a uuid4 (new_session_id). An upload subfolder
# named like one is treated as that session's directory (see uploads._require_directory_access).
SESSION_ID_HEX_DIGITS = 12
_SESSION_ID_SHAPE = re.compile(rf"[0-9a-f]{{{SESSION_ID_HEX_DIGITS}}}")


def new_session_id() -> str:
    return uuid4().hex[:SESSION_ID_HEX_DIGITS]


def is_session_id(name: str) -> bool:
    """Whether a name has the shape new_session_id gives, whether or not such a session exists."""
    return _SESSION_ID_SHAPE.fullmatch(name) is not None


# page.title() and page.evaluate() have no timeout, and a page running a script
# in an endless loop never answers them. These bound what the session list and
# close_session wait for, so one such page cannot hang either of them.
PAGE_TITLE_TIMEOUT_SECONDS = 2.0
# How long close_session waits for an action to release the session before it
# closes the browser context out from under it.
CLOSE_LOCK_WAIT_SECONDS = 10.0
# Each teardown step (trace, context, isolated runtime) gets at most this long.
TEARDOWN_STEP_TIMEOUT_SECONDS = 30.0


class BrowserSessionService:
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

    async def create(
        self,
        *,
        name: str | None = None,
        start_url: str | None = None,
        storage_state_path: str | None = None,
        auth_profile: str | None = None,
        memory_profile: str | None = None,
        proxy_persona: str | None = None,
        request_proxy_server: str | None = None,
        request_proxy_username: str | None = None,
        request_proxy_password: str | None = None,
        user_agent: str | None = None,
        protection_mode: str | None = None,
        totp_secret: str | None = None,
        totp_hosts: list[str] | None = None,
    ) -> dict[str, Any]:
        if storage_state_path and auth_profile:
            raise ValueError("Provide auth_profile or storage_state_path, not both")
        if proxy_persona and any((request_proxy_server, request_proxy_username, request_proxy_password)):
            raise ValueError("Provide proxy_persona or explicit proxy_server credentials, not both")
        if start_url:
            # Also for a start_url no request model read: a cron job's, stored by an earlier version.
            refuse_another_navigation_host(start_url, field_name="start_url")
            self.manager._assert_url_allowed(start_url)
        resolved_totp_hosts = tuple(resolve_totp_hosts(totp_hosts, start_url)) if totp_secret else ()
        resolved_protection_mode = protection_mode or self.manager.settings.witness_protection_mode_default
        self.manager._check_session_limit()

        session_id = new_session_id()
        artifact_dir, auth_dir, upload_dir = self.prepare_dirs(session_id)
        source_path: Path | None = None

        if proxy_persona:
            if self.manager.proxy_store is None:
                raise RuntimeError("No PROXY_PERSONA_FILE configured")
            resolved_proxy = self.manager.proxy_store.resolve_proxy(proxy_persona)
            proxy_server = resolved_proxy.get("server")
            proxy_username = resolved_proxy.get("username")
            proxy_password = resolved_proxy.get("password")
        else:
            proxy_server = request_proxy_server or self.manager.settings.default_proxy_server
            proxy_username = request_proxy_username or self.manager.settings.default_proxy_username
            proxy_password = request_proxy_password or self.manager.settings.default_proxy_password

        context_kwargs = self.manager._build_context_kwargs(user_agent, proxy_server, proxy_username, proxy_password)

        if auth_profile:
            # The takeover the report described: opening a session against
            # someone else's stored profile drives a browser already logged in
            # as them. Authorize before the state is ever loaded.
            self.manager.auth_profiles.require_access(auth_profile, action="opening a session from an auth profile")
            source_path = self.manager.auth_profiles.resolve_state_path(auth_profile, must_exist=True)
        elif storage_state_path:
            source_path = self.manager.auth_profiles.storage_state_source(storage_state_path)
            await self.manager.auth_profiles.require_session_record_access(source_path)
        if source_path is not None:
            context_kwargs["storage_state"] = self.manager.auth_state.prepare_for_context(source_path).storage_state

        memory = None
        if memory_profile and self.manager.memory is not None:
            # Read (and authorized) before any browser is opened: another
            # operator's profile text would go into this session's agent prompt.
            memory = await self.manager.memory.get(memory_profile)

        context: BrowserContext | None = None
        session: BrowserSession | None = None
        browser: Browser | None = None
        runtime: IsolatedBrowserRuntime | None = None
        gate = None
        gate_context_id: str | None = None
        created_hook_started = False
        # No await since check_limit, so nothing slipped in between.
        self._creating += 1
        reserved = True
        try:
            from ...browser_manager import BrowserSession

            browser, runtime = await self.manager._acquire_session_browser(session_id)
            # Before the first context: no session runs on a browser without its pre-request gate.
            gate = await self.manager.request_gates.require(browser)
            context = await browser.new_context(**context_kwargs)
            if self.manager.settings.enable_tracing:
                await context.tracing.start(screenshots=True, snapshots=True, sources=False)

            # On the context, before the first page exists: set on the first page
            # alone, tabs opened later and popups got neither the stealth patches
            # nor this timeout (they waited Playwright's 30 s default).
            context.set_default_timeout(self.manager.settings.action_timeout_ms)
            if self.manager.settings.stealth_enabled:
                await apply_stealth(context)
            page = await context.new_page()
            if gate is not None:
                # Owned from the first page, before anything navigates: its popups and tabs inherit it.
                gate_context_id = await gate.register_context(context, page)
                await self.manager.request_gates.bypass_service_workers(page)
            session = BrowserSession(
                id=session_id,
                name=name or f"session-{session_id}",
                created_at=datetime.now(UTC),
                context=context,
                page=page,
                artifact_dir=artifact_dir,
                auth_dir=auth_dir,
                upload_dir=upload_dir,
                takeover_url=runtime.takeover_url if runtime is not None else self.manager.settings.takeover_url,
                trace_path=artifact_dir / "trace.zip",
                trace_recording=self.manager.settings.enable_tracing,
                browser_node_name=runtime.browser_node_name if runtime is not None else "browser-node",
                isolation_mode=self.manager.settings.session_isolation_mode,
                browser=browser,
                owns_browser=runtime is not None,
                runtime=runtime,
                shared_takeover_surface=runtime is None,
                shared_browser_process=runtime is None,
                max_live_sessions_per_browser_node=1,
                proxy_persona=proxy_persona,
                last_auth_state_path=source_path if storage_state_path else None,
                auth_profile_name=self.manager.auth_profiles.normalize_name(auth_profile) if auth_profile else None,
                mouse_position=(
                    self.manager.settings.default_viewport_width / 2,
                    self.manager.settings.default_viewport_height / 2,
                ),
                protection_mode=resolved_protection_mode,
                totp_secret=totp_secret,
                totp_hosts=resolved_totp_hosts,
                witness_remote_state=self.manager._initial_witness_remote_state(resolved_protection_mode),
                owner=verified_operator(),
                gate_context_id=gate_context_id,
            )
            if source_path is not None:
                session.last_auth_state_path = source_path
            self.manager._attach_page_listeners(page, session)
            if hasattr(context, "on"):
                self.manager.diagnostics.watch_context(context, session)

            if self.manager.settings.network_inspector_enabled:
                inspector = NetworkInspector(
                    session_id=session_id,
                    max_entries=self.manager.settings.network_inspector_max_entries,
                    capture_bodies=self.manager.settings.network_inspector_capture_bodies,
                    body_max_bytes=self.manager.settings.network_inspector_body_max_bytes,
                    scrubber=self.manager.pii_scrubber if self.manager.settings.pii_scrub_enabled else None,
                    url_refused=self.manager._url_off_list,
                    page_refused=self.manager._page_refused,
                    session_refused=lambda: self.manager._session_has_refused_page(session),
                )
                # The context, not the first page: attached to the page, the
                # log missed every popup and tab opened later, and recorded
                # nothing at all once the first tab was closed.
                inspector.attach(context if hasattr(context, "on") else page)
                session.network_inspector = inspector

            self.manager.sessions[session_id] = session
            # Counted by manager.sessions from here on.
            self._creating -= 1
            reserved = False
            if self.manager._session_created_hook is not None:
                # Set before the call: a hook that fails halfway has still registered things.
                created_hook_started = True
                try:
                    await self.manager._session_created_hook(session_id, page)
                except Exception as exc:
                    logger.warning("session created hook failed for %s: %s", session_id, exc)

            if start_url:
                # The lock keeps the page guard (diagnostics) from moving the page to
                # about:blank between the load and the check: that would turn a
                # refused redirect into a session parked on a blank page.
                async with session.lock:
                    await self._goto_or_blocked(browser, page, start_url, action="create_session")
                    await self.manager._settle(page)
                    # The check navigate() makes after the page loads: an allowlisted
                    # start URL can redirect somewhere that is not. Raising here rolls
                    # the session back below, so no live session is left on that host.
                    self.manager._assert_runtime_url_allowed(page.url)

            await self.manager._maybe_provision_session_tunnel(session)
            if gate is not None and not gate.live:
                # Lost while this session started: the containment may have missed it (decision 2).
                raise RequestGateUnavailable(
                    "The pre-request gate detached from the browser while the session was starting, "
                    "so the session was closed. Create it again."
                )
            if memory is not None:
                session.metadata["memory_context"] = memory.to_system_prompt()
                session.metadata["memory_profile"] = memory_profile
                logger.info("memory profile loaded: %s", memory_profile)
            await self.manager._persist_session(session, status="active")
            await self.manager.witness_bridge.record_session_receipt(
                session,
                action="create_session",
                status="ok",
                metadata={
                    "start_url": start_url,
                    "storage_state_path": storage_state_path,
                    "auth_profile": auth_profile,
                    "memory_profile": memory_profile,
                    "proxy_persona": proxy_persona,
                    "totp_enabled": bool(totp_secret),
                },
            )
            await self.manager._persist_session(session, status="active")
            summary = await self.manager._session_summary(session)
            await self.manager.audit.append(
                event_type="session_created",
                status="ok",
                action="create_session",
                session_id=session.id,
                details={
                    "start_url": start_url,
                    "storage_state_path": storage_state_path,
                    "auth_profile": auth_profile,
                    "memory_profile": memory_profile,
                    "proxy_persona": proxy_persona,
                    "isolation_mode": session.isolation_mode,
                    "browser_node": session.browser_node_name,
                    "totp_enabled": bool(totp_secret),
                    "totp_hosts": list(resolved_totp_hosts),
                },
            )
            return summary
        except BaseException:
            # BaseException so a cancelled create (the caller gave up, shutdown)
            # also closes the context and releases the isolated runtime.
            await self.cleanup_failed(
                session_id,
                session=session,
                context=context,
                browser=browser,
                runtime=runtime,
                created_hook_started=created_hook_started,
            )
            raise
        finally:
            if reserved:
                self._creating -= 1

    def check_limit(self) -> None:
        if len(self.manager.sessions) + self._creating >= self.manager.settings.max_sessions:
            # Another operator's session answers like one that does not exist, so it is
            # counted here, not named.
            usable = sorted(sid for sid, session in self.manager.sessions.items() if may_use(session.owner))
            others = len(self.manager.sessions) - len(usable)
            message = (
                f"Session limit reached: max_sessions={self.manager.settings.max_sessions}. "
                f"Active live session(s): {', '.join(usable) or 'none'}."
            )
            if others:
                message += f" {others} more belong to other operators."
            if self._creating:
                message += f" {self._creating} more still starting."
            if self.manager.settings.session_isolation_mode == "shared_browser_node":
                message += (
                    " This scaffold uses one visible desktop and one shared browser node by default, "
                    "so only one live workflow is allowed unless you switch to docker_ephemeral isolation."
                )
            raise RuntimeError(message)

    def prepare_dirs(self, session_id: str) -> tuple[Path, Path, Path]:
        artifact_dir = self.manager.artifacts.prepare_session_dir(session_id)
        auth_dir = self.auth_root(session_id)
        upload_dir = self.upload_root(session_id)
        auth_dir.mkdir(parents=True, exist_ok=True)
        upload_dir.mkdir(parents=True, exist_ok=True)
        return artifact_dir, auth_dir, upload_dir

    def build_context_kwargs(
        self,
        user_agent: str | None,
        proxy_server: str | None,
        proxy_username: str | None,
        proxy_password: str | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "viewport": {
                "width": self.manager.settings.default_viewport_width,
                "height": self.manager.settings.default_viewport_height,
            },
            "accept_downloads": True,
        }
        effective_ua = user_agent or (
            self.manager.settings.random_user_agent if self.manager.settings.stealth_enabled else None
        )
        if effective_ua:
            kwargs["user_agent"] = effective_ua
        if self.manager.settings.stealth_enabled:
            kwargs.setdefault("timezone_id", "America/New_York")
            kwargs.setdefault("locale", "en-US")
            kwargs.setdefault("extra_http_headers", {"Accept-Language": "en-US,en;q=0.9"})
        if proxy_server:
            proxy_cfg: dict[str, Any] = {"server": proxy_server}
            if proxy_username:
                proxy_cfg["username"] = proxy_username
            if proxy_password:
                proxy_cfg["password"] = proxy_password
            kwargs["proxy"] = proxy_cfg
        return kwargs

    async def cleanup_failed(
        self,
        session_id: str,
        *,
        session: "BrowserSession | None",
        context: "BrowserContext | None",
        browser: "Browser | None",
        runtime: "IsolatedBrowserRuntime | None",
        created_hook_started: bool = False,
    ) -> None:
        self.manager.sessions.pop(session_id, None)
        if created_hook_started and self.manager._session_create_failed_hook is not None:
            # The created hook registered per-session state (network inspector,
            # CDP session) that only the closed path removed; a failed create
            # never reaches that path, so the dead session's entries stayed.
            try:
                await self.manager._session_create_failed_hook(session_id)
            except Exception as exc:
                logger.warning("session create-failed hook failed for %s: %s", session_id, exc)
        if session is not None and session.tunnel is not None:
            try:
                await self.manager.tunnel_broker.release(session.tunnel)
            except Exception as exc:
                logger.warning("failed to release session tunnel during create_session rollback: %s", exc)
        if context is not None:
            try:
                await context.close()
            except Exception as exc:
                logger.warning("failed to close browser context during create_session rollback: %s", exc)
        if browser is not None and runtime is not None:
            await self._close_owned_browser(browser, session_id)
        if runtime is not None:
            try:
                await self.manager.runtime_provisioner.release(runtime)
            except Exception as exc:
                logger.warning("failed to release isolated runtime during create_session rollback: %s", exc)

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
        """Close a session; ``reason`` is the controller's own (SessionRecord.close_reason), the next call names it."""
        session = await self.manager.get_session(session_id)
        locked = await self._lock_for_close(session)
        try:
            if self.manager.sessions.get(session_id) is not session:
                # Closed by a concurrent call while this one waited for the
                # lock. Carrying on released the tunnel and runtime twice and
                # wrote a second close to the audit log and witness chain.
                raise SessionNotFoundError(session_id, status="closed")
            try:
                if session.tunnel is not None:
                    await self._teardown_step(
                        session, "release session tunnel", lambda: self.manager.tunnel_broker.release(session.tunnel)
                    )
                try:
                    summary = await self.manager._session_summary(session, status="closed", live=False)
                finally:
                    # Released even when the summary fails: the context must not outlive the session.
                    await self._release_resources(session)
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
            await self.manager.audit.append(
                event_type="session_closed",
                status="ok",
                action="close_session",
                session_id=session.id,
                details={
                    "trace_path": str(session.trace_path),
                    "isolation_mode": session.isolation_mode,
                    "browser_node": session.browser_node_name,
                },
            )
            await self.manager._record_witness_receipt(
                session,
                event_type="session",
                status="ok",
                action="close_session",
                action_class="control",
                metadata={
                    "trace_path": str(session.trace_path),
                    "isolation_mode": session.isolation_mode,
                    "browser_node": session.browser_node_name,
                },
            )
            summary["witness_remote"] = session.witness_remote_state.model_dump()
            summary["close_reason"] = reason
            await self.manager.session_store.upsert(SessionRecord.model_validate(summary))
            return {"closed": True, "trace_path": str(session.trace_path), "session": summary}
        finally:
            if locked:
                session.lock.release()

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
        await self._teardown_step(session, "close a stuck browser context", session.context.close)
        try:
            await asyncio.wait_for(session.lock.acquire(), timeout=CLOSE_LOCK_WAIT_SECONDS)
            return True
        except TimeoutError:
            logger.warning("session %s is still busy after its context closed; closing it anyway", session.id)
            return False

    async def _release_resources(self, session: "BrowserSession") -> None:
        """Tear down everything a live session holds, each step independently.

        A failing step is logged and the rest still run, so one broken resource
        does not strand the others (the runtime container, the tunnel process).
        """
        await self._teardown_step(
            session, "stop trace recording", lambda: self.manager.observation.stop_trace_recording(session)
        )
        if session.network_inspector is not None:
            session.network_inspector.detach()
            session.network_inspector = None
        await self._teardown_step(session, "close browser context", session.context.close)
        if session.browser is not None and session.owns_browser:
            await self._close_owned_browser(session.browser, session.id)
        if session.runtime is not None:
            await self._teardown_step(
                session, "release isolated runtime", lambda: self.manager.runtime_provisioner.release(session.runtime)
            )

    async def _close_owned_browser(self, browser: Any, session_id: str) -> None:
        """Close a browser the session owns, then release its gate. Until the close has succeeded the gate stays
        attached, still gating whatever runs there, and the close taking the gate's session down is no loss."""
        gates = self.manager.request_gates
        gates.ending(browser)
        try:
            await asyncio.wait_for(browser.close(), timeout=TEARDOWN_STEP_TIMEOUT_SECONDS)
        except Exception as exc:
            gates.ending(browser, False)
            logger.error(
                "the isolated browser of session %s did not close (%s): its pre-request gate stays attached",
                session_id,
                f"{type(exc).__name__}: {exc}"[:200],
            )
            return
        await gates.release(browser)

    @staticmethod
    async def _teardown_step(session: "BrowserSession", label: str, step: Any) -> bool:
        try:
            await asyncio.wait_for(step(), timeout=TEARDOWN_STEP_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning("gave up waiting to %s for session %s", label, session.id)
        except Exception as exc:
            logger.warning("failed to %s for session %s: %s", label, session.id, exc)
        else:
            return True
        return False

    async def fork(
        self,
        session_id: str,
        *,
        name: str | None = None,
        start_url: str | None = None,
    ) -> dict[str, Any]:
        """Fork a session: clone cookies + localStorage state into a new session."""
        session = await self.manager.get_session(session_id)
        async with session.lock:
            # As shadow browse: without a start_url the fork opens the page's URL with the session's
            # cookies, and a refused page chose that URL, so a refused page is not forked onto it and the
            # URL is recorded as shown. A start_url the caller names is judged by create_session.
            if start_url is None:
                self.manager._assert_page_allowed(session)
            current_url = self.manager._shown_url(session.page)
            # Export through AuthStateManager so the state file is encrypted
            # at rest whenever an encryption key is configured.
            fork_auth_path = session.auth_dir / f"fork_{uuid4().hex[:8]}.json"
            self.manager.auth_profiles.mark_session_auth_dir(session.auth_dir, session.owner)
            auth_info = await self.manager.auth_state.write_storage_state(session.context, fork_auth_path)

        # Create the new session using the forked state
        try:
            forked = await self.manager.create_session(
                name=name or f"fork-of-{session.name}",
                start_url=start_url or current_url,
                storage_state_path=auth_info["path"],
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
            details={"new_session_id": forked["id"], "start_url": start_url or current_url},
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
        """
        manager = self.manager
        if not manager.settings.shadow_browse_enabled:
            raise RuntimeError("Shadow browsing is disabled (SHADOW_BROWSE_ENABLED=false)")
        if manager.playwright is None:
            raise RuntimeError("Playwright not started")

        session = await manager.get_session(session_id)
        async with session.lock:
            # The shadow opens the page's URL in a second browser: a refused page is not read
            # (its URL is the refused page's choice), and only an allowed http(s) URL is opened.
            manager._assert_page_allowed(session)
            current_url = manager._shown_url(session.page)
            if current_url != "about:blank":
                manager._assert_url_allowed(current_url)
            # In-memory export: shadow state never touches disk.
            storage_state = await session.context.storage_state()

        from ...browser_manager import BrowserSession

        shadow_session_id = new_session_id()
        artifact_dir, auth_dir, upload_dir = self.prepare_dirs(shadow_session_id)
        context_kwargs: dict[str, Any] = {
            "viewport": {
                "width": manager.settings.default_viewport_width,
                "height": manager.settings.default_viewport_height,
            },
            "accept_downloads": True,
            "storage_state": storage_state,
        }

        # Launch a local headed browser process
        headed_browser = await manager.playwright.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-setuid-sandbox", STORAGE_PARTITIONING_ON],
        )
        context = None
        try:
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
            manager.sessions[shadow_session_id] = shadow_session

            if current_url != "about:blank":
                await self._goto_or_blocked(headed_browser, page, current_url, action="enable_shadow_browse")
                await manager._settle(page)
            if gate is not None and not gate.live:
                # Lost before the session was in manager.sessions, where containment looks: refused, as in create.
                raise RequestGateUnavailable(
                    "The pre-request gate detached from the shadow browser while it was starting, so it was closed. "
                    "Enable shadow browsing again."
                )
            await manager._persist_session(shadow_session, status="active")
        except Exception:
            manager.sessions.pop(shadow_session_id, None)
            # As create's rollback: the context first, then the browser, and its gate only once that has closed.
            if context is not None:
                try:
                    await context.close()
                except Exception as exc:
                    logger.warning("failed to close the shadow context during rollback: %s", exc)
            await self._close_owned_browser(headed_browser, shadow_session_id)
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

    async def _goto_or_blocked(self, browser: Any, page: Any, url: str, *, action: str) -> None:
        """The controller's own navigation outside the action pipeline, a stop by the gate mapped to the block it
        is (R6's rule: the gate's own failure, and a block on this page since the navigation started)."""
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
                raise
            stopped = await gates.blocks_on_page(browser, page, since=since)
            raise navigation_blocked_error(stopped[-1] if stopped else None, action=action) from exc

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
        return page.url, title, True

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
        summary = await self.manager._session_summary(
            session,
            status=status,
            live=status == "active",
        )
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
