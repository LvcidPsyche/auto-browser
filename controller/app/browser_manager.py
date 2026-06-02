from __future__ import annotations

import asyncio
import fnmatch
import inspect
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse
from uuid import uuid4

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

from .actions import BrowserActionPipeline
from .approvals import ApprovalStore
from .artifacts import SessionArtifactService
from .audit import AuditStore
from .auth_state import AuthStateManager
from .browser.services import (
    BrowserActionService,
    BrowserApprovalService,
    BrowserAuthProfileService,
    BrowserBotChallengeService,
    BrowserDiagnosticsService,
    BrowserObservationService,
    BrowserRemoteAccessService,
    BrowserRuntimeService,
    BrowserSessionService,
    BrowserTabService,
    BrowserTakeoverService,
    BrowserUploadService,
    BrowserWitnessService,
)
from .browser_scripts import apply_stealth
from .config import Settings
from .downloads import DownloadCaptureService
from .memory_manager import MemoryManager
from .models import (
    ApprovalKind,
    BrowserActionDecision,
    SessionStatus,
    WitnessRemoteState,
)
from .network_inspector import NetworkInspector
from .ocr import OCRExtractor
from .pii_scrub import PiiScrubber
from .session_isolation import DockerBrowserNodeProvisioner, IsolatedBrowserRuntime
from .session_store import DurableSessionStore
from .session_tunnel import IsolatedSessionTunnel, IsolatedSessionTunnelBroker
from .utils import UTC
from .witness import (
    WitnessActionContext,
    WitnessApproval,
    WitnessPolicyEngine,
    WitnessPolicyOutcome,
    WitnessRecorder,
    WitnessRemoteClient,
    WitnessSessionContext,
)

logger = logging.getLogger(__name__)

__all__ = ["BrowserManager", "BrowserSession", "PlaywrightError"]


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
    trace_recording: bool = False
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
    proxy_persona: str | None = None
    last_auth_state_path: Path | None = None
    auth_profile_name: str | None = None
    tunnel_error: str | None = None
    mouse_position: tuple[float, float] | None = None
    totp_secret: str | None = None
    network_inspector: NetworkInspector | None = None
    # Headless/headed state — set to False to request headed mode on next fork
    headless: bool = True
    protection_mode: str = "normal"
    pending_witness_context: dict[str, Any] | None = None
    witness_remote_state: WitnessRemoteState = field(default_factory=WitnessRemoteState)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __getattribute__(self, name: str) -> Any:
        val = super().__getattribute__(name)
        if name == "page":
            try:
                is_closed = False
                if val is not None:
                    if hasattr(val, "is_closed") and callable(val.is_closed):
                        is_closed = val.is_closed()
                    else:
                        is_closed = getattr(val, "closed", False)
                
                if is_closed:
                    context = super().__getattribute__("context")
                    pages = context.pages
                    if pages:
                        for p in reversed(pages):
                            p_closed = False
                            if hasattr(p, "is_closed") and callable(p.is_closed):
                                p_closed = p.is_closed()
                            else:
                                p_closed = getattr(p, "closed", False)
                            
                            if not p_closed:
                                self.__dict__["page"] = p
                                return p
            except Exception:
                pass
        return val


SessionCreatedHook = Callable[[str, Page], Awaitable[None]]
SessionClosedHook = Callable[[str], Awaitable[None]]


