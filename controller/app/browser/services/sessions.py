from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from playwright.async_api import Error as PlaywrightError

from ...browser_scripts import apply_stealth
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
        session_map = {
            record.id: record.model_dump()
            for record in await self.manager.session_store.list()
        }
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
    ) -> dict[str, Any]:
        if storage_state_path and auth_profile:
            raise ValueError("Provide auth_profile or storage_state_path, not both")
        if proxy_persona and any((request_proxy_server, request_proxy_username, request_proxy_password)):
            raise ValueError("Provide proxy_persona or explicit proxy_server credentials, not both")
        if start_url:
            self.manager._assert_url_allowed(start_url)
        resolved_protection_mode = protection_mode or self.manager.settings.witness_protection_mode_default
        self.manager._check_session_limit()

        session_id = uuid4().hex[:12]
        artifact_dir, auth_dir, upload_dir = self.manager._prepare_session_dirs(session_id)
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

        if auth_profile:
            source_path = self.manager._resolve_auth_profile_state_path(auth_profile, must_exist=True)
        elif storage_state_path:
            source_path = self.manager._safe_auth_path(storage_state_path, must_exist=True)
        if source_path is not None:
            prepared_auth_state = self.manager.auth_state.prepare_for_context(source_path)
            context_kwargs["storage_state"] = str(prepared_auth_state.path)

        context: BrowserContext | None = None
        session: BrowserSession | None = None
        browser: Browser | None = None
        runtime: IsolatedBrowserRuntime | None = None
        try:
            from ...browser_manager import BrowserSession

            browser, runtime = await self.manager._acquire_session_browser(session_id)
            context = await browser.new_context(**context_kwargs)
            if self.manager.settings.enable_tracing:
                await context.tracing.start(screenshots=True, snapshots=True, sources=False)

            page = await context.new_page()
            page.set_default_timeout(self.manager.settings.action_timeout_ms)
            if self.manager.settings.stealth_enabled:
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
                auth_profile_name=self.manager._normalize_auth_profile_name(auth_profile) if auth_profile else None,
                mouse_position=(
                    self.manager.settings.default_viewport_width / 2,
                    self.manager.settings.default_viewport_height / 2,
                ),
                protection_mode=resolved_protection_mode,
                totp_secret=totp_secret,
                witness_remote_state=self.manager._initial_witness_remote_state(resolved_protection_mode),
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
            if memory_profile and self.manager.memory is not None:
                memory = await self.manager.memory.get(memory_profile)
                if memory is not None:
                    session.metadata["memory_context"] = memory.to_system_prompt()
                    session.metadata["memory_profile"] = memory_profile
                    logger.info("memory profile loaded: %s", memory_profile)
            await self.manager._persist_session(session, status="active")
            await self.manager._record_session_witness_receipt(
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
            await self.manager._cleanup_failed_session(
                session_id,
                session=session,
                context=context,
                browser=browser,
                runtime=runtime,
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
        auth_dir = self.manager._session_auth_root(session_id)
        upload_dir = self.manager._session_upload_root(session_id)
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
        effective_ua = (
            user_agent
            or (self.manager.settings.random_user_agent if self.manager.settings.stealth_enabled else None)
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
    ) -> None:
        self.manager.sessions.pop(session_id, None)
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
            await self.manager._stop_trace_recording(session)
            if session.network_inspector is not None:
                session.network_inspector.detach()
                session.network_inspector = None
            try:
                await session.context.close()
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
            "remote_access": self.manager._session_remote_access_info(session),
            "isolation": self.manager._session_isolation(session),
            "auth_state": self.manager._session_auth_state_info(session),
            "downloads": session.downloads[-20:],
            "last_action": session.last_action,
            "trace_path": str(session.trace_path),
            "proxy_persona": session.proxy_persona,
            "protection_mode": session.protection_mode,
            "witness_remote": session.witness_remote_state.model_dump(),
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
