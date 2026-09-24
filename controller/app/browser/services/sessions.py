from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from playwright.async_api import Error as PlaywrightError

from ...browser_scripts import apply_persistent_stealth, apply_stealth
from ...models import SessionRecord, SessionStatus
from ...network_inspector import NetworkInspector
from ...utils import UTC

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext

    from ...browser_manager import BrowserSession
    from ...session_isolation import IsolatedBrowserRuntime

logger = logging.getLogger(__name__)


class BrowserSessionService:
    """Encapsulates live session lifecycle and durable session summaries."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager

    async def list(self) -> list[dict[str, Any]]:
        session_map = {record.id: record.model_dump() for record in await self.manager.session_store.list()}
        for session in self.manager.sessions.values():
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
        unattended: bool = False,
    ) -> dict[str, Any]:
        """Open a session.

        `unattended` marks a call where nobody is watching to notice or retry
        a failed auto-load -- currently only the cron scheduler (see
        CronService._run_job_now). It relaxes the staleness check on an
        explicitly named `auth_profile` to the same much-larger limit the
        default "remember me" profile always uses below, since a named
        profile driven by an unattended schedule has exactly the same silent-
        failure risk as the default one.
        """
        if storage_state_path and auth_profile:
            raise ValueError("Provide auth_profile or storage_state_path, not both")
        if proxy_persona and any((request_proxy_server, request_proxy_username, request_proxy_password)):
            raise ValueError("Provide proxy_persona or explicit proxy_server credentials, not both")
        if start_url:
            self.manager._assert_url_allowed(start_url)
        resolved_protection_mode = protection_mode or self.manager.settings.witness_protection_mode_default
        self.manager._check_session_limit()

        session_id = uuid4().hex[:12]
        artifact_dir, auth_dir, upload_dir = self.prepare_dirs(session_id)
        prepared_auth_state = None
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

        # Whether the remembered login actually loaded, and why not when it
        # didn't -- surfaced on the session summary (see `summary()` below) so
        # the calling app can tell the owner "your saved login did not load"
        # instead of silently showing a logged-out browser. Only meaningful
        # for the "remember me" fallback path below; an explicitly named
        # auth_profile is a deliberate operator choice, not a "remembered
        # login", so it leaves these at their defaults.
        remembered_login_loaded = False
        remembered_login_error: str | None = None
        # A saved profile existed but failed to load into this session's
        # context -- belt and braces with the auto-persist downgrade guard in
        # BrowserAuthProfileService.save_auto_persist: this session must never
        # be allowed to auto-persist over that profile, since whatever it
        # holds now did not come from the remembered login.
        remembered_load_failed_over_existing_profile = False

        unattended_max_age = self.manager.settings.auth_state_unattended_max_age_hours

        if auth_profile:
            # The takeover the report described: opening a session against
            # someone else's stored profile drives a browser already logged in
            # as them. Authorize before the state is ever loaded.
            self.manager.auth_profiles.require_access(auth_profile, action="opening a session from an auth profile")
            source_path = self.manager.auth_profiles.resolve_state_path(auth_profile, must_exist=True)
        elif storage_state_path:
            source_path = self.manager.auth_profiles.safe_auth_path(storage_state_path, must_exist=True)

        # "Remember me": nobody named a profile, so fall back to whatever is
        # already sitting in the default auto-persist profile (an earlier
        # session's automatic save). Best-effort only — no profile yet, a
        # stale/corrupt one, or a permission quirk must open a fresh browser
        # instead of failing Open outright. This auto-load happens with
        # nobody watching by definition, so it always uses the much larger
        # unattended staleness limit -- sites expire their own cookies; going
        # 72h between browser opens must not be what silently erases a login
        # (the incident this whole fix responds to).
        auto_persist_name = self.manager.settings.auto_persist_profile_name
        if source_path is None and self.manager.settings.auto_persist_login_enabled:
            candidate_path: Path | None = None
            try:
                self.manager.auth_profiles.require_access(
                    auto_persist_name, action="auto-loading the remembered login"
                )
                candidate_path = self.manager.auth_profiles.resolve_state_path(auto_persist_name, must_exist=True)
            except FileNotFoundError:
                pass  # nothing saved yet -- not an error, nothing lost
            except Exception as exc:
                logger.warning(
                    "auto-persist: could not access remembered login profile '%s': %s", auto_persist_name, exc
                )

            if candidate_path is not None:
                try:
                    prepared_candidate = self.manager.auth_state.prepare_for_context(
                        candidate_path, max_age_hours=unattended_max_age
                    )
                    context_kwargs["storage_state"] = str(prepared_candidate.path)
                    prepared_auth_state = prepared_candidate
                    source_path = candidate_path
                    remembered_login_loaded = True
                except Exception as exc:
                    remembered_login_error = str(exc)
                    remembered_load_failed_over_existing_profile = True
                    logger.warning(
                        "auto-persist: remembered login ('%s') did not load: %s", auto_persist_name, exc
                    )
        elif source_path is not None:
            prepared_auth_state = self.manager.auth_state.prepare_for_context(
                source_path, max_age_hours=unattended_max_age if unattended else None
            )
            context_kwargs["storage_state"] = str(prepared_auth_state.path)

        # Persistent Chromium profiles (see persistent_profiles.py): a named
        # identity/profile -- an explicit auth_profile, or the auto-persist
        # default ("owner-default") when the caller names none -- gets its
        # own on-disk user-data-dir in browser-node instead of a brand-new
        # context every Open. Only wired for shared_browser_node; the
        # docker_ephemeral runtime keeps its existing per-session container
        # lifecycle unchanged. Any storage_state resolved above is popped out
        # of context_kwargs here: it is only ever consulted by browser-node,
        # and only to seed a profile whose user-data-dir is still empty (see
        # server.mjs) -- never to overwrite a profile that already exists.
        # An explicit storage_state_path (fork()'s "clone this session" path,
        # or a direct API caller) is left out of persistent-profile mode on
        # purpose: a fork exists to run an independent, parallel clone, and
        # collapsing it onto the same named profile's already-running
        # persistent context would hand it the SAME tab instead of one of
        # its own.
        persistent_profile_name: str | None = None
        persistent_storage_state: dict[str, Any] | None = None
        if (
            self.manager.settings.persistent_profiles_enabled
            and self.manager.settings.session_isolation_mode == "shared_browser_node"
            and storage_state_path is None
        ):
            persistent_profile_name = (
                self.manager.auth_profiles.normalize_name(auth_profile) if auth_profile else auto_persist_name
            )
            resolved_storage_state_path = context_kwargs.pop("storage_state", None)
            if resolved_storage_state_path:
                persistent_storage_state = json.loads(Path(resolved_storage_state_path).read_text(encoding="utf-8"))
            # A real device's locale/timezone/UA do not change between logins.
            # Override whatever build_context_kwargs picked for a one-off
            # ephemeral context (it defaults to en-US/America-New_York when
            # stealth is on, and a fresh random UA every time) with this
            # profile's pinned, env-configurable identity instead.
            context_kwargs["locale"] = self.manager.settings.persistent_profile_locale
            context_kwargs["timezone_id"] = self.manager.settings.persistent_profile_timezone
            # Let Playwright derive Accept-Language from locale rather than
            # keeping a hardcoded "en-US,en;q=0.9" that would contradict it.
            context_kwargs.pop("extra_http_headers", None)
            if self.manager.settings.persistent_profile_user_agent:
                context_kwargs["user_agent"] = self.manager.settings.persistent_profile_user_agent
            else:
                # A real, headed Chromium's own UA is more convincing than a
                # spoofed one -- see PERSISTENT_STEALTH_INIT_SCRIPT.
                context_kwargs.pop("user_agent", None)

        context: BrowserContext | None = None
        session: BrowserSession | None = None
        browser: Browser | None = None
        runtime: IsolatedBrowserRuntime | None = None
        persistent_handle = None
        try:
            from ...browser_manager import BrowserSession

            if persistent_profile_name is not None:
                attachment = await self.manager.runtime.acquire_persistent_context(
                    profile_name=persistent_profile_name,
                    context_kwargs=context_kwargs,
                    storage_state=persistent_storage_state,
                )
                browser = attachment.browser
                context = attachment.context
                persistent_handle = attachment.handle
                # The on-disk profile already had content (most opens, after
                # the first) or this call just seeded it from the saved
                # storage_state -- either way, this session did not start
                # logged out. A truly fresh, never-seeded profile leaves
                # these at their defaults (a real "first login" case).
                if persistent_handle.seeded or not persistent_handle.was_empty:
                    remembered_login_loaded = True
            else:
                browser, runtime = await self.manager._acquire_session_browser(session_id)
                context = await browser.new_context(**context_kwargs)

            # A reused, already-running profile may already be tracing from
            # its current open; starting it again would raise. A reused
            # profile also normally still has its tab(s) open -- adopt the
            # most recently opened one instead of piling on a redundant tab.
            if persistent_handle is None or not persistent_handle.already_open:
                if self.manager.settings.enable_tracing:
                    await context.tracing.start(screenshots=True, snapshots=True, sources=False)
                page = await context.new_page()
            elif context.pages:
                page = context.pages[-1]
            else:
                page = await context.new_page()

            page.set_default_timeout(self.manager.settings.action_timeout_ms)
            if self.manager.settings.stealth_enabled:
                if persistent_handle is not None:
                    await apply_persistent_stealth(page)
                else:
                    await apply_stealth(page)
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
                runtime=runtime,
                shared_takeover_surface=runtime is None,
                shared_browser_process=runtime is None,
                max_live_sessions_per_browser_node=1,
                proxy_persona=proxy_persona,
                last_auth_state_path=source_path if storage_state_path else None,
                auth_profile_name=self.manager.auth_profiles.normalize_name(auth_profile) if auth_profile else None,
                persistent_profile_name=persistent_profile_name,
                mouse_position=(
                    self.manager.settings.default_viewport_width / 2,
                    self.manager.settings.default_viewport_height / 2,
                ),
                protection_mode=resolved_protection_mode,
                totp_secret=totp_secret,
                witness_remote_state=self.manager._initial_witness_remote_state(resolved_protection_mode),
                remembered_login_loaded=remembered_login_loaded,
                remembered_login_error=remembered_login_error,
            )
            if source_path is not None:
                session.last_auth_state_path = source_path
            self.manager._attach_page_listeners(page, session)
            if hasattr(context, "on"):
                context.on("page", lambda popup: self.manager._attach_page_listeners(popup, session))

            if self.manager.settings.network_inspector_enabled:
                inspector = NetworkInspector(
                    session_id=session_id,
                    max_entries=self.manager.settings.network_inspector_max_entries,
                    capture_bodies=self.manager.settings.network_inspector_capture_bodies,
                    body_max_bytes=self.manager.settings.network_inspector_body_max_bytes,
                    scrubber=self.manager.pii_scrubber if self.manager.settings.pii_scrub_enabled else None,
                )
                inspector.attach(page)
                session.network_inspector = inspector

            self.manager.sessions[session_id] = session
            if self.manager._session_created_hook is not None:
                try:
                    await self.manager._session_created_hook(session_id, page)
                except Exception as exc:
                    logger.warning("session created hook failed for %s: %s", session_id, exc)

            if start_url:
                await page.goto(start_url, wait_until="domcontentloaded")
                await self.manager._settle(page)

            await self.manager._maybe_provision_session_tunnel(session)
            if (
                self.manager.settings.auto_persist_login_enabled
                and self.manager.settings.auto_persist_interval_seconds > 0
                and not remembered_load_failed_over_existing_profile
            ):
                # A session opened from an explicitly named auth_profile must
                # keep THAT profile's cookies fresh, not the "remember me"
                # default -- otherwise a named profile that is loaded but
                # never re-saved goes dead the moment the site rotates its
                # session cookies (the incident this responds to: opening
                # 'nihad-google' still only ever refreshed 'owner-default').
                # Access to the named profile was already checked above
                # (require_access at session-open); save_for_session/
                # save_auto_persist re-check it on every write regardless.
                # A session with no named profile keeps today's behaviour.
                persist_profile_name = session.auth_profile_name or auto_persist_name
                session.auto_persist_profile_name = persist_profile_name
                session.auto_persist_task = asyncio.create_task(
                    self._auto_persist_loop(session, persist_profile_name)
                )
            if memory_profile and self.manager.memory is not None:
                memory = await self.manager.memory.get(memory_profile)
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
                },
            )
            return summary
        except Exception:
            await self.cleanup_failed(
                session_id,
                session=session,
                context=context,
                browser=browser,
                runtime=runtime,
                persistent_profile_name=persistent_profile_name,
            )
            raise
        finally:
            if prepared_auth_state is not None:
                prepared_auth_state.cleanup()

    def check_limit(self) -> None:
        if len(self.manager.sessions) >= self.manager.settings.max_sessions:
            active_ids = ", ".join(sorted(self.manager.sessions.keys()))
            message = (
                f"Session limit reached: max_sessions={self.manager.settings.max_sessions}. "
                f"Active live session(s): {active_ids}."
            )
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
        persistent_profile_name: str | None = None,
    ) -> None:
        self.manager.sessions.pop(session_id, None)
        if persistent_profile_name is not None:
            await self.manager.persistent_profiles.close(persistent_profile_name)
        if session is not None and session.auto_persist_task is not None:
            session.auto_persist_task.cancel()
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
        if browser is not None and browser is not self.manager.browser:
            try:
                await browser.close()
            except Exception as exc:
                logger.warning("failed to close isolated browser during create_session rollback: %s", exc)
        if runtime is not None:
            try:
                await self.manager.runtime_provisioner.release(runtime)
            except Exception as exc:
                logger.warning("failed to release isolated runtime during create_session rollback: %s", exc)

    async def get(self, session_id: str) -> "BrowserSession":
        session = self.manager.sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session

    async def get_record(self, session_id: str) -> dict[str, Any]:
        session = self.manager.sessions.get(session_id)
        if session is not None:
            return await self.manager._session_summary(session)
        record = await self.manager.session_store.get(session_id)
        return record.model_dump()

    async def close(self, session_id: str) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        async with session.lock:
            if session.tunnel is not None:
                await self.manager.tunnel_broker.release(session.tunnel)
            summary = await self.manager._session_summary(session, status="closed", live=False)
            await self.manager.observation.stop_trace_recording(session)
            if session.network_inspector is not None:
                session.network_inspector.detach()
                session.network_inspector = None
            if session.auto_persist_task is not None:
                session.auto_persist_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await session.auto_persist_task
                session.auto_persist_task = None
            if session.auto_persist_profile_name:
                # Last chance to remember this login before the context (and
                # its cookies/localStorage) disappears. Best-effort: a page
                # left on an unreadable state (e.g. mid-navigation) must not
                # stop the browser from closing.
                try:
                    await self.manager.auth_profiles.save_auto_persist(
                        session, session.auto_persist_profile_name
                    )
                except Exception as exc:
                    logger.warning(
                        "auto-persist: failed to save remembered login on close for session %s: %s",
                        session_id,
                        exc,
                    )
            if session.persistent_profile_name:
                # browser-node owns the actual Chromium process behind a
                # persistent profile and tracks it by refcount (a second live
                # session on the same profile must not tear it down under the
                # first) -- release our hold there before touching the local
                # CDP client. Best-effort; never blocks the rest of close().
                await self.manager.persistent_profiles.close(session.persistent_profile_name)
            try:
                await session.context.close()
            except Exception as exc:
                if session.persistent_profile_name is None:
                    raise
                # Expected once the profile above already tore down the
                # underlying Chromium process (refcount reached zero).
                logger.debug(
                    "context.close() after releasing persistent profile '%s' for session %s: %s",
                    session.persistent_profile_name,
                    session_id,
                    exc,
                )
            finally:
                if session.browser is not None and session.browser is not self.manager.browser:
                    try:
                        await session.browser.close()
                    except Exception as exc:  # pragma: no cover - best effort isolated cleanup
                        logger.warning("failed to close isolated browser for session %s: %s", session_id, exc)
                if session.runtime is not None:
                    await self.manager.runtime_provisioner.release(session.runtime)
            self.manager.sessions.pop(session_id, None)
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
            await self.manager.session_store.upsert(SessionRecord.model_validate(summary))
            return {"closed": True, "trace_path": str(session.trace_path), "session": summary}

    async def _auto_persist_loop(self, session: "BrowserSession", profile_name: str) -> None:
        """Keep the "remember me" profile warm while a session is live.

        Runs for the lifetime of the session, saving into `profile_name`
        every `auto_persist_interval_seconds`. Cancelled from `close()`,
        which also does one final save — this loop only covers the case of
        a session that stays open a long time, or is lost without a clean
        close (server restart, crash), so at most one interval's worth of
        login activity is ever at risk instead of the whole session.
        """
        interval = self.manager.settings.auto_persist_interval_seconds
        while True:
            await asyncio.sleep(interval)
            async with session.lock:
                try:
                    await self.manager.auth_profiles.save_auto_persist(session, profile_name)
                except Exception as exc:
                    logger.warning(
                        "auto-persist: periodic save failed for session %s: %s", session.id, exc
                    )
                    # The tracked page can close out from under us — the site closed its
                    # own tab/window, the page crashed, or the owner's tab died — while
                    # the browser context (and the rest of the shared browser process)
                    # stays perfectly alive. Before this check, that left `session`
                    # sitting in `self.manager.sessions` forever: nothing ever removed
                    # it, so it kept occupying this tenant's one-session slot
                    # (max_sessions=1 by default) and every subsequent Open was refused
                    # ("Close the current session first", or the session limit) with no
                    # way out except restarting the controller. Detected here, since
                    # this loop already probes the page every interval, retire the
                    # session the same way an explicit close does.
                    #
                    # But the tracked page closing is NOT the same as the session dying:
                    # the owner (or the site itself, e.g. a re-auth flow that opens a
                    # fresh tab and closes the old one) may still have other tabs open in
                    # this same context. Retiring unconditionally used to tear down the
                    # WHOLE session -- every other open tab included -- the instant only
                    # the one tab we happened to be tracking closed. Adopt the most
                    # recently opened surviving tab instead, and only retire the session
                    # when none remain.
                    if session.page.is_closed():
                        try:
                            candidates = self.manager.tabs.pages(session)
                        except Exception:
                            candidates = []
                        remaining = [
                            p for p in candidates if p is not session.page and not p.is_closed()
                        ]
                        if remaining:
                            adopted = remaining[-1]
                            session.page = adopted
                            self.manager._attach_page_listeners(adopted, session)
                            logger.warning(
                                "session %s: its tracked page closed but %d other tab(s) "
                                "are still open -- adopted the most recent one instead of "
                                "retiring the whole session",
                                session.id, len(remaining),
                            )
                        else:
                            await self._retire_dead_session(
                                session,
                                reason="its tracked browser page has closed and no other tabs remain",
                            )
                            return

    async def _retire_dead_session(self, session: "BrowserSession", *, reason: str) -> None:
        """End a session whose underlying page died without an explicit close.

        Mirrors `close()`'s teardown, but every step is best-effort: the
        browser, context, or runtime behind a dead session may already be
        half gone, and a raised exception here must not leave the session
        wedged in `self.manager.sessions` — that is exactly the stuck state
        this method exists to clear. Must be called with `session.lock` held.
        """
        if self.manager.sessions.get(session.id) is not session:
            return  # already retired (e.g. an explicit close raced us) -- nothing to do
        logger.warning("session %s: %s -- retiring the stale session record", session.id, reason)
        self.manager.sessions.pop(session.id, None)
        if session.auto_persist_task is not None:
            session.auto_persist_task = None
        if session.tunnel is not None:
            try:
                await self.manager.tunnel_broker.release(session.tunnel)
            except Exception as exc:
                logger.warning(
                    "failed to release session tunnel while retiring session %s: %s", session.id, exc
                )
        if session.network_inspector is not None:
            session.network_inspector.detach()
            session.network_inspector = None
        if session.persistent_profile_name:
            await self.manager.persistent_profiles.close(session.persistent_profile_name)
        try:
            await session.context.close()
        except Exception as exc:
            logger.debug("context close failed while retiring dead session %s: %s", session.id, exc)
        if session.browser is not None and session.browser is not self.manager.browser:
            try:
                await session.browser.close()
            except Exception as exc:
                logger.debug("browser close failed while retiring dead session %s: %s", session.id, exc)
        if session.runtime is not None:
            try:
                await self.manager.runtime_provisioner.release(session.runtime)
            except Exception as exc:
                logger.warning(
                    "failed to release isolated runtime while retiring session %s: %s", session.id, exc
                )
        if self.manager._session_closed_hook is not None:
            try:
                await self.manager._session_closed_hook(session.id)
            except Exception as exc:
                logger.warning("session closed hook failed for %s: %s", session.id, exc)
        try:
            summary = await self.summary(session, status="interrupted", live=False)
            await self.manager.session_store.upsert(SessionRecord.model_validate(summary))
        except Exception as exc:
            logger.warning("failed to persist retirement summary for session %s: %s", session.id, exc)
        try:
            await self.manager.audit.append(
                event_type="session_closed",
                status="ok",
                action="auto_retire_dead_session",
                session_id=session.id,
                details={"reason": reason},
            )
        except Exception as exc:
            logger.warning("audit append failed while retiring session %s: %s", session.id, exc)

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
            # Export through AuthStateManager so the state file is encrypted
            # at rest whenever an encryption key is configured.
            fork_auth_path = session.auth_dir / f"fork_{uuid4().hex[:8]}.json"
            auth_info = await self.manager.auth_state.write_storage_state(session.context, fork_auth_path)
            current_url = session.page.url

        # Create the new session using the forked state
        forked = await self.manager.create_session(
            name=name or f"fork-of-{session.name}",
            start_url=start_url or current_url,
            storage_state_path=auth_info["path"],
        )
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
            current_url = session.page.url
            # In-memory export: shadow state never touches disk.
            storage_state = await session.context.storage_state()

        from ...browser_manager import BrowserSession

        shadow_session_id = uuid4().hex[:12]
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
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        try:
            context = await headed_browser.new_context(**context_kwargs)
            page = await context.new_page()
            page.set_default_timeout(manager.settings.action_timeout_ms)
            if manager.settings.stealth_enabled:
                await apply_stealth(page)

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
                headless=False,
            )
            manager._attach_page_listeners(page, shadow_session)
            manager.sessions[shadow_session_id] = shadow_session

            await page.goto(current_url, wait_until="domcontentloaded")
            await manager._settle(page)
            await manager._persist_session(shadow_session, status="active")
        except Exception:
            manager.sessions.pop(shadow_session_id, None)
            try:
                await headed_browser.close()
            except Exception as exc:  # pragma: no cover - best effort rollback
                logger.warning("failed to close shadow browser during rollback: %s", exc)
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

    @staticmethod
    async def _page_snapshot(session: "BrowserSession") -> tuple[str, str, bool]:
        page = session.page
        is_closed = getattr(page, "is_closed", None)
        if callable(is_closed) and is_closed():
            return "", "", False
        try:
            return page.url, await page.title(), True
        except PlaywrightError as exc:
            logger.debug("page snapshot failed for session %s: %s", session.id, exc)
            return "", "", False

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
            "remembered_login_loaded": session.remembered_login_loaded,
            "remembered_login_error": session.remembered_login_error,
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
                "vnc_port": session.runtime.vnc_port,
            }
        if session.persistent_profile_name:
            payload["persistent_profile"] = {
                "name": session.persistent_profile_name,
                # Manual cleanup only -- see PersistentProfileClient and the
                # deploy notes; nothing here ever deletes a profile directory.
                "disk_usage_bytes": self.manager.persistent_profiles.profile_disk_usage_bytes(
                    session.persistent_profile_name
                ),
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