class BrowserManager:
    def __init__(self, settings: Settings, *, proxy_store: Any | None = None):
        self.settings = settings
        self.proxy_store = proxy_store
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.sessions: dict[str, BrowserSession] = {}
        self._browser_lock = asyncio.Lock()
        self.action_pipeline = BrowserActionPipeline()
        self.actions = BrowserActionService(self)
        self.approval_service = BrowserApprovalService(self)
        self.auth_profiles = BrowserAuthProfileService(self)
        self.bot_challenge = BrowserBotChallengeService()
        self.tabs = BrowserTabService(self)
        self.uploads = BrowserUploadService(self)
        self.observation = BrowserObservationService(self)
        self.session_lifecycle = BrowserSessionService(self)
        self.runtime = BrowserRuntimeService(self)
        self.witness_bridge = BrowserWitnessService(self)
        self.remote_access = BrowserRemoteAccessService(self)
        self.takeover = BrowserTakeoverService(self)

        Path(self.settings.artifact_root).mkdir(parents=True, exist_ok=True)
        Path(self.settings.upload_root).mkdir(parents=True, exist_ok=True)
        Path(self.settings.auth_root).mkdir(parents=True, exist_ok=True)
        Path(self.settings.approval_root).mkdir(parents=True, exist_ok=True)
        Path(self.settings.audit_root).mkdir(parents=True, exist_ok=True)
        witness_root = Path(self.settings.witness_root)
        try:
            witness_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            witness_root = Path(self.settings.audit_root).resolve().parent / "witness"
            witness_root.mkdir(parents=True, exist_ok=True)
            self.settings.witness_root = str(witness_root)
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
        self.artifacts = SessionArtifactService(self.settings.artifact_root)
        self.download_capture = DownloadCaptureService(self.artifacts)
        self.session_store = DurableSessionStore(
            file_root=self.settings.session_store_root,
            redis_url=self.settings.redis_url,
            redis_prefix=self.settings.session_store_redis_prefix,
        )
        self.memory = MemoryManager(settings.memory_root) if settings.memory_enabled else None
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
        self.pii_scrubber = PiiScrubber.from_settings(self.settings)
        self.diagnostics = BrowserDiagnosticsService(
            self,
            self.pii_scrubber,
            self.download_capture,
        )
        self.witness = WitnessRecorder(self.settings.witness_root)
        self.witness_remote = WitnessRemoteClient(
            base_url=self.settings.witness_remote_url,
            api_key=self.settings.witness_remote_api_key,
            tenant_id=self.settings.witness_remote_tenant_id,
            timeout_seconds=self.settings.witness_remote_timeout_seconds,
            verify_tls=self.settings.witness_remote_verify_tls,
        )
        self.witness_policy = WitnessPolicyEngine()
        self.runtime_provisioner = DockerBrowserNodeProvisioner(self.settings)
        self.tunnel_broker = IsolatedSessionTunnelBroker(self.settings)
        self._session_created_hook: SessionCreatedHook | None = None
        self._session_closed_hook: SessionClosedHook | None = None

    def register_extension_hooks(
        self,
        *,
        session_created: SessionCreatedHook | None = None,
        session_closed: SessionClosedHook | None = None,
    ) -> None:
        self._session_created_hook = session_created
        self._session_closed_hook = session_closed

    def get_remote_access_info(self, session_id: str | None = None) -> dict[str, Any]:
        return self.remote_access.get_info(session_id)

    def _global_remote_access_info(self) -> dict[str, Any]:
        return self.remote_access.global_info()

    def _session_remote_access_info(self, session: BrowserSession) -> dict[str, Any]:
        return self.remote_access.session_info(session)

    def _current_takeover_url(self, session: BrowserSession | None = None) -> str:
        return self.remote_access.current_takeover_url(session)

    @staticmethod
    def _parse_remote_access_timestamp(value: Any) -> datetime | None:
        return BrowserRemoteAccessService.parse_timestamp(value)

    @staticmethod
    def _takeover_url_is_local_only(value: str) -> bool:
        return BrowserRemoteAccessService.takeover_url_is_local_only(value)

    async def startup(self) -> None:
        logger.info("starting browser manager")
        await self.approvals.startup()
        await self.audit.startup()
        await self.witness.startup()
        if self.settings.witness_enabled:
            await self.witness_remote.startup()
        await self.session_store.startup()
        await self.session_store.mark_all_active_interrupted()
        if self.memory is not None:
            await self.memory.startup()
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
        await self.witness_remote.shutdown()
        await self.session_store.shutdown()

    async def ensure_browser(self) -> Browser:
        return await self.runtime.ensure_browser()

    async def cdp_attach(self, cdp_url: str) -> dict[str, Any]:
        return await self.runtime.cdp_attach(cdp_url)

    async def _connect_browser(self, ws_target_factory, *, failure_context: str) -> Browser:
        return await self.runtime.connect_browser(ws_target_factory, failure_context=failure_context)

    async def _resolve_browser_ws_endpoint(self) -> str:
        return await self.runtime.resolve_browser_ws_endpoint()

    async def _acquire_session_browser(self, session_id: str) -> tuple[Browser, IsolatedBrowserRuntime | None]:
        return await self.runtime.acquire_session_browser(session_id)

    async def list_sessions(self) -> list[dict[str, Any]]:
        return await self.session_lifecycle.list()

    async def create_session(
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
        return await self.session_lifecycle.create(
            name=name,
            start_url=start_url,
            storage_state_path=storage_state_path,
            auth_profile=auth_profile,
            memory_profile=memory_profile,
            proxy_persona=proxy_persona,
            request_proxy_server=request_proxy_server,
            request_proxy_username=request_proxy_username,
            request_proxy_password=request_proxy_password,
            user_agent=user_agent,
            protection_mode=protection_mode,
            totp_secret=totp_secret,
        )

    def _check_session_limit(self) -> None:
        self.session_lifecycle.check_limit()

    def _prepare_session_dirs(self, session_id: str) -> tuple[Path, Path, Path]:
        return self.session_lifecycle.prepare_dirs(session_id)

    def _build_context_kwargs(
        self,
        user_agent: str | None,
        proxy_server: str | None,
        proxy_username: str | None,
        proxy_password: str | None,
    ) -> dict[str, Any]:
        return self.session_lifecycle.build_context_kwargs(
            user_agent,
            proxy_server,
            proxy_username,
            proxy_password,
        )

    async def _cleanup_failed_session(
        self,
        session_id: str,
        *,
        session: "BrowserSession | None",
        context: "BrowserContext | None",
        browser: "Browser | None",
        runtime: "IsolatedBrowserRuntime | None",
    ) -> None:
        await self.session_lifecycle.cleanup_failed(
            session_id,
            session=session,
            context=context,
            browser=browser,
            runtime=runtime,
        )

    async def get_session(self, session_id: str) -> BrowserSession:
        return await self.session_lifecycle.get(session_id)

    async def get_session_record(self, session_id: str) -> dict[str, Any]:
        return await self.session_lifecycle.get_record(session_id)

    async def list_approvals(
        self,
        *,
        status: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self.approval_service.list(status=status, session_id=session_id)

    async def get_approval(self, approval_id: str) -> dict[str, Any]:
        return await self.approval_service.get(approval_id)

    async def approve(self, approval_id: str, comment: str | None = None) -> dict[str, Any]:
        return await self.approval_service.approve(approval_id, comment=comment)

    async def reject(self, approval_id: str, comment: str | None = None) -> dict[str, Any]:
        return await self.approval_service.reject(approval_id, comment=comment)

    async def execute_approval(self, approval_id: str) -> dict[str, Any]:
        return await self.approval_service.execute(approval_id)

    async def observe(self, session_id: str, limit: int = 40, preset: str = "normal") -> dict[str, Any]:
        return await self.observation.observe(session_id, limit=limit, preset=preset)

    async def capture_screenshot(self, session_id: str, *, label: str = "manual") -> dict[str, Any]:
        return await self.observation.capture_screenshot(session_id, label=label)

    async def get_console_messages(self, session_id: str, *, limit: int = 20) -> dict[str, Any]:
        return await self.diagnostics.get_console_messages(session_id, limit=limit)

    async def get_network_log(
        self,
        session_id: str,
        *,
        limit: int = 100,
        method: str | None = None,
        url_contains: str | None = None,
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        async with session.lock:
            inspector = session.network_inspector
            if inspector is None:
                return {
                    "session": await self._session_summary(session),
                    "enabled": False,
                    "entries": [],
                    "summary": {},
                }
            return {
                "session": await self._session_summary(session),
                "enabled": True,
                "entries": inspector.entries(limit=limit, method=method, url_contains=url_contains),
                "summary": inspector.summary(),
            }

    async def fork_session(
        self,
        session_id: str,
        *,
        name: str | None = None,
        start_url: str | None = None,
    ) -> dict[str, Any]:
        """Fork a session: clone cookies + localStorage state into a new session."""
        session = await self.get_session(session_id)
        async with session.lock:
            # Export cookies and storage state to a temp file
            fork_auth_path = session.auth_dir / f"fork_{uuid4().hex[:8]}.json"
            await session.context.storage_state(path=str(fork_auth_path))
            current_url = session.page.url

        # Create the new session using the forked state
        forked = await self.create_session(
            name=name or f"fork-of-{session.name}",
            start_url=start_url or current_url,
            storage_state_path=str(fork_auth_path),
        )
        forked["forked_from"] = session_id
        await self.audit.append(
            event_type="session_forked",
            status="ok",
            action="fork_session",
            session_id=session_id,
            details={"new_session_id": forked["id"], "start_url": start_url or current_url},
        )
        return forked

    def get_pii_scrubber_status(self) -> dict[str, Any]:
        """Return current PII scrubber configuration."""
        return self.pii_scrubber.summary()

    async def enable_shadow_browse(self, session_id: str) -> dict[str, Any]:
        """Switch a session to headed (visible) mode for debugging.

        Because Playwright cannot flip headless→headed mid-session, this:
        1. Exports state (cookies + storage) from the running session
        2. Launches a new LOCAL headed Chromium process
        3. Creates a new BrowserSession with that state and the same URL
        4. Returns the new session's info (the old session keeps running)

        The caller is expected to close the original session when done debugging.
        """
        if not self.settings.shadow_browse_enabled:
            raise RuntimeError("Shadow browsing is disabled (SHADOW_BROWSE_ENABLED=false)")
        if self.playwright is None:
            raise RuntimeError("Playwright not started")

        session = await self.get_session(session_id)
        async with session.lock:
            current_url = session.page.url
            shadow_auth_path = session.auth_dir / f"shadow_{uuid4().hex[:8]}.json"
            await session.context.storage_state(path=str(shadow_auth_path))

        # Launch a local headed browser process
        headed_browser = await self.playwright.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )

        shadow_session_id = uuid4().hex[:12]
        artifact_dir = Path(self.settings.artifact_root) / shadow_session_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        auth_dir = self._session_auth_root(shadow_session_id)
        upload_dir = self._session_upload_root(shadow_session_id)
        auth_dir.mkdir(parents=True, exist_ok=True)
        upload_dir.mkdir(parents=True, exist_ok=True)

        context_kwargs: dict[str, Any] = {
            "viewport": {
                "width": self.settings.default_viewport_width,
                "height": self.settings.default_viewport_height,
            },
            "accept_downloads": True,
            "storage_state": str(shadow_auth_path),
        }
        context = await headed_browser.new_context(**context_kwargs)
        page = await context.new_page()
        page.set_default_timeout(self.settings.action_timeout_ms)
        if self.settings.stealth_enabled:
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
            takeover_url=self.settings.takeover_url,
            trace_path=artifact_dir / "trace.zip",
            browser=headed_browser,
            headless=False,
        )
        self._attach_page_listeners(page, shadow_session)
        self.sessions[shadow_session_id] = shadow_session

        await page.goto(current_url, wait_until="domcontentloaded")
        await self._settle(page)
        await self._persist_session(shadow_session, status="active")
        await self.audit.append(
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

    async def get_page_errors(self, session_id: str, *, limit: int = 20) -> dict[str, Any]:
        return await self.diagnostics.get_page_errors(session_id, limit=limit)

    async def get_request_failures(self, session_id: str, *, limit: int = 20) -> dict[str, Any]:
        return await self.diagnostics.get_request_failures(session_id, limit=limit)

    async def stop_trace(self, session_id: str) -> dict[str, Any]:
        return await self.observation.stop_trace(session_id)

    async def navigate(self, session_id: str, url: str) -> dict[str, Any]:
        return await self.actions.navigate(session_id, url)

    async def click(
        self,
        session_id: str,
        *,
        selector: str | None = None,
        element_id: str | None = None,
        x: float | None = None,
        y: float | None = None,
    ) -> dict[str, Any]:
        return await self.actions.click(session_id, selector=selector, element_id=element_id, x=x, y=y)

    async def hover(
        self,
        session_id: str,
        *,
        selector: str | None = None,
        element_id: str | None = None,
        x: float | None = None,
        y: float | None = None,
    ) -> dict[str, Any]:
        return await self.actions.hover(session_id, selector=selector, element_id=element_id, x=x, y=y)

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
        return await self.actions.select_option(
            session_id,
            selector=selector,
            element_id=element_id,
            value=value,
            label=label,
            index=index,
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
        return await self.actions.type(
            session_id,
            text=text,
            selector=selector,
            element_id=element_id,
            clear_first=clear_first,
            sensitive=sensitive,
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
        return BrowserActionService.text_target_payload(
            target,
            text,
            clear_first=clear_first,
            sensitive=sensitive,
            preview_chars=preview_chars,
        )

    async def _locator_is_sensitive_input(self, locator: Any) -> bool:
        return await self.actions.locator_is_sensitive_input(locator)

    async def press(self, session_id: str, key: str) -> dict[str, Any]:
        return await self.actions.press(session_id, key)

    async def scroll(self, session_id: str, delta_x: float, delta_y: float) -> dict[str, Any]:
        return await self.actions.scroll(session_id, delta_x, delta_y)

    async def wait(self, session_id: str, wait_ms: int) -> dict[str, Any]:
        return await self.actions.wait(session_id, wait_ms)

    async def reload(self, session_id: str) -> dict[str, Any]:
        return await self.actions.reload(session_id)

    async def go_back(self, session_id: str) -> dict[str, Any]:
        return await self.actions.go_back(session_id)

    async def go_forward(self, session_id: str) -> dict[str, Any]:
        return await self.actions.go_forward(session_id)

    async def list_tabs(self, session_id: str) -> list[dict[str, Any]]:
        return await self.tabs.list(session_id)

    async def open_tab(self, session_id: str, url: str | None, activate: bool) -> dict[str, Any]:
        return await self.tabs.open(session_id, url, activate)

    async def activate_tab(self, session_id: str, index: int) -> dict[str, Any]:
        return await self.tabs.activate(session_id, index)

    async def close_tab(self, session_id: str, index: int) -> dict[str, Any]:
        return await self.tabs.close(session_id, index)

    async def list_downloads(self, session_id: str) -> list[dict[str, Any]]:
        return await self.diagnostics.list_downloads(session_id)

    async def _locator_center(self, locator: Any) -> tuple[float, float] | None:
        return await self.actions.locator_center(locator)

    async def _move_mouse_human_like(self, session: BrowserSession, x: float, y: float) -> None:
        await self.actions.move_mouse_human_like(session, x, y)

    async def _click_human_like(self, session: BrowserSession, x: float, y: float) -> None:
        await self.actions.click_human_like(session, x, y)

    async def _focus_locator(self, session: BrowserSession, locator: Any) -> None:
        await self.actions.focus_locator(session, locator)

    async def _type_text_human_like(self, page: Page, text: str) -> None:
        await self.actions.type_text_human_like(page, text)

    async def _first_visible_locator(self, page: Page, selectors: list[str]) -> tuple[Any, str] | None:
        return await self.actions.first_visible_locator(page, selectors)

    async def _maybe_handle_totp(self, session: BrowserSession) -> dict[str, Any] | None:
        return await self.actions.maybe_handle_totp(session)

    @staticmethod
    def _host_matches(host: str, *domains: str) -> bool:
        host = host.lower().rstrip(".")
        for domain in domains:
            domain = domain.lower().rstrip(".")
            if host == domain or host.endswith("." + domain):
                return True
        return False

    def _current_platform(self, session: BrowserSession) -> str | None:
        host = (urlparse(session.page.url).hostname or "").lower()
        if self._host_matches(host, "x.com", "twitter.com"):
            return "x"
        if self._host_matches(host, "instagram.com"):
            return "instagram"
        if self._host_matches(host, "linkedin.com"):
            return "linkedin"
        if self._host_matches(host, "outlook.live.com", "outlook.office.com", "outlook.office365.com"):
            return "outlook"
        return None

    async def execute_decision(
        self,
        session_id: str,
        decision: BrowserActionDecision,
        *,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        return await self.actions.execute_decision(session_id, decision, approval_id=approval_id)

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
        return await self.uploads.upload(
            session_id,
            file_path=file_path,
            approved=approved,
            approval_id=approval_id,
            selector=selector,
            element_id=element_id,
        )

    async def save_storage_state(self, session_id: str, path: str) -> dict[str, Any]:
        return await self.auth_profiles.save_storage_state(session_id, path)

    async def _save_auth_profile_for_session(
        self,
        session: BrowserSession,
        profile_name: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.auth_profiles.save_for_session(session, profile_name, metadata=metadata)

    async def save_auth_profile(self, session_id: str, profile_name: str) -> dict[str, Any]:
        return await self.auth_profiles.save(session_id, profile_name)

    async def get_auth_profile(self, profile_name: str) -> dict[str, Any]:
        return await self.auth_profiles.get(profile_name)

    async def list_auth_profiles(self) -> list[dict[str, Any]]:
        return await self.auth_profiles.list()

    async def request_human_takeover(self, session_id: str, reason: str) -> dict[str, Any]:
        return await self.takeover.request(session_id, reason)

    async def _require_decision_approval(
        self,
        session_id: str,
        decision: BrowserActionDecision,
        *,
        approval_id: str | None,
        fallback_reason: str | None = None,
        approval_kind: ApprovalKind | None = None,
    ):
        return await self.actions.require_decision_approval(
            session_id,
            decision,
            approval_id=approval_id,
            fallback_reason=fallback_reason,
            approval_kind=approval_kind,
        )

    async def require_governed_approval(
        self,
        session_id: str,
        decision: BrowserActionDecision,
        *,
        approval_id: str | None,
    ):
        return await self.actions.require_governed_approval(session_id, decision, approval_id=approval_id)

    async def close_session(self, session_id: str) -> dict[str, Any]:
        return await self.session_lifecycle.close(session_id)

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
            session.tunnel_error = "isolated tunnel provisioning failed"
            logger.warning("failed to provision isolated tunnel for session %s: %s", session.id, exc)

    async def _run_action(
        self,
        session: BrowserSession,
        action_name: str,
        target: dict[str, Any],
        operation,
    ) -> dict[str, Any]:
        return await self.actions.run_action(session, action_name, target, operation)


    async def _check_bot_challenge(self, session: BrowserSession) -> dict[str, Any] | None:
        return await self.bot_challenge.check(session)

    async def _observation_payload(
        self,
        session: BrowserSession,
        *,
        limit: int = 40,
        screenshot_label: str = "observe",
        preset: str = "normal",
    ) -> dict[str, Any]:
        return await self.observation.observation_payload(
            session,
            limit=limit,
            screenshot_label=screenshot_label,
            preset=preset,
        )

    async def _light_snapshot(self, session: BrowserSession, *, label: str) -> dict[str, Any]:
        return await self.observation.light_snapshot(session, label=label)

    async def _capture_screenshot(self, session: BrowserSession, label: str) -> dict[str, str]:
        return await self.observation.capture_session_screenshot(session, label)

    def _trace_payload(self, session: BrowserSession) -> dict[str, Any]:
        return self.observation.trace_payload(session)

    async def _stop_trace_recording(self, session: BrowserSession) -> None:
        await self.observation.stop_trace_recording(session)

    async def _page_summary(self, page: Page, text_limit: int = 2000) -> dict[str, Any]:
        return await self.observation.page_summary(page, text_limit=text_limit)

    async def _accessibility_outline(self, page: Page) -> dict[str, Any]:
        return await self.observation.accessibility_outline(page)

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

    async def list_witness_receipts(self, session_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        receipts = await self.witness.list(session_id, limit=limit)
        return [item.model_dump() for item in receipts]

    def _initial_witness_remote_state(self, protection_mode: str) -> WitnessRemoteState:
        return self.witness_bridge.initial_remote_state(protection_mode)

    def _witness_remote_required_for_profile(self, protection_mode: str) -> bool:
        return self.witness_bridge.remote_required_for_profile(protection_mode)

    async def _ensure_witness_remote_ready(self, session: BrowserSession, *, action: str) -> None:
        await self.witness_bridge.ensure_remote_ready(session, action=action)

    def _auth_material_encryption_ready(self) -> bool:
        return self.witness_bridge.auth_material_encryption_ready()

    def _witness_session_context(self, session: BrowserSession) -> WitnessSessionContext:
        return self.witness_bridge.session_context(session)

    async def _record_witness_receipt(
        self,
        session: BrowserSession,
        *,
        event_type: str,
        status: str,
        action: str,
        action_class: str,
        risk_category: str | None = None,
        target: dict[str, Any] | None = None,
        outcome: WitnessPolicyOutcome | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        verification: dict[str, Any] | None = None,
        approval: WitnessApproval | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self.witness_bridge.record_receipt(
            session,
            event_type=event_type,
            status=status,
            action=action,
            action_class=action_class,
            risk_category=risk_category,
            target=target,
            outcome=outcome,
            before=before,
            after=after,
            verification=verification,
            approval=approval,
            metadata=metadata,
        )

    async def _record_session_witness_receipt(
        self,
        session: BrowserSession,
        *,
        action: str,
        status: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self.witness_bridge.record_session_receipt(
            session,
            action=action,
            status=status,
            metadata=metadata,
        )

    def _witness_action_class(self, action_name: str, *, risk_category: str | None = None) -> str:
        return BrowserWitnessService.action_class(action_name, risk_category=risk_category)

    def _consume_witness_context(self, session: BrowserSession) -> dict[str, Any]:
        return BrowserWitnessService.consume_context(session)

    def _build_witness_action_context(
        self,
        *,
        action_name: str,
        target: dict[str, Any],
        witness_context: dict[str, Any],
    ) -> WitnessActionContext:
        return self.witness_bridge.build_action_context(
            action_name=action_name,
            target=target,
            witness_context=witness_context,
        )

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
        elif action_name in {"type", "select_option"}:
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
        return await self.session_lifecycle.summary(session, status=status, live=live)

    async def get_session_summary(self, session_id: str) -> dict[str, Any]:
        """Public API for getting a session summary by ID."""
        return await self.session_lifecycle.get_summary(session_id)

    async def _persist_session(self, session: BrowserSession, *, status: SessionStatus) -> None:
        await self.session_lifecycle.persist(session, status=status)

    def _tab_pages(self, session: BrowserSession) -> list[Page]:
        return self.tabs.pages(session)

    async def _tab_summaries(self, session: BrowserSession) -> list[dict[str, Any]]:
        return await self.tabs.summaries(session)

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
        return BrowserSessionService.auth_root_for(base_root, session_id)

    @staticmethod
    def _session_upload_root_for(base_root: str, session_id: str) -> Path:
        return BrowserSessionService.upload_root_for(base_root, session_id)

    def _session_auth_root(self, session_id: str) -> Path:
        return self.session_lifecycle.auth_root(session_id)

    def _session_upload_root(self, session_id: str) -> Path:
        return self.session_lifecycle.upload_root(session_id)

    def _auth_profile_root(self) -> Path:
        return self.auth_profiles.root()

    @staticmethod
    def _resolve_contained_path(root: Path, candidate_path: str | Path, *, allow_absolute: bool = False) -> Path:
        return BrowserAuthProfileService.resolve_contained_path(
            root,
            candidate_path,
            allow_absolute=allow_absolute,
        )

    @staticmethod
    def _normalize_auth_profile_name(profile_name: str) -> str:
        return BrowserAuthProfileService.normalize_name(profile_name)

    def _auth_profile_dir(self, profile_name: str, *, create: bool) -> Path:
        return self.auth_profiles.dir(profile_name, create=create)

    def _auth_profile_metadata_path(self, profile_name: str, *, create: bool) -> Path:
        return self.auth_profiles.metadata_path(profile_name, create=create)

    def _auth_profile_state_base_path(self, profile_name: str, *, create: bool) -> Path:
        return self.auth_profiles.state_base_path(profile_name, create=create)

    def _resolve_auth_profile_state_path(self, profile_name: str, *, must_exist: bool) -> Path:
        return self.auth_profiles.resolve_state_path(profile_name, must_exist=must_exist)

    def _read_auth_profile_metadata(self, profile_name: str) -> dict[str, Any]:
        return self.auth_profiles.read_metadata(profile_name)

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
        return self.actions.approval_kind_for_decision(decision)

    @staticmethod
    def _governed_approval_kind_for_decision(decision: BrowserActionDecision) -> ApprovalKind | None:
        return BrowserActionService.governed_approval_kind_for_decision(decision)

    @staticmethod
    def _action_class(action_name: str) -> str:
        return BrowserActionService.action_class(action_name)

    def _assert_url_allowed(self, url: str) -> None:
        host = urlparse(url).hostname
        if not host:
            raise PermissionError(f"Could not determine hostname for URL: {url}")
        patterns = self.settings.allowed_host_patterns
        if "*" in patterns:
            return
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
        return BrowserActionService.resolve_target(selector=selector, element_id=element_id, x=x, y=y)

    def _safe_upload_path(self, file_path: str, *, session: BrowserSession | None = None) -> Path:
        return self.uploads.safe_path(file_path, session=session)

    @staticmethod
    def _path_is_contained_by(candidate: Path, root: Path) -> bool:
        return BrowserUploadService.path_is_contained_by(candidate, root)

    def _safe_session_auth_path(
        self,
        session: BrowserSession,
        relative_path: str,
        *,
        must_exist: bool = False,
    ) -> Path:
        return self.auth_profiles.safe_session_auth_path(session, relative_path, must_exist=must_exist)

    def _safe_auth_path(self, relative_path: str, must_exist: bool = False) -> Path:
        return self.auth_profiles.safe_auth_path(relative_path, must_exist=must_exist)

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
        return await self.diagnostics.handle_download(session, download)

    async def _append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        await self.artifacts.append_jsonl(path, payload)

    @staticmethod
    def _append_text(path: Path, text: str) -> None:
        SessionArtifactService.append_text(path, text)

    # ── Screenshot diff ──────────────────────────────────────────────────────

    async def screenshot_diff(self, session_id: str) -> dict[str, Any]:
        return await self.diagnostics.screenshot_diff(session_id)

    @staticmethod
    def _compute_diff(
        a_path: str,
        b_path: str,
        a_url: str,
        b_url: str,
        artifact_dir: Path,
    ) -> dict[str, Any]:
        return BrowserDiagnosticsService.compute_diff(a_path, b_path, a_url, b_url, artifact_dir)

    # ── Auth profile export / import ────────────────────────────────────────

    async def export_auth_profile(self, profile_name: str) -> dict[str, Any]:
        """Package an auth profile dir as a .tar.gz and return the artifact path."""
        return await self.auth_profiles.export(profile_name)

    @staticmethod
    def _write_tar(source_dir: Path, dest: Path) -> None:
        BrowserAuthProfileService.write_tar(source_dir, dest)

    @staticmethod
    def _safe_auth_archive_member_name(member_name: str):
        return BrowserAuthProfileService.safe_archive_member_name(member_name)

    async def import_auth_profile(self, archive_path: str, *, overwrite: bool = False) -> dict[str, Any]:
        """Extract a .tar.gz archive into the reusable auth profile root."""
        return await self.auth_profiles.import_profile(archive_path, overwrite=overwrite)
