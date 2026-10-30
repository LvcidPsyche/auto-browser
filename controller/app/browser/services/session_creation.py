"""Creating a session (BrowserSessionService.create) and what only creation uses: the session-limit check, the
context options, the rollback of a create that failed, and the session id it mints.

A mixin of BrowserSessionService (sessions.py), which holds what creation shares with the rest of a session's life
(its directories, the controller's own navigation, closing an owned browser).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ...browser_scripts import apply_stealth
from ...models import resolve_totp_hosts
from ...network_inspector import NetworkInspector
from ...session_ownership import may_use, verified_operator
from ...url_safety import refuse_another_navigation_host
from ...utils import UTC
from ..request_gate import RequestGateUnavailable

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
