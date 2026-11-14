"""Creating a session (BrowserSessionService.create) and what only creation uses: the session-limit check, the
context options, the rollback of a create that failed, and the session id it mints.

A mixin of BrowserSessionService (sessions.py), which holds what creation shares with the rest of a session's life
(its directories, the controller's own navigation, closing an owned browser).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ...action_errors import SessionNotFoundError
from ...browser_scripts import apply_stealth
from ...models import resolve_totp_hosts
from ...network_inspector import NetworkInspector
from ...session_ownership import may_use, verified_operator
from ...url_safety import refuse_another_navigation_host
from ...utils import UTC, error_text_for_log, quoted_for_log, run_to_completion
from ..request_gate import RequestGateUnavailable

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext

    from ...browser_manager import BrowserSession
    from ...session_isolation import IsolatedBrowserRuntime
    from ...session_tunnel import IsolatedSessionTunnel

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


class SessionLimitError(RuntimeError):
    """create_session refused because MAX_SESSIONS live sessions already exist (a fork counts too)."""


class _ClosedWhileStarting(Exception):
    """create()'s own: another call closed its session while it was starting. ``tunnel`` is the session's tunnel when
    it came up after that close had looked for one, so that nothing else releases it."""

    def __init__(self, tunnel: "IsolatedSessionTunnel | None" = None) -> None:
        super().__init__()
        self.tunnel = tunnel


class SessionCreation:
    """BrowserSessionService's creation half (a mixin: ``self`` is the service)."""

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
            source_path = self.manager.auth_profiles.state_path_to_load(auth_profile)
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
        # Checked again: the reads above await, and another create may have reserved a slot meanwhile. Nothing awaits
        # between this check and the reservation.
        self.manager._check_session_limit()
        self._creating += 1
        reserved = True
        try:
            from ...browser_manager import BrowserSession

            # After every check that can refuse the request, and inside the try: whatever fails from here on
            # removes them again (cleanup_failed).
            artifact_dir, auth_dir, upload_dir = self.prepare_dirs(session_id)
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
                user_agent=context_kwargs.get("user_agent"),  # the effective one: a stealth draw included
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
                # Judge this session's windows by their CDP target birth (runs with the gate off too).
                await self.manager.window_births.watch(session, context, page, context_id=gate_context_id)

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
            # Counted by manager.sessions from here on. Another call finds the session there, and can close it before
            # this one returns (a caller that listed it, the gate-loss containment, a shutdown): looked for after
            # each await below.
            self._creating -= 1
            reserved = False
            if self.manager._session_created_hook is not None:
                # Set before the call: a hook that fails halfway has still registered things.
                created_hook_started = True
                try:
                    await self.manager._session_created_hook(session_id, page)
                except Exception as exc:
                    logger.warning("session created hook failed for %s: %s", session_id, error_text_for_log(exc))
            self._stop_if_closed(session)

            if start_url:
                # The lock keeps the page guard (diagnostics) from moving the page to
                # about:blank between the load and the check: that would turn a
                # refused redirect into a session parked on a blank page.
                async with session.lock:
                    await self._goto_or_blocked(browser, page, start_url, action="create_session")
                    # The check navigate() makes after the page loads: an allowlisted
                    # start URL can redirect somewhere that is not. Raising here rolls
                    # the session back below, so no live session is left on that host.
                    self.manager._assert_runtime_url_allowed(page.url)
                self._stop_if_closed(session)

            await self.manager._maybe_provision_session_tunnel(session)
            # Nothing awaits between the tunnel reaching the session and this look: a close that has started looked for
            # the tunnel before it was there, and released none.
            self._stop_if_closed(session, unreleased=session.tunnel)
            if gate is not None and not gate.live:
                # Lost while this session started: the containment may have missed it (decision 2).
                raise RequestGateUnavailable(
                    "The pre-request gate detached from the browser while the session was starting, "
                    "so the session was closed. Create it again."
                )
            if memory is not None:
                session.metadata["memory_context"] = memory.to_system_prompt()
                session.metadata["memory_profile"] = memory_profile
                logger.info("memory profile loaded: %s", quoted_for_log(memory_profile))
            await self.manager._persist_session(session, status="active")
            self._stop_if_closed(session)
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
            self._stop_if_closed(session)
            await self.manager._persist_session(session, status="active")
            self._stop_if_closed(session)
            summary = await self.manager._session_summary(session)
            self._stop_if_closed(session)
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
            self._stop_if_closed(session)
            return summary
        except BaseException as exc:
            if session is not None and session.closing:
                # Closed by another call meanwhile, which tears the session down and records it: nothing is rolled
                # back. The rollback closed its context a second time, removed the closed session's directories and
                # wrote create_failed over its close reason.
                await run_to_completion(self._release_what_the_close_missed(session, exc, created_hook_started))
                if isinstance(exc, Exception):
                    # Answered as any call on a closed session is, with the close's reason where its record has it.
                    _, reason = await self._recorded_end(session_id)
                    raise SessionNotFoundError(session_id, status="closed", reason=reason) from None
                raise  # a cancel is raised as itself
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

    @staticmethod
    def _stop_if_closed(session: "BrowserSession", *, unreleased: "IsolatedSessionTunnel | None" = None) -> None:
        """End a create whose session another call has started closing (A2-25). The create went on with the closed
        session: it answered active, wrote session_created after session_closed, and left running a tunnel that came
        up after the close. ``unreleased`` is that tunnel, at the one look that can tell."""
        if session.closing:
            if unreleased is not None:
                # Off the session in the turn it reached it, so that nothing else finds it there while the create
                # releases it. The close's summary, still being built, took it for up (the closed record named its
                # public URL, on a port the release gives the next tunnel), and a second close released it again.
                session.tunnel = None
            raise _ClosedWhileStarting(unreleased)

    async def _release_what_the_close_missed(
        self, session: "BrowserSession", error: BaseException, created_hook_started: bool
    ) -> None:
        """What a create made on its session after another call closed it, which that close could not release. Run
        to completion by its caller, each step bounded as the rollback's are."""
        closed = isinstance(error, _ClosedWhileStarting)
        if isinstance(error, Exception) and not closed:
            logger.warning(
                "session %s was closed while it was starting, and its create then failed: %s: %s",
                session.id,
                type(error).__name__,
                error_text_for_log(error),
            )
        hook = self.manager._session_create_failed_hook
        if created_hook_started and hook is not None:
            # The closed hook may have run before the created hook had registered its state.
            await self._teardown_step(
                session.id, "run the create-failed hook of a session closed while it started", lambda: hook(session.id)
            )
        tunnel = error.tunnel if closed else None
        if tunnel is not None:
            await self._teardown_step(
                session.id,
                "release the tunnel that came up after the session closed",
                lambda: self.manager.tunnel_broker.release(tunnel),
            )

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
            raise SessionLimitError(message)

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
        """Roll back a create that failed: every step runs to its end even when the task awaiting it is cancelled
        during one (W7-2), and the cancel is raised once the rollback is done. A cancel at any step left the isolated
        runtime running, the directories in place and the record reading active."""
        # Unregistered here, before the first await: the steps start a turn of the loop later, and a close that was
        # waiting for this session (the gate-loss containment's, behind the start URL's load) began its own teardown
        # in that turn, beside this one. It finds the session gone now, and answers that.
        self.manager.sessions.pop(session_id, None)
        await run_to_completion(
            self._cleanup_failed_steps(
                session_id,
                session=session,
                context=context,
                browser=browser,
                runtime=runtime,
                created_hook_started=created_hook_started,
            )
        )

    async def _cleanup_failed_steps(
        self,
        session_id: str,
        *,
        session: "BrowserSession | None",
        context: "BrowserContext | None",
        browser: "Browser | None",
        runtime: "IsolatedBrowserRuntime | None",
        created_hook_started: bool,
    ) -> None:
        if session is not None:
            # Registered with the window-birth watcher before the failure (watch runs early): a dead session
            # left there was held, and re-registered with every replacement watcher.
            self.manager.window_births.forget_session(session)
        # The hook, the tunnel release, the context close, the owned browser's close and the runtime release below are
        # each bounded as close_session's teardown steps are (W9-3): this unit runs to completion under a cancel, so a
        # context.close() or runtime release that stalled held its caller for ever. The directory removal and the
        # record close in _forget_failed_session_state are not bounded. The labels name the rollback (W10-17), so its
        # log lines are not read as a close_session's.
        hook = self.manager._session_create_failed_hook
        if created_hook_started and hook is not None:
            # The created hook registered per-session state (network inspector,
            # CDP session) that only the closed path removed; a failed create
            # never reaches that path, so the dead session's entries stayed.
            await self._teardown_step(
                session_id, "run the create-failed hook of a failed create", lambda: hook(session_id)
            )
        if session is not None and session.tunnel is not None:
            tunnel = session.tunnel
            await self._teardown_step(
                session_id,
                "release the session tunnel of a failed create",
                lambda: self.manager.tunnel_broker.release(tunnel),
            )
        if context is not None:
            await self._teardown_step(session_id, "close the browser context of a failed create", context.close)
        if browser is not None and runtime is not None:
            await self._close_owned_browser(browser, session_id)
        if runtime is not None:
            await self._teardown_step(
                session_id,
                "release the isolated runtime of a failed create",
                lambda: self.manager.runtime_provisioner.release(runtime),
            )
        await self._forget_failed_session_state(session, session_id)

    def remove_session_dirs(self, session_id: str) -> None:
        """Remove what prepare_dirs made for a session that never came to be.

        Each directory on its own (W10-6): an OSError from its removal or its check (Path.exists raises for EACCES or
        EIO on Python 3.11) is logged and the loop goes on, so the other directories and the record close still run.
        A removal error inside rmtree is logged with the directory and the error, and rmtree goes on through the rest
        of that tree (W11-5); a path already gone is not an error. One warning per directory, naming its first error,
        the root cause (W12-4): the parent's "Directory not empty" that follows a file's unlink error is not logged.
        A link left at a directory's place is named too (R5C-22): POSIX rmtree reports a dangling symlink as
        FileNotFoundError and leaves it, and the check after it, which follows links, found nothing.
        """
        for directory in (
            self.manager.artifacts.artifact_root / session_id,
            self.auth_root(session_id),
            self.upload_root(session_id),
        ):
            reported: list[BaseException] = []

            def logged(
                _function: Any, _path: Any, error: Any, directory: Path = directory, reported: list = reported
            ) -> None:
                if isinstance(error, tuple):  # onerror's exc_info
                    error = error[1]
                if isinstance(error, FileNotFoundError) or reported:
                    return
                reported.append(error)
                logger.warning("rollback could not remove %s: %s", directory, error_text_for_log(error))

            try:
                # onexc from Python 3.12, where onerror is deprecated; onerror on 3.11.
                if sys.version_info >= (3, 12):
                    shutil.rmtree(directory, onexc=logged)
                else:
                    shutil.rmtree(directory, onerror=logged)
                # exists() raises what it cannot answer (W10-6); lexists() then sees a dangling link it took for gone.
                if not reported and (directory.exists() or os.path.lexists(directory)):
                    logger.warning("rollback could not remove %s", directory)
            except OSError as exc:
                if not reported:
                    logger.warning("rollback could not remove %s: %s", directory, error_text_for_log(exc))

    async def _forget_failed_session_state(self, session: "BrowserSession | None", session_id: str) -> None:
        """Remove the directories a failed create made, and close the record it persisted, if it got that far.

        Part of a protected unit in both its callers, cleanup_failed's (W5-6, W7-2) and enable_shadow_browse's
        rollback (W9-11): each await below was a point where a cancel stopped the rollback before the record read
        closed.
        """
        # In a worker thread: rmtree of a large profile or artifact tree would stall every request on the loop.
        await asyncio.to_thread(self.remove_session_dirs, session_id)
        if session is None:
            return  # no record is written before the session object exists
        try:
            await self.manager.session_store.get(session_id)
        except KeyError:
            return
        except ValueError:
            pass  # a record that cannot be parsed is closed over (W8-2)
        except Exception as exc:
            logger.warning("could not read the record of failed session %s: %s", session_id, error_text_for_log(exc))
            return
        await self._persist_closed_without_summary(session, "create_failed")
