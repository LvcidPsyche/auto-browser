from __future__ import annotations

import asyncio
import fnmatch
import inspect
import json
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from playwright.async_api import Browser, BrowserContext, Error as PlaywrightError, Page, Playwright, async_playwright

try:  # pragma: no cover - optional until dependency is installed in runtime image
    import pyotp
except Exception:  # pragma: no cover - graceful fallback for non-login test runs
    pyotp = None  # type: ignore[assignment]

from .action_errors import BrowserActionError
from .audit import AuditStore
from .approvals import ApprovalRequiredError, ApprovalStore
from .auth_state import AuthStateManager
from .config import Settings
from .models import ApprovalKind, BrowserActionDecision, SessionRecord, SessionStatus
from .ocr import OCRExtractor
from .session_store import DurableSessionStore
from .session_isolation import DockerBrowserNodeProvisioner, IsolatedBrowserRuntime
from .social_errors import SocialActionError
from .session_tunnel import IsolatedSessionTunnel, IsolatedSessionTunnelBroker
from .browser_scripts import (
    INTERACTABLES_SCRIPT,
    ACTIVE_ELEMENT_SCRIPT,
    PAGE_SUMMARY_SCRIPT,
    EXTRACT_POSTS_SCRIPT,
    EXTRACT_PROFILE_SCRIPT,
    EXTRACT_COMMENTS_SCRIPT,
    SMOOTH_SCROLL_SCRIPT,
    FIND_LIKE_BUTTON_SCRIPT,
    FIND_FOLLOW_BUTTON_SCRIPT,
    FIND_UNFOLLOW_BUTTON_SCRIPT,
    FIND_REPLY_BUTTON_SCRIPT,
    FIND_REPOST_BUTTON_SCRIPT,
    FIND_SEARCH_INPUT_SCRIPT,
    apply_stealth,
)

logger = logging.getLogger(__name__)

ACCESSIBILITY_NODE_LIMIT = 30


@dataclass
class BrowserSession:
    id: str
    name: str
    created_at: datetime
    context: BrowserContext
    page: Page
    artifact_dir: Path
    auth_dir: Path
    upload_dir: Path
    takeover_url: str
    trace_path: Path
    browser_node_name: str = "browser-node"
    isolation_mode: str = "shared_browser_node"
    browser: Browser | None = None
    runtime: IsolatedBrowserRuntime | None = None
    tunnel: IsolatedSessionTunnel | None = None
    shared_takeover_surface: bool = True
    shared_browser_process: bool = True
    max_live_sessions_per_browser_node: int = 1
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    console_messages: list[dict[str, Any]] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    request_failures: list[dict[str, Any]] = field(default_factory=list)
    downloads: list[dict[str, Any]] = field(default_factory=list)
    attached_pages: set[int] = field(default_factory=set)
    last_action: str | None = None
    last_auth_state_path: Path | None = None
    auth_profile_name: str | None = None
    tunnel_error: str | None = None
    mouse_position: tuple[float, float] | None = None
    totp_secret: str | None = None


class BrowserManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.sessions: dict[str, BrowserSession] = {}
        self._browser_lock = asyncio.Lock()

        Path(self.settings.artifact_root).mkdir(parents=True, exist_ok=True)
        Path(self.settings.upload_root).mkdir(parents=True, exist_ok=True)
        Path(self.settings.auth_root).mkdir(parents=True, exist_ok=True)
        Path(self.settings.approval_root).mkdir(parents=True, exist_ok=True)
        Path(self.settings.audit_root).mkdir(parents=True, exist_ok=True)
        if self.settings.state_db_path:
            Path(self.settings.state_db_path).resolve().parent.mkdir(parents=True, exist_ok=True)
        Path(self.settings.session_store_root).mkdir(parents=True, exist_ok=True)
        approval_kwargs: dict[str, Any] = {"db_path": self.settings.state_db_path}
        if "approval_ttl_minutes" in inspect.signature(ApprovalStore).parameters:
            approval_kwargs["approval_ttl_minutes"] = self.settings.approval_ttl_minutes
        self.approvals = ApprovalStore(self.settings.approval_root, **approval_kwargs)
        self.audit = AuditStore(
            self.settings.audit_root,
            db_path=self.settings.state_db_path,
            max_events=self.settings.audit_max_events,
        )
        self.session_store = DurableSessionStore(
            file_root=self.settings.session_store_root,
            redis_url=self.settings.redis_url,
            redis_prefix=self.settings.session_store_redis_prefix,
        )
        self.auth_state = AuthStateManager(
            encryption_key=self.settings.auth_state_encryption_key,
            require_encryption=self.settings.require_auth_state_encryption,
            max_age_hours=self.settings.auth_state_max_age_hours,
        )
        self.ocr = OCRExtractor(
            enabled=self.settings.ocr_enabled,
            language=self.settings.ocr_language,
            max_blocks=self.settings.ocr_max_blocks,
            text_limit=self.settings.ocr_text_limit,
        )
        self.runtime_provisioner = DockerBrowserNodeProvisioner(self.settings)
        self.tunnel_broker = IsolatedSessionTunnelBroker(self.settings)

    def get_remote_access_info(self, session_id: str | None = None) -> dict[str, Any]:
        if session_id:
            session = self.sessions.get(session_id)
            if session is not None:
                return self._session_remote_access_info(session)
        return self._global_remote_access_info()

    def _global_remote_access_info(self) -> dict[str, Any]:
        info_path = Path(self.settings.remote_access_info_path)
        payload: dict[str, Any] = {
            "active": False,
            "status": "inactive",
            "stale": False,
            "source": "static",
            "configured_takeover_url": self.settings.takeover_url,
            "takeover_url": self.settings.takeover_url,
            "api_url": None,
            "api_auth_enabled": bool(self.settings.api_bearer_token),
            "info_path": str(info_path),
            "exists": info_path.exists(),
            "last_updated": None,
            "age_seconds": None,
            "stale_after_seconds": float(self.settings.remote_access_stale_after_seconds),
            "tunnel": None,
            "error": None,
        }
        if not info_path.exists():
            return payload
        try:
            tunnel = json.loads(info_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("failed to read remote access info %s: %s", info_path, exc)
            payload["status"] = "error"
            payload["source"] = "metadata_file"
            payload["error"] = str(exc)
            return payload

        last_updated = self._parse_remote_access_timestamp(tunnel.get("updated_at"))
        if last_updated is None:
            try:
                last_updated = datetime.fromtimestamp(info_path.stat().st_mtime, tz=UTC)
            except OSError:
                last_updated = None
        age_seconds = None
        if last_updated is not None:
            age_seconds = max(0.0, (datetime.now(UTC) - last_updated).total_seconds())
        stale_after_seconds = float(
            tunnel.get("stale_after_seconds") or self.settings.remote_access_stale_after_seconds
        )
        raw_status = str(tunnel.get("status") or "active")
        stale = bool(age_seconds is not None and age_seconds > stale_after_seconds)
        active = raw_status == "active" and not stale
        takeover_url = tunnel.get("public_takeover_url") if active else self.settings.takeover_url
        api_url = tunnel.get("public_api_url") if active else None
        payload.update(
            {
                "active": active,
                "status": "stale" if stale else raw_status,
                "stale": stale,
                "source": "metadata_file",
                "takeover_url": takeover_url,
                "api_url": api_url,
                "last_updated": (
                    last_updated.isoformat().replace("+00:00", "Z")
                    if last_updated is not None
                    else None
                ),
                "age_seconds": age_seconds,
                "stale_after_seconds": stale_after_seconds,
                "tunnel": tunnel,
            }
        )
        return payload

    def _session_remote_access_info(self, session: BrowserSession) -> dict[str, Any]:
        if session.isolation_mode != "docker_ephemeral":
            return self._global_remote_access_info()

        shared_remote_access = self._global_remote_access_info()
        takeover_url = session.takeover_url
        takeover_local_only = self._takeover_url_is_local_only(takeover_url)
        api_url = shared_remote_access.get("api_url")
        session_tunnel = self.tunnel_broker.describe(session.tunnel)
        warning = None
        status = "active"
        active = True
        effective_takeover_url = takeover_url
        requires_direct_host_access = takeover_local_only
        local_only = takeover_local_only

        if session_tunnel and session_tunnel.get("active"):
            effective_takeover_url = str(session_tunnel["public_takeover_url"])
            requires_direct_host_access = False
            local_only = False
            status = "active"
            active = True
        elif not takeover_local_only:
            status = "active"
            active = True
            requires_direct_host_access = False
            local_only = False
        else:
            active = False
            status = "api_only" if api_url else "local_only"
            warning = (
                "This isolated takeover URL is still bound to a local host/port. "
                "Enable ISOLATED_TUNNEL_* settings or set ISOLATED_TAKEOVER_HOST to a remotely reachable hostname "
                "or IP if humans need remote takeover."
            )
            if session_tunnel and session_tunnel.get("status") in {"error", "degraded"}:
                status = "degraded"
                warning = (
                    "The isolated session tunnel is unavailable, so takeover fell back to the local-only URL. "
                    f"{session_tunnel.get('error') or ''}"
                ).strip()
            elif session.tunnel_error:
                status = "degraded"
                warning = (
                    "The isolated session tunnel could not be created, so takeover fell back to the local-only URL. "
                    f"{session.tunnel_error}"
                ).strip()

        payload = dict(shared_remote_access)
        payload.update(
            {
                "session_id": session.id,
                "source": (
                    "isolated_session_tunnel"
                    if session_tunnel and session_tunnel.get("active")
                    else "isolated_runtime"
                ),
                "configured_takeover_url": takeover_url,
                "takeover_url": effective_takeover_url,
                "local_only": local_only,
                "requires_direct_host_access": requires_direct_host_access,
                "shared_api_url": api_url,
                "shared_tunnel_active": bool(shared_remote_access.get("active")),
                "shared_tunnel": shared_remote_access.get("tunnel"),
                "session_tunnel": session_tunnel,
                "session_tunnel_error": session.tunnel_error,
                "active": active,
                "status": status,
                "warning": warning,
            }
        )
        if session.runtime is not None:
            payload["runtime"] = {
                "container_name": session.runtime.container_name,
                "browser_node": session.runtime.browser_node_name,
                "novnc_port": session.runtime.novnc_port,
                "vnc_port": session.runtime.vnc_port,
            }
        return payload

    def _current_takeover_url(self, session: BrowserSession | None = None) -> str:
        if session is not None and session.isolation_mode == "docker_ephemeral":
            tunnel = self.tunnel_broker.describe(session.tunnel)
            if tunnel and tunnel.get("active") and tunnel.get("public_takeover_url"):
                return str(tunnel["public_takeover_url"])
            return session.takeover_url
        remote_access = self._global_remote_access_info()
        if remote_access.get("active") and remote_access.get("takeover_url"):
            return str(remote_access["takeover_url"])
        if session is not None:
            return session.takeover_url
        return self.settings.takeover_url

    @staticmethod
    def _parse_remote_access_timestamp(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @staticmethod
    def _takeover_url_is_local_only(value: str) -> bool:
        host = (urlparse(value).hostname or "").strip().lower()
        return host in {"", "127.0.0.1", "localhost", "::1", "0.0.0.0"}

    async def startup(self) -> None:
        logger.info("starting browser manager")
        await self.approvals.startup()
        await self.audit.startup()
        await self.session_store.startup()
        await self.session_store.mark_all_active_interrupted()
        self.playwright = await async_playwright().start()
        await self.tunnel_broker.startup()
        await self.runtime_provisioner.startup()
        if self.settings.session_isolation_mode == "shared_browser_node":
            await self.ensure_browser()

    async def shutdown(self) -> None:
        logger.info("shutting down browser manager")
        session_ids = list(self.sessions.keys())
        for session_id in session_ids:
            try:
                await self.close_session(session_id)
            except Exception as exc:  # pragma: no cover - best effort cleanup
                logger.warning("failed to close session %s during shutdown: %s", session_id, exc)

        self.browser = None
        if self.playwright is not None:
            await self.playwright.stop()
        await self.tunnel_broker.shutdown()
        await self.session_store.shutdown()

    async def ensure_browser(self) -> Browser:
        async with self._browser_lock:
            if self.browser is not None and self.browser.is_connected():
                return self.browser
            if self.playwright is None:
                raise RuntimeError("Playwright not started")

            self.browser = await self._connect_browser(
                self._resolve_browser_ws_endpoint,
                failure_context=(
                    "Unable to connect to browser node via Playwright server. "
                    f"Checked ws endpoint file {self.settings.browser_ws_endpoint_file} "
                    f"and direct endpoint {self.settings.browser_ws_endpoint or '<not configured>'}."
                ),
            )
            return self.browser

    async def _connect_browser(self, ws_target_factory, *, failure_context: str) -> Browser:
        if self.playwright is None:
            raise RuntimeError("Playwright not started")

        last_error: Exception | None = None
        for attempt in range(1, self.settings.connect_retries + 1):
            try:
                ws_target = await ws_target_factory()
                browser = await self.playwright.chromium.connect(ws_target)
                logger.info(
                    "connected to browser node on attempt %s via playwright endpoint %s",
                    attempt,
                    ws_target,
                )
                return browser
            except Exception as exc:  # pragma: no cover - depends on external service
                last_error = exc
                await asyncio.sleep(self.settings.connect_retry_delay_seconds)
        raise RuntimeError(failure_context) from last_error

    async def _resolve_browser_ws_endpoint(self) -> str:
        ws_endpoint_file = Path(self.settings.browser_ws_endpoint_file)
        if ws_endpoint_file.exists():
            ws_endpoint = ws_endpoint_file.read_text(encoding="utf-8").strip()
            if ws_endpoint:
                return ws_endpoint
        if self.settings.browser_ws_endpoint:
            return self.settings.browser_ws_endpoint
        raise FileNotFoundError(f"missing playwright ws endpoint file: {ws_endpoint_file}")

    async def _acquire_session_browser(self, session_id: str) -> tuple[Browser, IsolatedBrowserRuntime | None]:
        if self.settings.session_isolation_mode != "docker_ephemeral":
            return await self.ensure_browser(), None

        runtime = await self.runtime_provisioner.provision(session_id)
        try:
            browser = await self._connect_browser(
                lambda: asyncio.sleep(0, result=runtime.ws_endpoint),
                failure_context=(
                    "Unable to connect to isolated browser node via Playwright server. "
                    f"Checked isolated endpoint file {runtime.ws_endpoint_file}."
                ),
            )
            return browser, runtime
        except Exception:
            await self.runtime_provisioner.release(runtime)
            raise

    async def list_sessions(self) -> list[dict[str, Any]]:
        session_map = {
            record.id: record.model_dump()
            for record in await self.session_store.list()
        }
        for session in self.sessions.values():
            summary = await self._session_summary(session)
            session_map[summary["id"]] = summary
        return sorted(
            session_map.values(),
            key=lambda item: (item.get("created_at") or "", item.get("id") or ""),
            reverse=True,
        )

    async def create_session(
        self,
        *,
        name: str | None = None,
        start_url: str | None = None,
        storage_state_path: str | None = None,
        auth_profile: str | None = None,
        request_proxy_server: str | None = None,
        request_proxy_username: str | None = None,
        request_proxy_password: str | None = None,
        user_agent: str | None = None,
        totp_secret: str | None = None,
    ) -> dict[str, Any]:
        if storage_state_path and auth_profile:
            raise ValueError("Provide auth_profile or storage_state_path, not both")
        if start_url:
            self._assert_url_allowed(start_url)
        if len(self.sessions) >= self.settings.max_sessions:
            active_ids = ", ".join(sorted(self.sessions.keys()))
            message = (
                f"Session limit reached: max_sessions={self.settings.max_sessions}. "
                f"Active live session(s): {active_ids}."
            )
            if self.settings.session_isolation_mode == "shared_browser_node":
                message += (
                    " This scaffold uses one visible desktop and one shared browser node by default, "
                    "so only one live workflow is allowed unless you switch to docker_ephemeral isolation."
                )
            raise RuntimeError(message)

        session_id = uuid4().hex[:12]
        artifact_dir = Path(self.settings.artifact_root) / session_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "downloads").mkdir(parents=True, exist_ok=True)
        auth_dir = self._session_auth_root(session_id)
        upload_dir = self._session_upload_root(session_id)
        auth_dir.mkdir(parents=True, exist_ok=True)
        upload_dir.mkdir(parents=True, exist_ok=True)
        prepared_auth_state = None
        source_path: Path | None = None

        # Choose proxy: per-session override > default settings
        proxy_server = request_proxy_server or self.settings.default_proxy_server
        proxy_username = request_proxy_username or self.settings.default_proxy_username
        proxy_password = request_proxy_password or self.settings.default_proxy_password

        context_kwargs: dict[str, Any] = {
            "viewport": {
                "width": self.settings.default_viewport_width,
                "height": self.settings.default_viewport_height,
            },
            "accept_downloads": True,
        }
        # Use provided UA or pick one from the pool
        effective_ua = user_agent or (self.settings.random_user_agent if self.settings.stealth_enabled else None)
        if effective_ua:
            context_kwargs["user_agent"] = effective_ua
        if self.settings.stealth_enabled:
            context_kwargs.setdefault("timezone_id", "America/New_York")
            context_kwargs.setdefault("locale", "en-US")
            context_kwargs.setdefault(
                "extra_http_headers",
                {
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
        if proxy_server:
            proxy_cfg: dict[str, Any] = {"server": proxy_server}
            if proxy_username:
                proxy_cfg["username"] = proxy_username
            if proxy_password:
                proxy_cfg["password"] = proxy_password
            context_kwargs["proxy"] = proxy_cfg
        if auth_profile:
            source_path = self._resolve_auth_profile_state_path(auth_profile, must_exist=True)
        elif storage_state_path:
            source_path = self._safe_auth_path(storage_state_path, must_exist=True)
        if source_path is not None:
            prepared_auth_state = self.auth_state.prepare_for_context(source_path)
            context_kwargs["storage_state"] = str(prepared_auth_state.path)

        context: BrowserContext | None = None
        session: BrowserSession | None = None
        browser: Browser | None = None
        runtime: IsolatedBrowserRuntime | None = None
        try:
            browser, runtime = await self._acquire_session_browser(session_id)
            context = await browser.new_context(**context_kwargs)
            if self.settings.enable_tracing:
                await context.tracing.start(screenshots=True, snapshots=True, sources=False)

            page = await context.new_page()
            page.set_default_timeout(self.settings.action_timeout_ms)
            if self.settings.stealth_enabled:
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
                takeover_url=runtime.takeover_url if runtime is not None else self.settings.takeover_url,
                trace_path=artifact_dir / "trace.zip",
                browser_node_name=runtime.browser_node_name if runtime is not None else "browser-node",
                isolation_mode=self.settings.session_isolation_mode,
                browser=browser,
                runtime=runtime,
                shared_takeover_surface=runtime is None,
                shared_browser_process=runtime is None,
                max_live_sessions_per_browser_node=1,
                last_auth_state_path=source_path if storage_state_path else None,
                auth_profile_name=self._normalize_auth_profile_name(auth_profile) if auth_profile else None,
                mouse_position=(
                    self.settings.default_viewport_width / 2,
                    self.settings.default_viewport_height / 2,
                ),
                totp_secret=totp_secret,
            )
            if source_path is not None:
                session.last_auth_state_path = source_path
            self._attach_page_listeners(page, session)
            if hasattr(context, "on"):
                context.on("page", lambda popup: self._attach_page_listeners(popup, session))
            self.sessions[session_id] = session

            if start_url:
                await page.goto(start_url, wait_until="domcontentloaded")
                await self._settle(page)

            await self._maybe_provision_session_tunnel(session)
            await self._persist_session(session, status="active")
            summary = await self._session_summary(session)
            await self.audit.append(
                event_type="session_created",
                status="ok",
                action="create_session",
                session_id=session.id,
                details={
                    "start_url": start_url,
                    "storage_state_path": storage_state_path,
                    "auth_profile": auth_profile,
                    "isolation_mode": session.isolation_mode,
                    "browser_node": session.browser_node_name,
                    "totp_enabled": bool(totp_secret),
                },
            )
            return summary
        except Exception:
            self.sessions.pop(session_id, None)
            if session is not None and session.tunnel is not None:
                try:
                    await self.tunnel_broker.release(session.tunnel)
                except Exception as exc:
                    logger.warning("failed to release session tunnel during create_session rollback: %s", exc)
            if context is not None:
                try:
                    await context.close()
                except Exception as exc:
                    logger.warning("failed to close browser context during create_session rollback: %s", exc)
            if browser is not None and browser is not self.browser:
                try:
                    await browser.close()
                except Exception as exc:
                    logger.warning("failed to close isolated browser during create_session rollback: %s", exc)
            if runtime is not None:
                try:
                    await self.runtime_provisioner.release(runtime)
                except Exception as exc:
                    logger.warning("failed to release isolated runtime during create_session rollback: %s", exc)
            raise
        finally:
            if prepared_auth_state is not None:
                prepared_auth_state.cleanup()

    async def get_session(self, session_id: str) -> BrowserSession:
        session = self.sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session

    async def get_session_record(self, session_id: str) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if session is not None:
            return await self._session_summary(session)
        record = await self.session_store.get(session_id)
        return record.model_dump()

    async def list_approvals(
        self,
        *,
        status: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        approvals = await self.approvals.list(status=status, session_id=session_id)
        return [approval.model_dump() for approval in approvals]

    async def get_approval(self, approval_id: str) -> dict[str, Any]:
        approval = await self.approvals.get(approval_id)
        return approval.model_dump()

    async def approve(self, approval_id: str, comment: str | None = None) -> dict[str, Any]:
        approval = await self.approvals.approve(approval_id, comment=comment)
        await self.audit.append(
            event_type="approval_decision",
            status="approved",
            action="approve",
            session_id=approval.session_id,
            approval_id=approval.id,
            details={"kind": approval.kind, "comment": comment},
        )
        return approval.model_dump()

    async def reject(self, approval_id: str, comment: str | None = None) -> dict[str, Any]:
        approval = await self.approvals.reject(approval_id, comment=comment)
        await self.audit.append(
            event_type="approval_decision",
            status="rejected",
            action="reject",
            session_id=approval.session_id,
            approval_id=approval.id,
            details={"kind": approval.kind, "comment": comment},
        )
        return approval.model_dump()

    async def execute_approval(self, approval_id: str) -> dict[str, Any]:
        approval = await self.approvals.get(approval_id)
        if approval.status != "approved":
            raise PermissionError(f"approval {approval_id} is not approved")

        decision = approval.action
        if decision.action == "upload":
            execution = await self.upload(
                approval.session_id,
                selector=decision.selector,
                element_id=decision.element_id,
                file_path=decision.file_path or "",
                approved=False,
                approval_id=approval.id,
            )
            latest = await self.approvals.get(approval.id)
        elif decision.action == "social_login":
            raise PermissionError(
                "social_login approvals must be executed through the dedicated social login endpoint/tool with credentials"
            )
        else:
            execution = await self.execute_decision(
                approval.session_id,
                decision,
                approval_id=approval.id,
            )
            latest = await self.approvals.get(approval.id)
        await self.audit.append(
            event_type="approval_executed",
            status="ok",
            action="execute_approval",
            session_id=approval.session_id,
            approval_id=approval.id,
            details={"kind": approval.kind, "action": decision.action},
        )
        return {
            "approval": latest.model_dump(),
            "execution": execution,
        }

    async def observe(self, session_id: str, limit: int = 40) -> dict[str, Any]:
        session = await self.get_session(session_id)
        async with session.lock:
            return await self._observation_payload(session, limit=limit)

    async def capture_screenshot(self, session_id: str, *, label: str = "manual") -> dict[str, Any]:
        session = await self.get_session(session_id)
        async with session.lock:
            screenshot = await self._capture_screenshot(session, label)
            return {
                "session": await self._session_summary(session),
                "url": session.page.url,
                "screenshot_path": screenshot["path"],
                "screenshot_url": screenshot["url"],
                "takeover_url": self._current_takeover_url(session),
            }

    async def navigate(self, session_id: str, url: str) -> dict[str, Any]:
        self._assert_url_allowed(url)
        session = await self.get_session(session_id)

        async def operation() -> None:
            await session.page.goto(url, wait_until="domcontentloaded")
            await self._settle(session.page)
            # Check for bot challenge pages after navigation
            challenge = await self._check_bot_challenge(session)
            if challenge:
                logger.warning("bot challenge detected after navigation: %s", challenge)
                try:
                    await self.request_human_takeover(session.id, reason=f"Bot challenge detected: {challenge['signal']}")
                except Exception:
                    pass

        return await self._run_action(session, "navigate", {"url": url}, operation)

    async def click(
        self,
        session_id: str,
        *,
        selector: str | None = None,
        element_id: str | None = None,
        x: float | None = None,
        y: float | None = None,
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        target = self._resolve_target(selector=selector, element_id=element_id, x=x, y=y)

        async def operation() -> None:
            if target["mode"] == "coordinates":
                await self._click_human_like(session, float(x), float(y))
            else:
                locator = session.page.locator(target["selector"]).first
                await locator.scroll_into_view_if_needed()
                coords = await self._locator_center(locator)
                if coords is None:
                    await locator.click()
                else:
                    target["x"], target["y"] = coords
                    await self._click_human_like(session, coords[0], coords[1])
            await self._settle(session.page)

        return await self._run_action(session, "click", target, operation)

    async def hover(
        self,
        session_id: str,
        *,
        selector: str | None = None,
        element_id: str | None = None,
        x: float | None = None,
        y: float | None = None,
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        target = self._resolve_target(selector=selector, element_id=element_id, x=x, y=y)

        async def operation() -> None:
            if target["mode"] == "coordinates":
                await self._move_mouse_human_like(session, float(x), float(y))
            else:
                locator = session.page.locator(target["selector"]).first
                await locator.scroll_into_view_if_needed()
                coords = await self._locator_center(locator)
                if coords is None:
                    await locator.hover()
                else:
                    target["x"], target["y"] = coords
                    await self._move_mouse_human_like(session, coords[0], coords[1])
            await self._settle(session.page)

        return await self._run_action(session, "hover", target, operation)

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
        session = await self.get_session(session_id)
        target = self._resolve_target(selector=selector, element_id=element_id)

        async def operation() -> None:
            locator = session.page.locator(target["selector"]).first
            await locator.scroll_into_view_if_needed()
            if index is not None:
                await locator.select_option(index=index)
            elif value is not None:
                await locator.select_option(value=value)
            else:
                await locator.select_option(label=label)
            await self._settle(session.page)

        return await self._run_action(
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
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        target = self._resolve_target(selector=selector, element_id=element_id)
        payload = self._text_target_payload(target, text, clear_first=clear_first, sensitive=sensitive, preview_chars=80)

        async def operation() -> None:
            locator = session.page.locator(target["selector"]).first
            if await self._locator_is_sensitive_input(locator):
                payload.pop("text_preview", None)
                payload["text_redacted"] = True
            await locator.scroll_into_view_if_needed()
            await self._focus_locator(session, locator)
            if clear_first:
                await session.page.keyboard.press("Control+a")
                await asyncio.sleep(0.03)
                await session.page.keyboard.press("Delete")
                await asyncio.sleep(0.05)
            await self._type_text_human_like(session.page, text)
            await self._settle(session.page)

        return await self._run_action(
            session,
            "type",
            payload,
            operation,
        )

    @staticmethod
    def _text_target_payload(
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

    async def _locator_is_sensitive_input(self, locator: Any) -> bool:
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

        input_type = (attributes.get("type") or "").strip().lower()
        if input_type == "password":
            return True

        autocomplete = (attributes.get("autocomplete") or "").strip().lower()
        if autocomplete in {"current-password", "new-password", "one-time-code"}:
            return True

        haystack = " ".join(str(value or "") for value in attributes.values()).lower()
        return bool(re.search(r"password|passcode|otp|one[- ]time|verification|token|secret|2fa|mfa", haystack))

    async def press(self, session_id: str, key: str) -> dict[str, Any]:
        session = await self.get_session(session_id)

        async def operation() -> None:
            await session.page.keyboard.press(key)
            await self._settle(session.page)

        return await self._run_action(session, "press", {"key": key}, operation)

    async def scroll(self, session_id: str, delta_x: float, delta_y: float) -> dict[str, Any]:
        session = await self.get_session(session_id)

        async def operation() -> None:
            await session.page.mouse.wheel(delta_x, delta_y)
            await self._settle(session.page)

        return await self._run_action(
            session,
            "scroll",
            {"delta_x": delta_x, "delta_y": delta_y},
            operation,
        )

    async def wait(self, session_id: str, wait_ms: int) -> dict[str, Any]:
        session = await self.get_session(session_id)

        async def operation() -> None:
            await asyncio.sleep(max(0, wait_ms) / 1000)

        return await self._run_action(session, "wait", {"wait_ms": wait_ms}, operation)

    async def reload(self, session_id: str) -> dict[str, Any]:
        session = await self.get_session(session_id)

        async def operation() -> None:
            await session.page.reload(wait_until="domcontentloaded")
            await self._settle(session.page)

        return await self._run_action(session, "reload", {}, operation)

    async def go_back(self, session_id: str) -> dict[str, Any]:
        session = await self.get_session(session_id)

        async def operation() -> None:
            await session.page.go_back(wait_until="domcontentloaded")
            await self._settle(session.page)

        return await self._run_action(session, "go_back", {}, operation)

    async def go_forward(self, session_id: str) -> dict[str, Any]:
        session = await self.get_session(session_id)

        async def operation() -> None:
            await session.page.go_forward(wait_until="domcontentloaded")
            await self._settle(session.page)

        return await self._run_action(session, "go_forward", {}, operation)

    async def list_tabs(self, session_id: str) -> list[dict[str, Any]]:
        session = await self.get_session(session_id)
        async with session.lock:
            return await self._tab_summaries(session)

    async def activate_tab(self, session_id: str, index: int) -> dict[str, Any]:
        session = await self.get_session(session_id)
        async with session.lock:
            pages = self._tab_pages(session)
            if index < 0 or index >= len(pages):
                raise ValueError(f"Unknown tab index: {index}")
            target_page = pages[index]
            self._attach_page_listeners(target_page, session)
            if hasattr(target_page, "bring_to_front"):
                await target_page.bring_to_front()
            session.page = target_page
            await self._settle(session.page)
            await self._persist_session(session, status="active")
            return {
                "index": index,
                "session": await self._session_summary(session),
                "tabs": await self._tab_summaries(session),
            }

    async def close_tab(self, session_id: str, index: int) -> dict[str, Any]:
        session = await self.get_session(session_id)
        async with session.lock:
            pages = self._tab_pages(session)
            if index < 0 or index >= len(pages):
                raise ValueError(f"Unknown tab index: {index}")
            if len(pages) == 1:
                raise ValueError("Cannot close the only open tab in a session")
            target_page = pages[index]
            was_active = target_page is session.page
            await target_page.close()
            remaining = self._tab_pages(session)
            if was_active and remaining:
                session.page = remaining[max(0, min(index, len(remaining) - 1))]
                self._attach_page_listeners(session.page, session)
                if hasattr(session.page, "bring_to_front"):
                    await session.page.bring_to_front()
                await self._settle(session.page)
            await self._persist_session(session, status="active")
            return {
                "closed_index": index,
                "session": await self._session_summary(session),
                "tabs": await self._tab_summaries(session),
            }

    async def list_downloads(self, session_id: str) -> list[dict[str, Any]]:
        session = self.sessions.get(session_id)
        if session is not None:
            return list(session.downloads)
        record = await self.session_store.get(session_id)
        return list(record.downloads)

    async def extract_posts(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        session = await self.get_session(session_id)
        return await session.page.evaluate(EXTRACT_POSTS_SCRIPT, limit)

    async def extract_profile(self, session_id: str) -> dict[str, Any]:
        session = await self.get_session(session_id)
        return await session.page.evaluate(EXTRACT_PROFILE_SCRIPT)

    async def extract_comments(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        session = await self.get_session(session_id)
        return await session.page.evaluate(EXTRACT_COMMENTS_SCRIPT, limit)

    async def scroll_feed(self, session_id: str, direction: str = "down", screens: int = 3) -> dict[str, Any]:
        session = await self.get_session(session_id)
        delta = self.settings.default_viewport_height * screens
        if direction == "up":
            delta = -delta

        async def operation() -> None:
            await session.page.evaluate(SMOOTH_SCROLL_SCRIPT, delta)
            # Human pause after scroll
            await asyncio.sleep(0.3 + random.random() * 0.5)
            await self._settle(session.page)

        return await self._run_action(session, "scroll_feed", {"direction": direction, "screens": screens}, operation)

    # JS that finds the most likely text composer on the page
    _FIND_COMPOSER_SCRIPT = r"""
() => {
  const candidates = [
    '[data-testid="tweetTextarea_0"]',
    '[data-testid="tweetTextarea"]',
    '[aria-label*="post" i][contenteditable]',
    '[aria-label*="tweet" i][contenteditable]',
    '[aria-label*="write" i][contenteditable]',
    '[aria-label*="comment" i][contenteditable]',
    '[placeholder*="post" i]',
    '[placeholder*="tweet" i]',
    '[placeholder*="share" i]',
    '[placeholder*="what" i]',
    '[placeholder*="say" i]',
    'div[contenteditable="true"]',
    'textarea',
  ];
  for (const sel of candidates) {
    const el = document.querySelector(sel);
    if (el) {
      const rect = el.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) return sel;
    }
  }
  return null;
}
"""

    async def _locator_center(self, locator: Any) -> tuple[float, float] | None:
        try:
            box = await locator.bounding_box()
        except Exception:
            return None
        if not box:
            return None
        return (float(box["x"] + box["width"] / 2), float(box["y"] + box["height"] / 2))

    async def _move_mouse_human_like(self, session: BrowserSession, x: float, y: float) -> None:
        start = session.mouse_position
        if start is None:
            start = (
                self.settings.default_viewport_width / 2 + random.randint(-120, 120),
                self.settings.default_viewport_height / 2 + random.randint(-80, 80),
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
            px = (
                inv**3 * start_x
                + 3 * inv * inv * t * control_1[0]
                + 3 * inv * t * t * control_2[0]
                + t**3 * x
            )
            py = (
                inv**3 * start_y
                + 3 * inv * inv * t * control_1[1]
                + 3 * inv * t * t * control_2[1]
                + t**3 * y
            )
            await session.page.mouse.move(px, py)
            await asyncio.sleep(random.uniform(0.004, 0.018))
        session.mouse_position = (x, y)

    async def _click_human_like(self, session: BrowserSession, x: float, y: float) -> None:
        jitter_x = x + random.uniform(-2.5, 2.5)
        jitter_y = y + random.uniform(-2.5, 2.5)
        await self._move_mouse_human_like(session, jitter_x, jitter_y)
        await asyncio.sleep(random.uniform(0.03, 0.12))
        await session.page.mouse.down()
        await asyncio.sleep(random.uniform(0.02, 0.08))
        await session.page.mouse.up()
        session.mouse_position = (jitter_x, jitter_y)

    async def _focus_locator(self, session: BrowserSession, locator: Any) -> None:
        coords = await self._locator_center(locator)
        if coords is None:
            await locator.click()
        else:
            await self._click_human_like(session, coords[0], coords[1])
        await asyncio.sleep(0.05 + random.random() * 0.1)

    async def _type_text_human_like(self, page: Page, text: str) -> None:
        for index, char in enumerate(text):
            await page.keyboard.type(char)
            delay_ms = random.randint(
                self.settings.human_typing_min_delay_ms,
                self.settings.human_typing_max_delay_ms,
            )
            if index > 0 and index % random.randint(6, 12) == 0:
                delay_ms += random.randint(180, 600)
            await asyncio.sleep(delay_ms / 1000)

    async def _first_visible_locator(self, page: Page, selectors: list[str]) -> tuple[Any, str] | None:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() > 0 and await locator.is_visible():
                    return locator, selector
            except Exception:
                continue
        return None

    async def _accessibility_locator(
        self,
        page: Page,
        *,
        roles: set[str],
        include_keywords: list[str],
        exclude_keywords: list[str] | None = None,
        index: int = 0,
    ) -> tuple[Any, dict[str, Any]] | None:
        accessibility = getattr(page, "accessibility", None)
        if accessibility is None or not hasattr(accessibility, "snapshot"):
            return None
        try:
            snapshot = await accessibility.snapshot(interesting_only=True)
        except Exception:
            return None
        if not snapshot:
            return None

        normalized_roles = {item.lower() for item in roles}
        exclude = [item.lower() for item in (exclude_keywords or [])]
        candidates: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()

        def walk(node: dict[str, Any]) -> None:
            role = str(node.get("role") or "").lower()
            name = str(node.get("name") or "").strip()
            if role in normalized_roles and name:
                lowered = name.lower()
                if any(keyword in lowered for keyword in include_keywords) and not any(
                    keyword in lowered for keyword in exclude
                ):
                    key = (role, name)
                    if key not in seen:
                        seen.add(key)
                        candidates.append(key)
            for child in node.get("children") or []:
                if isinstance(child, dict):
                    walk(child)

        walk(snapshot)
        remaining = index
        for role, name in candidates:
            locator = page.get_by_role(role, name=re.compile(re.escape(name), re.IGNORECASE))
            try:
                count = await locator.count()
            except Exception:
                continue
            for offset in range(count):
                candidate = locator.nth(offset)
                try:
                    if not await candidate.is_visible():
                        continue
                except Exception:
                    continue
                if remaining == 0:
                    return candidate, {
                        "mode": "accessibility",
                        "accessibility_role": role,
                        "accessibility_name": name,
                    }
                remaining -= 1
        return None

    async def _raise_social_action_error(
        self,
        session: BrowserSession,
        *,
        action: str,
        code: str,
        message: str,
        retryable: bool,
        details: dict[str, Any] | None = None,
    ) -> None:
        raise SocialActionError(
            message,
            action=action,
            code=code,
            retryable=retryable,
            url=session.page.url,
            details=details,
        )

    async def _clear_focused_input(self, session: BrowserSession, locator: Any) -> None:
        try:
            await locator.fill("")
            return
        except Exception:
            pass
        await session.page.keyboard.press("Control+a")
        await asyncio.sleep(0.03)
        await session.page.keyboard.press("Delete")

    async def _ensure_no_social_interlock(self, session: BrowserSession, *, action: str) -> None:
        challenge = await self._check_bot_challenge(session)
        if challenge is not None:
            await self.request_human_takeover(session.id, reason=f"Bot challenge detected: {challenge['signal']}")
            await self._raise_social_action_error(
                session,
                action=action,
                code="captcha_detected",
                message=f"Human takeover required: {challenge['signal']}",
                retryable=False,
                details=challenge,
            )
        rate_limit = await self._check_rate_limit_signal(session)
        if rate_limit is not None:
            await self.request_human_takeover(session.id, reason=f"Rate limit detected: {rate_limit['signal']}")
            await self._raise_social_action_error(
                session,
                action=action,
                code="rate_limited",
                message=f"Platform blocked the action: {rate_limit['signal']}",
                retryable=False,
                details=rate_limit,
            )

    async def _check_rate_limit_signal(self, session: BrowserSession) -> dict[str, Any] | None:
        phrases = [
            "try again later",
            "rate limit exceeded",
            "too many requests",
            "you are being rate limited",
            "temporarily limited",
            "please wait a few moments",
        ]
        try:
            title = (await session.page.title()).lower()
            body_text = (
                await session.page.evaluate(
                    "() => [document.body?.innerText || '', ...Array.from(document.querySelectorAll('[role=\"dialog\"], [aria-live]')).map((el) => el.innerText || '')].join(' ').slice(0, 4000)"
                )
            ).lower()
        except Exception:
            return None
        combined = f"{session.page.url.lower()} {title} {body_text}"
        for phrase in phrases:
            if phrase in combined:
                return {
                    "signal": phrase,
                    "url": session.page.url,
                    "title": title,
                }
        return None

    async def _maybe_handle_totp(self, session: BrowserSession) -> dict[str, Any] | None:
        if not session.totp_secret:
            return None
        if pyotp is None:
            await self._raise_social_action_error(
                session,
                action="social_login",
                code="totp_unavailable",
                message="TOTP support is not installed in this controller runtime",
                retryable=False,
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
        located = await self._first_visible_locator(session.page, selectors)
        if located is None:
            return None

        locator, selector = located
        code = pyotp.TOTP(session.totp_secret).now()
        await self._focus_locator(session, locator)
        try:
            await locator.fill("")
        except Exception:
            await session.page.keyboard.press("Control+a")
            await session.page.keyboard.press("Delete")
        await self._type_text_human_like(session.page, code)
        submit = await self._first_visible_locator(
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
            coords = await self._locator_center(submit[0])
            if coords is None:
                await submit[0].click()
            else:
                await self._click_human_like(session, coords[0], coords[1])
        await self._settle(session.page)
        return {"selector": selector, "code_length": len(code)}

    @staticmethod
    def _platform_alias(platform: str) -> str:
        normalized = platform.strip().lower()
        if normalized == "twitter":
            return "x"
        if normalized in {"microsoft", "live"}:
            return "outlook"
        return normalized

    def _current_platform(self, session: BrowserSession) -> str | None:
        host = (urlparse(session.page.url).hostname or "").lower()
        if "x.com" in host or "twitter.com" in host:
            return "x"
        if "instagram.com" in host:
            return "instagram"
        if "linkedin.com" in host:
            return "linkedin"
        if "outlook.live.com" in host or "outlook.office.com" in host or "outlook.office365.com" in host:
            return "outlook"
        return None

    async def _persist_platform_auth_state(self, session: BrowserSession, platform: str) -> dict[str, Any]:
        safe_path = self._safe_session_auth_path(session, f"{self._platform_alias(platform)}-latest.json")
        auth_info = await self.auth_state.write_storage_state(session.context, safe_path)
        session.last_auth_state_path = Path(auth_info["path"]) if auth_info["path"] else None
        await self.audit.append(
            event_type="auth_state_saved",
            status="ok",
            action="save_storage_state",
            session_id=session.id,
            details={"saved_to": auth_info["path"], "encrypted": auth_info["encrypted"], "platform": platform},
        )
        return {
            "saved_to": auth_info["path"],
            "auth_state": auth_info,
        }

    async def _verify_post_submission(
        self,
        session: BrowserSession,
        *,
        action: str,
        text: str,
        composer_locator: Any,
        initial_url: str,
    ) -> dict[str, Any]:
        snippet = text[:120].lower().strip()
        for _ in range(20):
            challenge = await self._check_bot_challenge(session)
            if challenge is not None:
                await self.request_human_takeover(session.id, reason=f"Bot challenge detected: {challenge['signal']}")
                await self._raise_social_action_error(
                    session,
                    action=action,
                    code="captcha_detected",
                    message=f"Human takeover required: {challenge['signal']}",
                    retryable=False,
                    details=challenge,
                )

            rate_limit = await self._check_rate_limit_signal(session)
            if rate_limit is not None:
                await self.request_human_takeover(session.id, reason=f"Rate limit detected: {rate_limit['signal']}")
                await self._raise_social_action_error(
                    session,
                    action=action,
                    code="rate_limited",
                    message=f"Platform blocked the action: {rate_limit['signal']}",
                    retryable=False,
                    details=rate_limit,
                )

            success_toast = await self._first_visible_locator(
                session.page,
                [
                    '[role="status"]',
                    '[aria-live="polite"]',
                    '[data-testid*="toast"]',
                    '[data-testid*="confirmation"]',
                ],
            )
            if success_toast is not None:
                try:
                    toast_text = (await success_toast[0].inner_text()).strip()
                except Exception:
                    toast_text = ""
                lowered = toast_text.lower()
                if any(token in lowered for token in ["sent", "posted", "published", "success"]):
                    return {"verified": True, "signal": "success_toast", "detail": toast_text[:200]}

            if session.page.url != initial_url:
                return {"verified": True, "signal": "url_changed"}

            composer_text = ""
            try:
                composer_text = (
                    await composer_locator.evaluate(
                        "(el) => ('value' in el ? el.value : (el.innerText || el.textContent || '')).trim()"
                    )
                ) or ""
            except Exception:
                composer_text = ""

            if snippet and snippet not in composer_text.lower() and len(composer_text.strip()) < max(10, len(text) // 4):
                return {"verified": True, "signal": "composer_cleared"}

            body_text = (
                await session.page.evaluate(
                    "() => [document.body?.innerText || '', ...Array.from(document.querySelectorAll('[role=\"dialog\"], [aria-live]')).map((el) => el.innerText || '')].join(' ').slice(0, 6000)"
                )
            ).lower()
            if "character limit" in body_text or "too long" in body_text or "could not send" in body_text:
                await self._raise_social_action_error(
                    session,
                    action=action,
                    code="post_rejected",
                    message="Platform rejected the post after submit",
                    retryable=True,
                    details={"current_url": session.page.url},
                )
            if snippet and snippet in body_text and snippet not in composer_text.lower():
                return {"verified": True, "signal": "posted_text_visible"}

            await asyncio.sleep(0.35)

        await self._raise_social_action_error(
            session,
            action=action,
            code="post_unverified",
            message="The post was submitted but success could not be verified",
            retryable=True,
            details={"initial_url": initial_url, "current_url": session.page.url},
        )


    async def _interactable_keyword_target(
        self,
        session: BrowserSession,
        *,
        include_keywords: list[str],
        exclude_keywords: list[str] | None = None,
        tags: set[str] | None = None,
        index: int = 0,
    ) -> dict[str, Any] | None:
        include = [item.lower() for item in include_keywords]
        exclude = [item.lower() for item in (exclude_keywords or [])]
        interactables = await session.page.evaluate(INTERACTABLES_SCRIPT, 120)
        matches: list[dict[str, Any]] = []
        for item in interactables:
            label = str(item.get("label") or "").lower()
            tag = str(item.get("tag") or "").lower()
            role = str(item.get("role") or "").lower()
            if tags and tag not in tags and role not in tags:
                continue
            if not any(keyword in label for keyword in include):
                continue
            if any(keyword in label for keyword in exclude):
                continue
            bbox = item.get("bbox") or {}
            matches.append(
                {
                    "mode": "selector",
                    "selector": item.get("selector_hint"),
                    "element_id": item.get("element_id"),
                    "label": item.get("label"),
                    "x": float((bbox.get("x") or 0) + (bbox.get("width") or 0) / 2),
                    "y": float((bbox.get("y") or 0) + (bbox.get("height") or 0) / 2),
                    "matched_via": "interactables",
                }
            )
        if index < len(matches):
            return matches[index]
        return None

    async def _resolve_text_locator(
        self,
        session: BrowserSession,
        *,
        action: str,
        selectors: list[str],
        accessibility_keywords: list[str],
        accessibility_roles: set[str] | None = None,
        error_code: str,
        error_message: str,
    ) -> tuple[Any, dict[str, Any]]:
        filtered_selectors = [selector for selector in selectors if selector]
        located = await self._first_visible_locator(session.page, filtered_selectors)
        if located is not None:
            locator, selector = located
            target: dict[str, Any] = {"mode": "selector", "selector": selector}
            coords = await self._locator_center(locator)
            if coords is not None:
                target["x"], target["y"] = coords
            return locator, target

        accessibility = await self._accessibility_locator(
            session.page,
            roles=accessibility_roles or {"textbox", "searchbox", "combobox"},
            include_keywords=accessibility_keywords,
        )
        if accessibility is not None:
            locator, details = accessibility
            target = {"mode": "accessibility", **details}
            coords = await self._locator_center(locator)
            if coords is not None:
                target["x"], target["y"] = coords
            return locator, target

        interactable_target = await self._interactable_keyword_target(
            session,
            include_keywords=accessibility_keywords,
            tags={"textarea", "input", "textbox", "searchbox"},
        )
        if interactable_target is not None and interactable_target.get("selector"):
            locator = session.page.locator(str(interactable_target["selector"])).first
            return locator, interactable_target

        await self._raise_social_action_error(
            session,
            action=action,
            code=error_code,
            message=error_message,
            retryable=True,
            details={"keywords": accessibility_keywords},
        )

    async def _resolve_button_target(
        self,
        session: BrowserSession,
        *,
        action: str,
        primary_script: str | None,
        primary_arg: Any = None,
        include_keywords: list[str],
        exclude_keywords: list[str] | None = None,
        index: int = 0,
        error_code: str,
        error_message: str,
    ) -> dict[str, Any]:
        match = None
        if primary_script:
            try:
                if primary_arg is None:
                    match = await session.page.evaluate(primary_script)
                else:
                    match = await session.page.evaluate(primary_script, primary_arg)
            except Exception:
                match = None
        if isinstance(match, dict) and match:
            return {
                "mode": "coordinates",
                **match,
            }

        interactable_target = await self._interactable_keyword_target(
            session,
            include_keywords=include_keywords,
            exclude_keywords=exclude_keywords,
            tags={"button", "link"},
            index=index,
        )
        if interactable_target is not None:
            return interactable_target

        accessibility = await self._accessibility_locator(
            session.page,
            roles={"button", "link", "menuitem"},
            include_keywords=include_keywords,
            exclude_keywords=exclude_keywords,
            index=index,
        )
        if accessibility is not None:
            locator, details = accessibility
            target = {"mode": "accessibility", **details}
            coords = await self._locator_center(locator)
            if coords is not None:
                target["x"], target["y"] = coords
                return target

        await self._raise_social_action_error(
            session,
            action=action,
            code=error_code,
            message=error_message,
            retryable=True,
            details={"keywords": include_keywords, "exclude_keywords": exclude_keywords or []},
        )

    async def _click_target_payload(self, session: BrowserSession, target: dict[str, Any]) -> None:
        selector = target.get("selector")
        if selector:
            locator = session.page.locator(str(selector)).first
            await locator.scroll_into_view_if_needed()
            coords = await self._locator_center(locator)
            if coords is None:
                await locator.click()
                return
            target["x"], target["y"] = coords
            await self._click_human_like(session, coords[0], coords[1])
            return
        if target.get("x") is not None and target.get("y") is not None:
            await self._click_human_like(session, float(target["x"]), float(target["y"]))
            return
        await self._raise_social_action_error(
            session,
            action=str(target.get("action") or "social_click"),
            code="target_unusable",
            message="Resolved social target could not be clicked",
            retryable=True,
            details={"target": target},
        )

    async def _resolve_composer_locator(
        self,
        session: BrowserSession,
        *,
        action: str,
    ) -> tuple[Any, dict[str, Any]]:
        selector = None
        try:
            selector = await session.page.evaluate(self._FIND_COMPOSER_SCRIPT)
        except Exception:
            selector = None
        selectors = [
            *([selector] if selector else []),
            '[aria-label*="message" i][contenteditable]',
            '[placeholder*="message" i]',
            'div[role="textbox"][contenteditable="true"]',
            'div[contenteditable="true"]',
            'textarea',
            'input[type="text"]',
        ]
        return await self._resolve_text_locator(
            session,
            action=action,
            selectors=selectors,
            accessibility_keywords=["post", "tweet", "share", "write", "reply", "comment", "message"],
            error_code="composer_missing",
            error_message="No composer was found on the current page",
        )

    async def _resolve_search_locator(self, session: BrowserSession) -> tuple[Any, dict[str, Any]]:
        selector = None
        try:
            selector = await session.page.evaluate(FIND_SEARCH_INPUT_SCRIPT)
        except Exception:
            selector = None
        selectors = [
            *([selector] if selector else []),
            'input[type="search"]',
            'input[role="searchbox"]',
            '[role="searchbox"]',
            '[aria-label*="search" i]',
            '[placeholder*="search" i]',
        ]
        return await self._resolve_text_locator(
            session,
            action="search_page",
            selectors=selectors,
            accessibility_keywords=["search"],
            accessibility_roles={"searchbox", "textbox", "combobox"},
            error_code="search_input_missing",
            error_message="No search input found on the current page",
        )

    def _platform_login_config(self, platform: str) -> dict[str, Any]:
        if platform == "x":
            return {
                "login_url": "https://x.com/i/flow/login",
                "username_selectors": [
                    'input[autocomplete="username"]',
                    'input[name="text"]',
                    'input[name="session[username_or_email]"]',
                ],
                "username_continue_selectors": [
                    'button:has-text("Next")',
                    'button:has-text("Continue")',
                ],
                "password_selectors": [
                    'input[name="password"]',
                    'input[type="password"]',
                ],
                "submit_selectors": [
                    '[data-testid="LoginForm_Login_Button"]',
                    'button[type="submit"]',
                    'button:has-text("Log in")',
                ],
                "login_url_tokens": ["/login", "/i/flow/login"],
                "success_selectors": [
                    '[data-testid="SideNav_AccountSwitcher_Button"]',
                    '[aria-label*="Profile" i]',
                    'a[href="/home"]',
                ],
            }
        if platform == "instagram":
            return {
                "login_url": "https://www.instagram.com/accounts/login/",
                "username_selectors": ['input[name="username"]', 'input[autocomplete="username"]'],
                "username_continue_selectors": [],
                "password_selectors": ['input[name="password"]', 'input[type="password"]'],
                "submit_selectors": ['button[type="submit"]', 'button:has-text("Log in")'],
                "login_url_tokens": ["/accounts/login"],
                "success_selectors": [
                    'svg[aria-label="Home"]',
                    'a[href="/direct/inbox/"]',
                    'a[href="/accounts/edit/"]',
                ],
            }
        if platform == "linkedin":
            return {
                "login_url": "https://www.linkedin.com/login",
                "username_selectors": ['#username', 'input[name="session_key"]', 'input[autocomplete="username"]'],
                "username_continue_selectors": [],
                "password_selectors": ['#password', 'input[name="session_password"]', 'input[type="password"]'],
                "submit_selectors": ['button[type="submit"]', 'button:has-text("Sign in")'],
                "login_url_tokens": ["/login", "/checkpoint"],
                "success_selectors": [
                    'a[href*="/feed/"]',
                    'button[aria-label*="Account" i]',
                    'img.global-nav__me-photo',
                ],
            }
        if platform == "outlook":
            return {
                "login_url": "https://login.live.com/",
                "username_selectors": [
                    'input[name="loginfmt"]',
                    'input[type="email"]',
                    'input[autocomplete="username"]',
                ],
                "username_continue_selectors": [
                    '#idSIButton9',
                    'button[type="submit"]',
                    'input[type="submit"]',
                    'button:has-text("Next")',
                    'button:has-text("Sign in")',
                ],
                "password_selectors": [
                    'input[name="passwd"]',
                    'input[type="password"]',
                    'input[autocomplete="current-password"]',
                ],
                "submit_selectors": [
                    '#idSIButton9',
                    'button[type="submit"]',
                    'input[type="submit"]',
                    'button:has-text("Sign in")',
                ],
                "post_submit_selectors": [
                    '#idSIButton9',
                    'button:has-text("Yes")',
                    'input[type="submit"][value="Yes"]',
                ],
                "login_url_tokens": ["login.live.com", "login.srf", "ppsecure"],
                "success_selectors": [
                    'button[aria-label*="New mail" i]',
                    '[title="Inbox"]',
                    'button[aria-label*="Account manager" i]',
                    '[data-icon-name="MailRegular"]',
                ],
            }
        raise ValueError(f"Unsupported social platform: {platform}")

    def _platform_dm_compose_url(self, platform: str) -> str:
        if platform == "x":
            return "https://x.com/messages/compose"
        if platform == "instagram":
            return "https://www.instagram.com/direct/new/"
        if platform == "linkedin":
            return "https://www.linkedin.com/messaging/compose/"
        raise ValueError(f"Unsupported social platform: {platform}")

    async def social_login(
        self,
        session_id: str,
        *,
        platform: str,
        username: str,
        password: str,
        auth_profile: str | None = None,
        approval_id: str | None = None,
        totp_secret: str | None = None,
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        platform_alias = self._platform_alias(platform)
        if totp_secret:
            session.totp_secret = totp_secret

        approval = await self._require_decision_approval(
            session_id,
            BrowserActionDecision(
                action="social_login",
                reason=f"Log into {platform_alias} as {username}",
                platform=platform_alias,
                username=username,
                risk_category="account_change",
            ),
            approval_id=approval_id,
            fallback_reason="Logging into a social account requires approval",
        )
        target: dict[str, Any] = {"platform": platform_alias, "username": username}
        login_config = self._platform_login_config(platform_alias)
        auth_state_payload: dict[str, Any] = {}
        saved_auth_profile: dict[str, Any] | None = None
        totp_used: dict[str, Any] | None = None

        async def operation() -> None:
            nonlocal saved_auth_profile, totp_used
            if not any(token in session.page.url for token in login_config["login_url_tokens"]):
                await session.page.goto(login_config["login_url"], wait_until="domcontentloaded")
                await self._settle(session.page)

            username_locator, username_target = await self._resolve_text_locator(
                session,
                action="social_login",
                selectors=login_config["username_selectors"],
                accessibility_keywords=["email", "username", "phone"],
                error_code="login_username_missing",
                error_message="Could not find the login username field",
            )
            target.setdefault("username_target", username_target)
            await self._focus_locator(session, username_locator)
            await self._clear_focused_input(session, username_locator)
            await self._type_text_human_like(session.page, username)
            await self._settle(session.page)

            password_visible = await self._first_visible_locator(session.page, login_config["password_selectors"])
            if password_visible is None and login_config["username_continue_selectors"]:
                await self._submit_visible_button(
                    session,
                    action="social_login",
                    selectors=login_config["username_continue_selectors"],
                )
                await self._settle(session.page)

            password_locator, password_target = await self._resolve_text_locator(
                session,
                action="social_login",
                selectors=login_config["password_selectors"],
                accessibility_keywords=["password"],
                error_code="login_password_missing",
                error_message="Could not find the login password field",
            )
            target.setdefault("password_target", password_target)
            await self._focus_locator(session, password_locator)
            await self._clear_focused_input(session, password_locator)
            await self._type_text_human_like(session.page, password)
            await self._submit_visible_button(
                session,
                action="social_login",
                selectors=login_config["submit_selectors"],
            )
            await self._settle(session.page)

            totp_used = await self._maybe_handle_totp(session)

            for _ in range(20):
                await self._ensure_no_social_interlock(session, action="social_login")
                post_submit_selectors = login_config.get("post_submit_selectors", [])
                if post_submit_selectors:
                    interstitial = await self._first_visible_locator(session.page, post_submit_selectors)
                    if interstitial is not None:
                        await self._submit_visible_button(
                            session,
                            action="social_login",
                            selectors=post_submit_selectors,
                        )
                        await self._settle(session.page)
                        continue
                success = await self._first_visible_locator(session.page, login_config.get("success_selectors", []))
                if success is not None:
                    target["success_selector"] = success[1]
                    break
                password_field = await self._first_visible_locator(session.page, login_config["password_selectors"])
                username_field = await self._first_visible_locator(session.page, login_config["username_selectors"])
                if password_field is None and username_field is None and not any(
                    token in session.page.url for token in login_config["login_url_tokens"]
                ):
                    break
                await asyncio.sleep(0.5)
            else:
                await self._raise_social_action_error(
                    session,
                    action="social_login",
                    code="login_unverified",
                    message="Login completed but success could not be verified",
                    retryable=True,
                    details={"platform": platform_alias, "current_url": session.page.url},
                )

            auth_state_payload.update(await self._persist_platform_auth_state(session, platform_alias))
            if auth_profile:
                saved_auth_profile = await self._save_auth_profile_for_session(
                    session,
                    auth_profile,
                    metadata={
                        "platform": platform_alias,
                        "username": username,
                        "saved_via": "social_login",
                    },
                )

        result = await self._run_action(session, "social_login", target, operation)
        result["auth_state"] = auth_state_payload
        if saved_auth_profile is not None:
            result["saved_auth_profile"] = saved_auth_profile
        result["totp_used"] = totp_used
        if approval is not None:
            await self.approvals.mark_executed(approval.id)
        return result

    async def post_content(
        self,
        session_id: str,
        text: str,
        *,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        approval = await self._require_decision_approval(
            session_id,
            BrowserActionDecision(
                action="social_post",
                reason="Publish a social post",
                text=text,
                risk_category="post",
            ),
            approval_id=approval_id,
            fallback_reason="Publishing a social post requires approval",
        )
        locator, target = await self._resolve_composer_locator(session, action="social_post")
        target["text_preview"] = text[:160]
        initial_url = session.page.url
        delivery: dict[str, Any] = {}

        async def operation() -> None:
            await locator.scroll_into_view_if_needed()
            await self._focus_locator(session, locator)
            try:
                await locator.fill("")
            except Exception:
                await session.page.keyboard.press("Control+a")
                await session.page.keyboard.press("Delete")
            await self._type_text_human_like(session.page, text)
            await asyncio.sleep(0.25 + random.random() * 0.25)
            target["submit_selector"] = await self._submit_visible_button(
                session,
                action="social_post",
                selectors=[
                    '[data-testid="tweetButtonInline"]',
                    '[data-testid="tweetButton"]',
                    'button[type="submit"]',
                    '[aria-label*="post" i][role="button"]',
                    '[aria-label*="tweet" i][role="button"]',
                    '[aria-label*="share" i][role="button"]',
                    'button:has-text("Post")',
                    'button:has-text("Tweet")',
                    'button:has-text("Share")',
                    'button:has-text("Submit")',
                ],
            )
            await self._settle(session.page)
            delivery.update(
                await self._verify_post_submission(
                    session,
                    action="social_post",
                    text=text,
                    composer_locator=locator,
                    initial_url=initial_url,
                )
            )

        result = await self._run_action(session, "social_post", target, operation)
        result["delivery"] = delivery or {
            "verified": False,
            "signal": "unknown",
        }
        if approval is not None:
            await self.approvals.mark_executed(approval.id)
        return result

    async def comment_on_post(
        self,
        session_id: str,
        *,
        text: str,
        post_index: int = 0,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        approval = await self._require_decision_approval(
            session_id,
            BrowserActionDecision(
                action="social_comment",
                reason="Comment on the selected social post",
                text=text,
                index=post_index,
                risk_category="post",
            ),
            approval_id=approval_id,
            fallback_reason="Commenting on a post requires approval",
        )
        target = await self._resolve_button_target(
            session,
            action="social_comment",
            primary_script=FIND_REPLY_BUTTON_SCRIPT,
            primary_arg=post_index,
            include_keywords=["reply", "comment"],
            error_code="reply_button_missing",
            error_message="No reply or comment button was found on the current page",
        )
        target["post_index"] = post_index
        target["text_preview"] = text[:160]
        initial_url = session.page.url
        delivery: dict[str, Any] = {}

        async def operation() -> None:
            await self._click_target_payload(session, target)
            await self._settle(session.page)
            composer_locator, composer_target = await self._resolve_composer_locator(session, action="social_comment")
            target["composer"] = composer_target
            await self._focus_locator(session, composer_locator)
            try:
                await composer_locator.fill("")
            except Exception:
                await session.page.keyboard.press("Control+a")
                await session.page.keyboard.press("Delete")
            await self._type_text_human_like(session.page, text)
            target["submit_selector"] = await self._submit_visible_button(
                session,
                action="social_comment",
                selectors=[
                    '[data-testid="tweetButton"]',
                    'button[type="submit"]',
                    '[aria-label*="reply" i][role="button"]',
                    '[aria-label*="comment" i][role="button"]',
                    'button:has-text("Reply")',
                    'button:has-text("Comment")',
                    'button:has-text("Post")',
                    'button:has-text("Send")',
                ],
            )
            await self._settle(session.page)
            delivery.update(
                await self._verify_post_submission(
                    session,
                    action="social_comment",
                    text=text,
                    composer_locator=composer_locator,
                    initial_url=initial_url,
                )
            )

        result = await self._run_action(session, "social_comment", target, operation)
        result["delivery"] = delivery or {"verified": False, "signal": "unknown"}
        if approval is not None:
            await self.approvals.mark_executed(approval.id)
        return result

    async def like_post(
        self,
        session_id: str,
        post_index: int = 0,
        *,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        target = await self._resolve_button_target(
            session,
            action="social_like",
            primary_script=FIND_LIKE_BUTTON_SCRIPT,
            primary_arg=post_index,
            include_keywords=["like", "heart", "love"],
            exclude_keywords=["liked"],
            index=post_index,
            error_code="like_button_missing",
            error_message="No like button was found on the current page",
        )
        approval = await self._require_decision_approval(
            session_id,
            BrowserActionDecision(
                action="social_like",
                reason="Like the selected social post",
                index=post_index,
                risk_category="post",
            ),
            approval_id=approval_id,
            fallback_reason="Liking a post requires approval",
        )
        target["post_index"] = post_index

        async def operation() -> None:
            await asyncio.sleep(0.08 + random.random() * 0.12)
            await self._click_target_payload(session, target)
            await self._settle(session.page)

        result = await self._run_action(session, "social_like", target, operation)
        if approval is not None:
            await self.approvals.mark_executed(approval.id)
        return result

    async def follow_user(
        self,
        session_id: str,
        *,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        target = await self._resolve_button_target(
            session,
            action="social_follow",
            primary_script=FIND_FOLLOW_BUTTON_SCRIPT,
            include_keywords=["follow"],
            exclude_keywords=["unfollow", "following"],
            error_code="follow_button_missing",
            error_message="No follow button was found on the current page",
        )
        approval = await self._require_decision_approval(
            session_id,
            BrowserActionDecision(
                action="social_follow",
                reason="Follow the current social profile",
                risk_category="post",
            ),
            approval_id=approval_id,
            fallback_reason="Following an account requires approval",
        )

        async def operation() -> None:
            await asyncio.sleep(0.08 + random.random() * 0.12)
            await self._click_target_payload(session, target)
            await self._settle(session.page)

        result = await self._run_action(session, "social_follow", target, operation)
        if approval is not None:
            await self.approvals.mark_executed(approval.id)
        return result

    async def unfollow_user(
        self,
        session_id: str,
        *,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        target = await self._resolve_button_target(
            session,
            action="social_unfollow",
            primary_script=FIND_UNFOLLOW_BUTTON_SCRIPT,
            include_keywords=["following", "unfollow"],
            error_code="unfollow_button_missing",
            error_message="No following or unfollow button was found on the current page",
        )
        approval = await self._require_decision_approval(
            session_id,
            BrowserActionDecision(
                action="social_unfollow",
                reason="Unfollow the current social profile",
                risk_category="post",
            ),
            approval_id=approval_id,
            fallback_reason="Unfollowing an account requires approval",
        )

        async def operation() -> None:
            await self._click_target_payload(session, target)
            await self._settle(session.page)
            confirm = await self._first_visible_locator(
                session.page,
                [
                    '[data-testid="confirmationSheetConfirm"]',
                    'button:has-text("Unfollow")',
                    '[role="menuitem"]:has-text("Unfollow")',
                ],
            )
            if confirm is not None:
                coords = await self._locator_center(confirm[0])
                if coords is None:
                    await confirm[0].click()
                else:
                    await self._click_human_like(session, coords[0], coords[1])
                await self._settle(session.page)

        result = await self._run_action(session, "social_unfollow", target, operation)
        if approval is not None:
            await self.approvals.mark_executed(approval.id)
        return result

    async def repost_post(
        self,
        session_id: str,
        post_index: int = 0,
        *,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        target = await self._resolve_button_target(
            session,
            action="social_repost",
            primary_script=FIND_REPOST_BUTTON_SCRIPT,
            primary_arg=post_index,
            include_keywords=["repost", "retweet"],
            error_code="repost_button_missing",
            error_message="No repost or retweet button was found on the current page",
        )
        approval = await self._require_decision_approval(
            session_id,
            BrowserActionDecision(
                action="social_repost",
                reason="Repost the selected social post",
                index=post_index,
                risk_category="post",
            ),
            approval_id=approval_id,
            fallback_reason="Reposting a post requires approval",
        )
        target["post_index"] = post_index

        async def operation() -> None:
            await self._click_target_payload(session, target)
            await self._settle(session.page)
            confirm = await self._first_visible_locator(
                session.page,
                [
                    '[data-testid="retweetConfirm"]',
                    'button:has-text("Repost")',
                    'button:has-text("Retweet")',
                    '[role="menuitem"]:has-text("Repost")',
                    '[role="menuitem"]:has-text("Retweet")',
                ],
            )
            if confirm is not None:
                coords = await self._locator_center(confirm[0])
                if coords is None:
                    await confirm[0].click()
                else:
                    await self._click_human_like(session, coords[0], coords[1])
                await self._settle(session.page)

        result = await self._run_action(session, "social_repost", target, operation)
        if approval is not None:
            await self.approvals.mark_executed(approval.id)
        return result

    async def send_direct_message(
        self,
        session_id: str,
        *,
        recipient: str,
        text: str,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        platform = self._current_platform(session)
        if platform is None:
            await self._raise_social_action_error(
                session,
                action="social_dm",
                code="unsupported_platform",
                message="Direct messages are only implemented for X, Instagram, and LinkedIn",
                retryable=False,
                details={"url": session.page.url},
            )
        approval = await self._require_decision_approval(
            session_id,
            BrowserActionDecision(
                action="social_dm",
                reason=f"Send a direct message to {recipient}",
                recipient=recipient,
                text=text,
                risk_category="post",
            ),
            approval_id=approval_id,
            fallback_reason="Sending a direct message requires approval",
        )
        target: dict[str, Any] = {"platform": platform, "recipient": recipient, "text_preview": text[:160]}
        compose_url = self._platform_dm_compose_url(platform)
        delivery: dict[str, Any] = {}

        async def operation() -> None:
            if session.page.url != compose_url:
                await session.page.goto(compose_url, wait_until="domcontentloaded")
                await self._settle(session.page)
            recipient_locator, recipient_target = await self._resolve_text_locator(
                session,
                action="social_dm",
                selectors=[
                    'input[role="combobox"]',
                    'input[placeholder*="search" i]',
                    'input[placeholder*="name" i]',
                    'input[aria-label*="search" i]',
                ],
                accessibility_keywords=["search", "name", "recipient", "to"],
                error_code="dm_recipient_missing",
                error_message="Could not find the DM recipient field",
            )
            target["recipient_target"] = recipient_target
            await self._focus_locator(session, recipient_locator)
            try:
                await recipient_locator.fill("")
            except Exception:
                await session.page.keyboard.press("Control+a")
                await session.page.keyboard.press("Delete")
            await self._type_text_human_like(session.page, recipient)
            await asyncio.sleep(0.5)
            await session.page.keyboard.press("Enter")
            await self._settle(session.page)
            message_locator, message_target = await self._resolve_text_locator(
                session,
                action="social_dm",
                selectors=[
                    'div[contenteditable="true"][role="textbox"]',
                    '[aria-label*="message" i][contenteditable="true"]',
                    'textarea[placeholder*="message" i]',
                    'textarea',
                ],
                accessibility_keywords=["message", "reply", "compose"],
                error_code="dm_composer_missing",
                error_message="Could not find the DM message composer",
            )
            target["composer"] = message_target
            await self._focus_locator(session, message_locator)
            try:
                await message_locator.fill("")
            except Exception:
                await session.page.keyboard.press("Control+a")
                await session.page.keyboard.press("Delete")
            await self._type_text_human_like(session.page, text)
            target["submit_selector"] = await self._submit_visible_button(
                session,
                action="social_dm",
                selectors=[
                    '[data-testid="dmComposerSendButton"]',
                    'button[type="submit"]',
                    '[aria-label*="send" i][role="button"]',
                    'button:has-text("Send")',
                ],
            )
            await self._settle(session.page)
            delivery.update(
                await self._verify_post_submission(
                    session,
                    action="social_dm",
                    text=text,
                    composer_locator=message_locator,
                    initial_url=compose_url,
                )
            )

        result = await self._run_action(session, "social_dm", target, operation)
        result["delivery"] = delivery or {"verified": False, "signal": "unknown"}
        if approval is not None:
            await self.approvals.mark_executed(approval.id)
        return result

    async def search_page(self, session_id: str, query: str) -> dict[str, Any]:
        session = await self.get_session(session_id)
        locator, target = await self._resolve_search_locator(session)
        target["query"] = query

        async def operation() -> None:
            await self._focus_locator(session, locator)
            await session.page.keyboard.press("Control+A")
            await self._type_text_human_like(session.page, query)
            await asyncio.sleep(0.2 + random.random() * 0.15)
            await session.page.keyboard.press("Enter")
            await self._settle(session.page)

        return await self._run_action(session, "search_page", target, operation)

    async def execute_decision(
        self,
        session_id: str,
        decision: BrowserActionDecision,
        *,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        if decision.action == "social_post":
            return await self.post_content(session_id, decision.text or "", approval_id=approval_id)
        if decision.action == "social_comment":
            return await self.comment_on_post(
                session_id,
                text=decision.text or "",
                post_index=decision.index or 0,
                approval_id=approval_id,
            )
        if decision.action == "social_like":
            return await self.like_post(session_id, post_index=decision.index or 0, approval_id=approval_id)
        if decision.action == "social_follow":
            return await self.follow_user(session_id, approval_id=approval_id)
        if decision.action == "social_unfollow":
            return await self.unfollow_user(session_id, approval_id=approval_id)
        if decision.action == "social_repost":
            return await self.repost_post(session_id, post_index=decision.index or 0, approval_id=approval_id)
        if decision.action == "social_dm":
            return await self.send_direct_message(
                session_id,
                recipient=decision.recipient or "",
                text=decision.text or "",
                approval_id=approval_id,
            )
        if decision.action == "social_login":
            raise ValueError("Use the dedicated social login endpoint/tool with credentials and optional approval_id")

        approval = await self._require_decision_approval(
            session_id,
            decision,
            approval_id=approval_id,
        )

        if decision.action == "navigate":
            result = await self.navigate(session_id, decision.url or "")
        elif decision.action == "click":
            result = await self.click(
                session_id,
                selector=decision.selector,
                element_id=decision.element_id,
                x=decision.x,
                y=decision.y,
            )
        elif decision.action == "hover":
            result = await self.hover(
                session_id,
                selector=decision.selector,
                element_id=decision.element_id,
                x=decision.x,
                y=decision.y,
            )
        elif decision.action == "select_option":
            result = await self.select_option(
                session_id,
                selector=decision.selector,
                element_id=decision.element_id,
                value=decision.value,
                label=decision.label,
                index=decision.index,
            )
        elif decision.action == "type":
            result = await self.type(
                session_id,
                selector=decision.selector,
                element_id=decision.element_id,
                text=decision.text or "",
                clear_first=decision.clear_first,
                sensitive=decision.sensitive,
            )
        elif decision.action == "press":
            result = await self.press(session_id, decision.key or "")
        elif decision.action == "scroll":
            result = await self.scroll(session_id, decision.delta_x, decision.delta_y)
        elif decision.action == "wait":
            result = await self.wait(session_id, decision.wait_ms)
        elif decision.action == "reload":
            result = await self.reload(session_id)
        elif decision.action == "go_back":
            result = await self.go_back(session_id)
        elif decision.action == "go_forward":
            result = await self.go_forward(session_id)
        elif decision.action == "upload":
            result = await self.upload(
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
            await self.approvals.mark_executed(approval.id)
        return result

    async def upload(

        self,
        session_id: str,
        *,
        file_path: str,
        approved: bool,
        approval_id: str | None = None,
        selector: str | None = None,
        element_id: str | None = None,
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        safe_path = self._safe_upload_path(file_path, session=session)
        approval = await self._require_decision_approval(
            session_id,
            BrowserActionDecision(
                action="upload",
                reason="Manual upload request",
                selector=selector,
                element_id=element_id,
                file_path=file_path,
                risk_category="upload",
            ),
            approval_id=approval_id,
            fallback_reason="Upload actions require approval",
        )

        target = self._resolve_target(selector=selector, element_id=element_id)

        async def operation() -> None:
            locator = session.page.locator(target["selector"]).first
            await locator.set_input_files(str(safe_path))
            await self._settle(session.page)

        result = await self._run_action(
            session,
            "upload",
            {**target, "file_path": str(safe_path), "approved": bool(approval), "approval_id": approval_id},
            operation,
        )
        if approval is not None:
            await self.approvals.mark_executed(approval.id)
        return result

    async def save_storage_state(self, session_id: str, path: str) -> dict[str, Any]:
        session = await self.get_session(session_id)
        safe_path = self._safe_session_auth_path(session, path)
        async with session.lock:
            auth_info = await self.auth_state.write_storage_state(session.context, safe_path)
            session.last_auth_state_path = Path(auth_info["path"]) if auth_info["path"] else None
            payload = {
                "saved_to": auth_info["path"],
                "auth_state": auth_info,
                "session": await self._session_summary(session),
            }
            await self._append_jsonl(
                session.artifact_dir / "actions.jsonl",
                {"timestamp": self._timestamp(), "action": "save_storage_state", **payload},
            )
            await self.audit.append(
                event_type="auth_state_saved",
                status="ok",
                action="save_storage_state",
                session_id=session.id,
                details={"saved_to": auth_info["path"], "encrypted": auth_info["encrypted"]},
            )
            await self._persist_session(session, status="active")
            return payload

    async def _save_auth_profile_for_session(
        self,
        session: BrowserSession,
        profile_name: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = self._normalize_auth_profile_name(profile_name)
        profile_state_path = self._auth_profile_state_base_path(normalized, create=True)
        auth_info = await self.auth_state.write_storage_state(session.context, profile_state_path)
        session.last_auth_state_path = Path(auth_info["path"]) if auth_info["path"] else None
        session.auth_profile_name = normalized

        profile_payload = {
            "profile_name": normalized,
            "last_saved_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "saved_from_session_id": session.id,
            "saved_from_url": session.page.url,
            "saved_from_title": await session.page.title(),
            "platform": self._current_platform(session),
        }
        if metadata:
            profile_payload.update(metadata)

        metadata_path = self._auth_profile_metadata_path(normalized, create=True)
        metadata_path.write_text(json.dumps(profile_payload, indent=2, sort_keys=True), encoding="utf-8")
        return {
            "profile_name": normalized,
            "saved_to": auth_info["path"],
            "auth_state": auth_info,
            "metadata": profile_payload,
        }

    async def save_auth_profile(self, session_id: str, profile_name: str) -> dict[str, Any]:
        session = await self.get_session(session_id)
        async with session.lock:
            payload = await self._save_auth_profile_for_session(session, profile_name)
            payload["session"] = await self._session_summary(session)
            await self._append_jsonl(
                session.artifact_dir / "actions.jsonl",
                {"timestamp": self._timestamp(), "action": "save_auth_profile", **payload},
            )
            await self.audit.append(
                event_type="auth_profile_saved",
                status="ok",
                action="save_auth_profile",
                session_id=session.id,
                details={"profile_name": payload["profile_name"], "saved_to": payload["saved_to"]},
            )
            await self._persist_session(session, status="active")
            return payload

    async def get_auth_profile(self, profile_name: str) -> dict[str, Any]:
        normalized = self._normalize_auth_profile_name(profile_name)
        profile_dir = self._auth_profile_dir(normalized, create=False)
        metadata = self._read_auth_profile_metadata(normalized)
        state_path = self._resolve_auth_profile_state_path(normalized, must_exist=False)
        state_exists = state_path.exists()
        if not state_exists and not metadata:
            raise KeyError(normalized)
        return {
            "profile_name": normalized,
            "profile_dir": str(profile_dir),
            "auth_state": self.auth_state.inspect(state_path if state_exists else None),
            "metadata": metadata,
        }

    async def list_auth_profiles(self) -> list[dict[str, Any]]:
        root = self._auth_profile_root()
        if not root.exists():
            return []
        profiles: list[dict[str, Any]] = []
        for directory in sorted((item for item in root.iterdir() if item.is_dir()), key=lambda item: item.name.lower()):
            try:
                profiles.append(await self.get_auth_profile(directory.name))
            except KeyError:
                continue
        profiles.sort(
            key=lambda item: (item.get("metadata") or {}).get("last_saved_at") or "",
            reverse=True,
        )
        return profiles

    async def request_human_takeover(self, session_id: str, reason: str) -> dict[str, Any]:
        session = await self.get_session(session_id)
        payload = {
            "session": await self._session_summary(session),
            "reason": reason,
            "takeover_url": self._current_takeover_url(session),
            "remote_access": self._session_remote_access_info(session),
            "message": (
                "Human takeover requested. Open the noVNC URL to continue visually."
                if session.isolation_mode == "docker_ephemeral"
                else "Human takeover requested. Open the noVNC URL to continue visually. In this POC, takeover is global to the single browser desktop."
            ),
        }
        await self._append_jsonl(
            session.artifact_dir / "actions.jsonl",
            {"timestamp": self._timestamp(), "action": "request_human_takeover", **payload},
        )
        await self.audit.append(
            event_type="takeover_requested",
            status="ok",
            action="request_human_takeover",
            session_id=session.id,
            details={"reason": reason},
        )
        await self._persist_session(session, status="active")
        return payload

    async def _require_decision_approval(
        self,
        session_id: str,
        decision: BrowserActionDecision,
        *,
        approval_id: str | None,
        fallback_reason: str | None = None,
    ):
        kind = self._approval_kind_for_decision(decision)
        if kind is None:
            return None
        if approval_id:
            return await self.approvals.require_approved(
                approval_id=approval_id,
                session_id=session_id,
                kind=kind,
                action=decision,
            )

        session = await self.get_session(session_id)
        approval = await self.approvals.create_or_reuse_pending(
            session_id=session_id,
            kind=kind,
            reason=fallback_reason or decision.reason,
            action=decision,
            observation=await self._approval_observation(session),
        )
        raise ApprovalRequiredError(approval)

    async def close_session(self, session_id: str) -> dict[str, Any]:
        session = await self.get_session(session_id)
        async with session.lock:
            if session.tunnel is not None:
                await self.tunnel_broker.release(session.tunnel)
            summary = await self._session_summary(session, status="closed", live=False)
            if self.settings.enable_tracing:
                try:
                    await session.context.tracing.stop(path=str(session.trace_path))
                except Exception as exc:  # pragma: no cover - depends on external browser support
                    logger.warning("failed to stop tracing for session %s: %s", session_id, exc)
            try:
                await session.context.close()
            finally:
                if session.browser is not None and session.browser is not self.browser:
                    try:
                        await session.browser.close()
                    except Exception as exc:  # pragma: no cover - best effort isolated cleanup
                        logger.warning("failed to close isolated browser for session %s: %s", session_id, exc)
                if session.runtime is not None:
                    await self.runtime_provisioner.release(session.runtime)
            await self.session_store.upsert(SessionRecord.model_validate(summary))
            self.sessions.pop(session_id, None)
            await self.audit.append(
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
            return {"closed": True, "trace_path": str(session.trace_path), "session": summary}

    async def _maybe_provision_session_tunnel(self, session: BrowserSession) -> None:
        if session.isolation_mode != "docker_ephemeral" or session.runtime is None:
            return
        if not self.tunnel_broker.enabled:
            return
        if session.runtime.novnc_port is None or not self._takeover_url_is_local_only(session.takeover_url):
            return
        try:
            session.tunnel = await self.tunnel_broker.provision(
                session.id,
                local_host=session.runtime.tunnel_local_host,
                local_port=session.runtime.tunnel_local_port,
            )
            session.tunnel_error = None
        except Exception as exc:
            session.tunnel = None
            session.tunnel_error = str(exc)
            logger.warning("failed to provision isolated tunnel for session %s: %s", session.id, exc)

    async def _run_action(
        self,
        session: BrowserSession,
        action_name: str,
        target: dict[str, Any],
        operation,
    ) -> dict[str, Any]:
        async with session.lock:
            before = await self._light_snapshot(session, label=f"before-{action_name}")
            try:
                await operation()
                totp_result = await self._maybe_handle_totp(session)
                if totp_result is not None:
                    target.setdefault("totp", totp_result)
                self._assert_runtime_url_allowed(session.page.url)
                challenge = await self._check_bot_challenge(session)
                if challenge is not None:
                    await self.request_human_takeover(session.id, reason=f"Bot challenge detected: {challenge['signal']}")
                    raise SocialActionError(
                        f"Bot challenge detected: {challenge['signal']}",
                        action=action_name,
                        code="captcha_detected",
                        retryable=False,
                        url=session.page.url,
                        details=challenge,
                    )
                if action_name.startswith("social_") or action_name in {"like_post", "follow_user", "search_page"}:
                    rate_limit = await self._check_rate_limit_signal(session)
                    if rate_limit is not None:
                        await self.request_human_takeover(session.id, reason=f"Rate limit detected: {rate_limit['signal']}")
                        raise SocialActionError(
                            f"Platform blocked the action: {rate_limit['signal']}",
                            action=action_name,
                            code="rate_limited",
                            retryable=False,
                            url=session.page.url,
                            details=rate_limit,
                        )
            except PermissionError as exc:
                try:
                    if session.page.url != before.get("url"):
                        await session.page.go_back(wait_until="domcontentloaded")
                        await self._settle(session.page)
                except Exception:
                    pass
                failed = await self._light_snapshot(session, label=f"blocked-{action_name}")
                await self._append_jsonl(
                    session.artifact_dir / "actions.jsonl",
                    {
                        "timestamp": self._timestamp(),
                        "action": action_name,
                        "status": "blocked",
                        "target": target,
                        "before": before,
                        "after": failed,
                        "error": str(exc),
                    },
                )
                await self.audit.append(
                    event_type="browser_action",
                    status="blocked",
                    action=action_name,
                    session_id=session.id,
                    details={"target": target, "error": str(exc)},
                )
                raise BrowserActionError(
                    str(exc),
                    code="browser_action_blocked",
                    action=action_name,
                    status_code=403,
                    retryable=False,
                    url=session.page.url,
                    details={"snapshot": failed},
                ) from exc
            except SocialActionError as exc:
                failed = await self._light_snapshot(session, label=f"failed-{action_name}")
                await self._append_jsonl(
                    session.artifact_dir / "actions.jsonl",
                    {
                        "timestamp": self._timestamp(),
                        "action": action_name,
                        "status": "failed",
                        "target": target,
                        "before": before,
                        "after": failed,
                        "error": exc.payload,
                    },
                )
                await self.audit.append(
                    event_type="browser_action",
                    status="failed",
                    action=action_name,
                    session_id=session.id,
                    details={"target": target, "error": exc.payload},
                )
                exc.details.setdefault("snapshot", failed)
                raise
            except PlaywrightError as exc:
                failed = await self._light_snapshot(session, label=f"failed-{action_name}")
                await self._append_jsonl(
                    session.artifact_dir / "actions.jsonl",
                    {
                        "timestamp": self._timestamp(),
                        "action": action_name,
                        "status": "failed",
                        "target": target,
                        "before": before,
                        "after": failed,
                        "error": str(exc),
                    },
                )
                await self.audit.append(
                    event_type="browser_action",
                    status="failed",
                    action=action_name,
                    session_id=session.id,
                    details={"target": target, "error": str(exc)},
                )
                raise BrowserActionError(
                    f"Action failed for {action_name}. Refresh observation and retry.",
                    code="browser_action_failed",
                    action=action_name,
                    status_code=400,
                    retryable=True,
                    url=session.page.url,
                    details={"snapshot": failed, "details": str(exc)},
                ) from exc
            after = await self._observation_payload(session, limit=20, screenshot_label=f"after-{action_name}")
            session.last_action = action_name
            verification = self._action_verification(action_name, target, before, after)
            payload = {
                "timestamp": self._timestamp(),
                "action": action_name,
                "action_class": self._action_class(action_name),
                "target": target,
                "before": before,
                "after": after,
                "verification": verification,
            }
            await self._append_jsonl(session.artifact_dir / "actions.jsonl", payload)
            await self.audit.append(
                event_type="browser_action",
                status="ok",
                action=action_name,
                session_id=session.id,
                details={"target": target, "verification": verification},
            )
            await self._persist_session(session, status="active")
            return {
                "action": action_name,
                "action_class": self._action_class(action_name),
                "session": await self._session_summary(session),
                "before": before,
                "after": after,
                "target": target,
                "verification": verification,
            }


    # Known bot challenge URL patterns and page signals
    _BOT_CHALLENGE_SIGNALS = [
        "challenge.cloudflare.com",
        "challenges.cloudflare.com",
        "/cdn-cgi/challenge-platform/",
        "captcha",
        "recaptcha",
        "hcaptcha",
        "arkose",
        "unusual activity",
        "suspicious activity",
        "verify you're human",
        "verify you are human",
        "security check",
        "access denied",
        "bot detected",
    ]

    async def _check_bot_challenge(self, session: BrowserSession) -> dict[str, Any] | None:
        """Return a takeover payload if a bot challenge is detected, else None."""
        url = session.page.url.lower()
        title = ""
        body_text = ""
        iframe_sources: list[str] = []
        try:
            title = (await session.page.title()).lower()
            body_text = (await session.page.evaluate("() => document.body?.innerText?.slice(0, 500) || ''")).lower()
            iframe_sources = [
                item.lower()
                for item in (
                    await session.page.evaluate(
                        "() => Array.from(document.querySelectorAll('iframe')).map((el) => el.src || el.getAttribute('src') || '')"
                    )
                )
            ]
        except Exception:
            pass

        combined = f"{url} {title} {body_text} {' '.join(iframe_sources)}"
        for signal in self._BOT_CHALLENGE_SIGNALS:
            if signal in combined:
                return {
                    "bot_challenge_detected": True,
                    "signal": signal,
                    "url": session.page.url,
                    "title": title,
                    "iframes": iframe_sources[:10],
                }
        return None

    async def _observation_payload(
        self,
        session: BrowserSession,
        *,
        limit: int = 40,
        screenshot_label: str = "observe",
    ) -> dict[str, Any]:
        interactables = await session.page.evaluate(INTERACTABLES_SCRIPT, limit)
        screenshot = await self._capture_screenshot(session, screenshot_label)
        summary = await self._page_summary(session.page)
        ocr = await self.ocr.extract_from_image(screenshot["path"])
        tabs = await self._tab_summaries(session)
        return {
            "session": await self._session_summary(session),
            "url": session.page.url,
            "title": summary["title"],
            "active_element": summary["active_element"],
            "text_excerpt": summary["text_excerpt"],
            "dom_outline": summary["dom_outline"],
            "accessibility_outline": summary["accessibility_outline"],
            "ocr": ocr,
            "interactables": interactables,
            "screenshot_path": screenshot["path"],
            "screenshot_url": screenshot["url"],
            "console_messages": session.console_messages[-10:],
            "page_errors": session.page_errors[-10:],
            "request_failures": session.request_failures[-10:],
            "tabs": tabs,
            "recent_downloads": session.downloads[-10:],
            "takeover_url": self._current_takeover_url(session),
            "remote_access": self._session_remote_access_info(session),
        }

    async def _light_snapshot(self, session: BrowserSession, *, label: str) -> dict[str, Any]:
        screenshot = await self._capture_screenshot(session, label)
        summary = await self._page_summary(session.page)
        return {
            "url": session.page.url,
            "title": summary["title"],
            "active_element": summary["active_element"],
            "text_excerpt": summary["text_excerpt"],
            "dom_outline": summary["dom_outline"],
            "accessibility_outline": summary["accessibility_outline"],
            "screenshot_path": screenshot["path"],
            "screenshot_url": screenshot["url"],
        }

    async def _capture_screenshot(self, session: BrowserSession, label: str) -> dict[str, str]:
        filename = f"{self._timestamp()}-{label}.png"
        path = session.artifact_dir / filename
        await session.page.screenshot(path=str(path), full_page=False)
        return {"path": str(path), "url": f"/artifacts/{session.id}/{filename}"}

    async def _page_summary(self, page: Page) -> dict[str, Any]:
        summary = await page.evaluate(PAGE_SUMMARY_SCRIPT, 2000)
        accessibility_outline = await self._accessibility_outline(page)
        return {
            "title": await page.title(),
            "active_element": await page.evaluate(ACTIVE_ELEMENT_SCRIPT),
            "text_excerpt": summary.get("text_excerpt", ""),
            "dom_outline": summary.get("dom_outline", {}),
            "accessibility_outline": accessibility_outline,
        }

    async def _accessibility_outline(self, page: Page) -> dict[str, Any]:
        accessibility = getattr(page, "accessibility", None)
        if accessibility is None or not hasattr(accessibility, "snapshot"):
            return {
                "available": False,
                "root_role": None,
                "root_name": None,
                "focused": None,
                "role_counts": {},
                "nodes": [],
            }

        try:
            snapshot = await accessibility.snapshot(interesting_only=True)
        except Exception as exc:
            logger.debug("failed to capture accessibility snapshot: %s", exc)
            return {
                "available": False,
                "root_role": None,
                "root_name": None,
                "focused": None,
                "role_counts": {},
                "nodes": [],
                "error": str(exc),
            }

        if not snapshot:
            return {
                "available": True,
                "root_role": None,
                "root_name": None,
                "focused": None,
                "role_counts": {},
                "nodes": [],
            }

        nodes: list[dict[str, Any]] = []
        role_counts: dict[str, int] = {}
        focused: dict[str, Any] | None = None

        def walk(node: dict[str, Any], depth: int) -> None:
            nonlocal focused
            if len(nodes) >= ACCESSIBILITY_NODE_LIMIT:
                return
            role = node.get("role")
            if isinstance(role, str) and role:
                role_counts[role] = role_counts.get(role, 0) + 1
            compact = {
                "role": role,
                "name": node.get("name"),
                "value": node.get("valueString") or node.get("value"),
                "description": node.get("description"),
                "focused": bool(node.get("focused")),
                "disabled": bool(node.get("disabled")),
                "selected": bool(node.get("selected")),
                "checked": node.get("checked"),
                "expanded": node.get("expanded"),
                "pressed": node.get("pressed"),
                "depth": depth,
            }
            nodes.append(compact)
            if compact["focused"] and focused is None:
                focused = compact
            for child in node.get("children") or []:
                if not isinstance(child, dict):
                    continue
                walk(child, depth + 1)
                if len(nodes) >= ACCESSIBILITY_NODE_LIMIT:
                    return

        walk(snapshot, 0)
        return {
            "available": True,
            "root_role": snapshot.get("role"),
            "root_name": snapshot.get("name"),
            "focused": focused,
            "role_counts": role_counts,
            "nodes": nodes,
        }

    def _session_auth_state_info(self, session: BrowserSession) -> dict[str, Any]:
        info = self.auth_state.inspect(session.last_auth_state_path)
        info["session_auth_root"] = str(session.auth_dir)
        info["profile_name"] = session.auth_profile_name
        return info

    async def get_auth_state_info(self, session_id: str) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if session is not None:
            return self._session_auth_state_info(session)
        record = await self.session_store.get(session_id)
        return record.auth_state

    async def list_audit_events(
        self,
        *,
        limit: int = 100,
        session_id: str | None = None,
        event_type: str | None = None,
        operator_id: str | None = None,
    ) -> list[dict[str, Any]]:
        events = await self.audit.list(
            limit=limit,
            session_id=session_id,
            event_type=event_type,
            operator_id=operator_id,
        )
        return [item.model_dump() for item in events]

    @staticmethod
    def _action_verification(
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
            "scroll_feed",
            "like_post",
            "follow_user",
            "social_like",
            "social_follow",
            "social_repost",
            "social_unfollow",
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
            verified = bool(
                {"active_element_changed", "text_excerpt_changed", "accessibility_focus_changed"} & set(signals)
            ) or target_seen_after is not None
        elif action_name in {"type", "select_option", "social_post", "social_comment", "social_dm", "search_page", "social_login"}:
            verified = bool({"active_element_changed", "text_excerpt_changed", "accessibility_focus_changed"} & set(signals))
        elif action_name in {"wait", "reload"}:
            verified = True
        elif action_name == "upload":
            verified = True

        return {
            "verified": verified,
            "signals": signals,
            "target_seen_after": target_seen_after,
        }

    async def _session_summary(
        self,
        session: BrowserSession,
        *,
        status: SessionStatus = "active",
        live: bool = True,
    ) -> dict[str, Any]:
        return {
            "id": session.id,
            "name": session.name,
            "created_at": session.created_at.isoformat(),
            "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "status": status,
            "live": live,
            "current_url": session.page.url,
            "title": await session.page.title(),
            "artifact_dir": str(session.artifact_dir),
            "takeover_url": self._current_takeover_url(session),
            "remote_access": self._session_remote_access_info(session),
            "isolation": self._session_isolation(session),
            "auth_state": self._session_auth_state_info(session),
            "downloads": session.downloads[-20:],
            "last_action": session.last_action,
            "trace_path": str(session.trace_path),
        }

    async def get_session_summary(self, session_id: str) -> dict[str, Any]:
        """Public API for getting a session summary by ID."""
        session = await self.get_session(session_id)
        return await self._session_summary(session)

    async def _persist_session(self, session: BrowserSession, *, status: SessionStatus) -> None:
        summary = await self._session_summary(
            session,
            status=status,
            live=status == "active",
        )
        await self.session_store.upsert(SessionRecord.model_validate(summary))

    def _tab_pages(self, session: BrowserSession) -> list[Page]:
        pages = getattr(session.context, "pages", None)
        if callable(pages):
            pages = pages()
        if isinstance(pages, list) and pages:
            return pages
        return [session.page]

    async def _tab_summaries(self, session: BrowserSession) -> list[dict[str, Any]]:
        tabs: list[dict[str, Any]] = []
        for index, page in enumerate(self._tab_pages(session)):
            self._attach_page_listeners(page, session)
            try:
                title = await page.title()
            except Exception:
                title = ""
            tabs.append(
                {
                    "index": index,
                    "active": page is session.page,
                    "url": getattr(page, "url", ""),
                    "title": title,
                }
            )
        return tabs

    async def _settle(self, page: Page) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=min(self.settings.action_timeout_ms, 5000))
        except Exception:
            pass
        await page.wait_for_timeout(250)

    def _assert_runtime_url_allowed(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme in {"about", "data", "blob", ""}:
            return
        self._assert_url_allowed(url)

    @staticmethod
    def _session_auth_root_for(base_root: str, session_id: str) -> Path:
        return Path(base_root).resolve() / session_id

    @staticmethod
    def _session_upload_root_for(base_root: str, session_id: str) -> Path:
        return Path(base_root).resolve() / session_id

    def _session_auth_root(self, session_id: str) -> Path:
        return self._session_auth_root_for(self.settings.auth_root, session_id)

    def _session_upload_root(self, session_id: str) -> Path:
        return self._session_upload_root_for(self.settings.upload_root, session_id)

    def _auth_profile_root(self) -> Path:
        root = Path(self.settings.auth_root).resolve() / "profiles"
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _normalize_auth_profile_name(profile_name: str) -> str:
        normalized = profile_name.strip()
        if not normalized:
            raise ValueError("auth profile name is required")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", normalized):
            raise ValueError("auth profile names may contain letters, numbers, dots, underscores, and hyphens")
        return normalized

    def _auth_profile_dir(self, profile_name: str, *, create: bool) -> Path:
        normalized = self._normalize_auth_profile_name(profile_name)
        root = self._auth_profile_root()
        directory = (root / normalized).resolve()
        if root not in directory.parents and directory != root:
            raise PermissionError("auth profile path must stay inside auth root")
        if create:
            directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _auth_profile_metadata_path(self, profile_name: str, *, create: bool) -> Path:
        return self._auth_profile_dir(profile_name, create=create) / "profile.json"

    def _auth_profile_state_base_path(self, profile_name: str, *, create: bool) -> Path:
        return self._auth_profile_dir(profile_name, create=create) / "state.json"

    def _resolve_auth_profile_state_path(self, profile_name: str, *, must_exist: bool) -> Path:
        base_path = self._auth_profile_state_base_path(profile_name, create=not must_exist)
        candidates = [base_path.with_name(f"{base_path.name}.enc"), base_path]
        existing = [candidate for candidate in candidates if candidate.exists()]
        if existing:
            existing.sort(key=lambda candidate: candidate.stat().st_mtime, reverse=True)
            return existing[0]
        if must_exist:
            raise FileNotFoundError(base_path)
        return base_path

    def _read_auth_profile_metadata(self, profile_name: str) -> dict[str, Any]:
        metadata_path = self._auth_profile_metadata_path(profile_name, create=False)
        if not metadata_path.exists():
            return {}
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _session_isolation(self, session: BrowserSession) -> dict[str, Any]:
        payload = {
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
        return payload

    async def _approval_observation(self, session: BrowserSession) -> dict[str, Any]:
        return {
            "url": session.page.url,
            "title": await session.page.title(),
            "takeover_url": self._current_takeover_url(session),
            "remote_access": self._session_remote_access_info(session),
            "isolation": self._session_isolation(session),
            "auth_state": self._session_auth_state_info(session),
            "last_action": session.last_action,
        }

    def _approval_kind_for_decision(self, decision: BrowserActionDecision) -> ApprovalKind | None:
        if decision.action == "upload":
            return "upload" if self.settings.require_approval_for_uploads else None
        if decision.risk_category in {"post", "payment", "account_change", "destructive"}:
            return decision.risk_category
        return None

    @staticmethod
    def _action_class(action_name: str) -> str:
        if action_name in {
            "navigate",
            "hover",
            "scroll",
            "scroll_feed",
            "wait",
            "reload",
            "go_back",
            "go_forward",
            "search_page",
        }:
            return "read"
        return "write"

    def _assert_url_allowed(self, url: str) -> None:
        host = urlparse(url).hostname
        if not host:
            raise PermissionError(f"Could not determine hostname for URL: {url}")
        patterns = self.settings.allowed_host_patterns
        if not patterns or patterns == ["*"]:
            return
        for pattern in patterns:
            normalized = pattern.removeprefix("*.")
            if fnmatch.fnmatch(host, pattern) or host == normalized or host.endswith(f".{normalized}"):
                return
        raise PermissionError(f"Host {host!r} is not allowlisted")

    def _resolve_target(
        self,
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

    def _safe_upload_path(self, file_path: str, *, session: BrowserSession | None = None) -> Path:
        root = Path(self.settings.upload_root).resolve()
        raw_path = Path(file_path)
        if raw_path.is_absolute():
            candidate = raw_path.resolve()
            allowed_roots = [root]
            if session is not None:
                allowed_roots.append(session.upload_dir.resolve())
        else:
            allowed_roots = [root]
            preferred_roots: list[Path] = []
            if session is not None:
                preferred_roots.append(session.upload_dir.resolve())
                allowed_roots.append(session.upload_dir.resolve())
            preferred_roots.append(root)

            for candidate_root in preferred_roots:
                candidate = (candidate_root / file_path).resolve()
                if candidate.exists():
                    break
            else:
                candidate = (preferred_roots[0] / file_path).resolve()

        if not any(candidate == allowed_root or allowed_root in candidate.parents for allowed_root in allowed_roots):
            raise PermissionError("file_path must stay inside upload root")
        if not candidate.exists():
            raise FileNotFoundError(candidate)
        return candidate

    def _safe_session_auth_path(
        self,
        session: BrowserSession,
        relative_path: str,
        *,
        must_exist: bool = False,
    ) -> Path:
        root = session.auth_dir.resolve()
        candidate = (root / relative_path).resolve()
        if root not in candidate.parents and candidate != root:
            raise PermissionError("auth path must stay inside the session auth root")
        candidate.parent.mkdir(parents=True, exist_ok=True)
        if must_exist and not candidate.exists():
            raise FileNotFoundError(candidate)
        return candidate

    def _safe_auth_path(self, relative_path: str, must_exist: bool = False) -> Path:
        root = Path(self.settings.auth_root).resolve()
        candidate = (root / relative_path).resolve()
        if root not in candidate.parents and candidate != root:
            raise PermissionError("auth path must stay inside auth root")
        candidate.parent.mkdir(parents=True, exist_ok=True)
        if must_exist and not candidate.exists():
            raise FileNotFoundError(candidate)
        return candidate

    def _attach_page_listeners(self, page: Page, session: BrowserSession) -> None:
        if not hasattr(page, "on"):
            return
        page_id = id(page)
        if page_id in session.attached_pages:
            return
        session.attached_pages.add(page_id)

        page.on("console", lambda message: self._bounded_append(
            session.console_messages,
            {
                "type": message.type,
                "text": message.text,
                "location": message.location,
            },
        ))
        page.on("pageerror", lambda error: self._bounded_append(session.page_errors, str(error)))
        page.on("requestfailed", lambda request: self._bounded_append(
            session.request_failures,
            {
                "url": request.url,
                "method": request.method,
                "failure": str(request.failure) if request.failure else None,
            },
        ))
        page.on("download", lambda download: asyncio.create_task(self._handle_download(session, download)))

    def _bounded_append(self, items: list[Any], value: Any, limit: int = 50) -> None:
        items.append(value)
        if len(items) > limit:
            del items[: len(items) - limit]

    async def _handle_download(self, session: BrowserSession, download: Any) -> None:
        suggested = Path(str(getattr(download, "suggested_filename", "") or f"download-{uuid4().hex}")).name
        destination = session.artifact_dir / "downloads" / suggested
        if destination.exists():
            destination = destination.with_name(f"{destination.stem}-{uuid4().hex[:8]}{destination.suffix}")

        failure: str | None = None
        status = "completed"
        try:
            await download.save_as(str(destination))
            if hasattr(download, "failure"):
                failure = await download.failure()
        except Exception as exc:
            failure = str(exc)
            status = "failed"

        if failure:
            status = "failed"

        record = {
            "id": uuid4().hex[:12],
            "timestamp": self._timestamp(),
            "status": status,
            "filename": destination.name,
            "suggested_filename": suggested,
            "path": str(destination),
            "url": f"/artifacts/{session.id}/downloads/{destination.name}",
            "source_url": getattr(download, "url", None),
            "failure": failure,
        }
        self._bounded_append(session.downloads, record, limit=100)
        await self._append_jsonl(session.artifact_dir / "downloads.jsonl", record)
        await self.audit.append(
            event_type="download_captured",
            status=status,
            action="download",
            session_id=session.id,
            details={"filename": record["filename"], "url": record["url"], "failure": failure},
        )
        if session.id in self.sessions:
            try:
                await self._persist_session(session, status="active")
            except Exception as exc:
                logger.warning("failed to persist download metadata for session %s: %s", session.id, exc)

    async def _append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False)
        await asyncio.to_thread(self._append_text, path, line + "\n")

    @staticmethod
    def _append_text(path: Path, text: str) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
