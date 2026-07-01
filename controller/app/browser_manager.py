from __future__ import annotations

import asyncio
import fnmatch
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

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
from .config import Settings
from .downloads import DownloadCaptureService
from .memory_manager import MemoryManager
from .models import (
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


SessionCreatedHook = Callable[[str, Page], Awaitable[None]]
SessionClosedHook = Callable[[str], Awaitable[None]]


class BrowserManager:
    """Facade and composition root for the browser control plane.

    Domain logic lives in ``app.browser.services``; this class wires the
    services together and exposes the public API. The remaining private
    methods are deliberate seams: services and ``actions/pipeline.py`` route
    shared calls through them so tests can patch a single point.
    """

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
        self.approvals = ApprovalStore(
            self.settings.approval_root,
            db_path=self.settings.state_db_path,
            approval_ttl_minutes=self.settings.approval_ttl_minutes,
        )
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

    # ── Lifecycle ────────────────────────────────────────────────────────────

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
            self.playwright = None
        await self.tunnel_broker.shutdown()
        await self.witness_remote.shutdown()
        await self.session_store.shutdown()

    # ── Browser runtime ──────────────────────────────────────────────────────

    async def ensure_browser(self) -> Browser:
        return await self.runtime.ensure_browser()

    async def cdp_attach(self, cdp_url: str) -> dict[str, Any]:
        return await self.runtime.cdp_attach(cdp_url)

    async def _resolve_browser_ws_endpoint(self) -> str:
        return await self.runtime.resolve_browser_ws_endpoint()

    async def _acquire_session_browser(self, session_id: str) -> tuple[Browser, IsolatedBrowserRuntime | None]:
        return await self.runtime.acquire_session_browser(session_id)

    # ── Remote access ────────────────────────────────────────────────────────

    def get_remote_access_info(self, session_id: str | None = None) -> dict[str, Any]:
        return self.remote_access.get_info(session_id)

    def _current_takeover_url(self, session: BrowserSession | None = None) -> str:
        return self.remote_access.current_takeover_url(session)

    # ── Sessions ─────────────────────────────────────────────────────────────

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

    async def get_session(self, session_id: str) -> BrowserSession:
        return await self.session_lifecycle.get(session_id)

    async def get_session_record(self, session_id: str) -> dict[str, Any]:
        return await self.session_lifecycle.get_record(session_id)

    async def get_session_summary(self, session_id: str) -> dict[str, Any]:
        """Public API for getting a session summary by ID."""
        return await self.session_lifecycle.get_summary(session_id)

    async def close_session(self, session_id: str) -> dict[str, Any]:
        return await self.session_lifecycle.close(session_id)

    async def fork_session(
        self,
        session_id: str,
        *,
        name: str | None = None,
        start_url: str | None = None,
    ) -> dict[str, Any]:
        """Fork a session: clone cookies + localStorage state into a new session."""
        return await self.session_lifecycle.fork(session_id, name=name, start_url=start_url)

    async def enable_shadow_browse(self, session_id: str) -> dict[str, Any]:
        """Launch a headed clone of a session for visual debugging."""
        return await self.session_lifecycle.enable_shadow_browse(session_id)

    def _check_session_limit(self) -> None:
        self.session_lifecycle.check_limit()

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

    async def _session_summary(
        self,
        session: BrowserSession,
        *,
        status: SessionStatus = "active",
        live: bool = True,
    ) -> dict[str, Any]:
        return await self.session_lifecycle.summary(session, status=status, live=live)

    async def _persist_session(self, session: BrowserSession, *, status: SessionStatus) -> None:
        await self.session_lifecycle.persist(session, status=status)

    async def _maybe_provision_session_tunnel(self, session: BrowserSession) -> None:
        await self.session_lifecycle.maybe_provision_tunnel(session)

    # ── Approvals ────────────────────────────────────────────────────────────

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

    async def require_governed_approval(
        self,
        session_id: str,
        decision: BrowserActionDecision,
        *,
        approval_id: str | None,
    ):
        return await self.actions.require_governed_approval(session_id, decision, approval_id=approval_id)

    # ── Observation ──────────────────────────────────────────────────────────

    async def observe(self, session_id: str, limit: int = 40, preset: str = "normal") -> dict[str, Any]:
        return await self.observation.observe(session_id, limit=limit, preset=preset)

    async def capture_screenshot(self, session_id: str, *, label: str = "manual") -> dict[str, Any]:
        return await self.observation.capture_screenshot(session_id, label=label)

    async def stop_trace(self, session_id: str) -> dict[str, Any]:
        return await self.observation.stop_trace(session_id)

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

    # ── Diagnostics ──────────────────────────────────────────────────────────

    async def get_console_messages(self, session_id: str, *, limit: int = 20) -> dict[str, Any]:
        return await self.diagnostics.get_console_messages(session_id, limit=limit)

    async def get_page_errors(self, session_id: str, *, limit: int = 20) -> dict[str, Any]:
        return await self.diagnostics.get_page_errors(session_id, limit=limit)

    async def get_request_failures(self, session_id: str, *, limit: int = 20) -> dict[str, Any]:
        return await self.diagnostics.get_request_failures(session_id, limit=limit)

    async def get_network_log(
        self,
        session_id: str,
        *,
        limit: int = 100,
        method: str | None = None,
        url_contains: str | None = None,
    ) -> dict[str, Any]:
        return await self.diagnostics.get_network_log(
            session_id,
            limit=limit,
            method=method,
            url_contains=url_contains,
        )

    async def list_downloads(self, session_id: str) -> list[dict[str, Any]]:
        return await self.diagnostics.list_downloads(session_id)

    async def screenshot_diff(self, session_id: str) -> dict[str, Any]:
        return await self.diagnostics.screenshot_diff(session_id)

    def get_pii_scrubber_status(self) -> dict[str, Any]:
        """Return current PII scrubber configuration."""
        return self.pii_scrubber.summary()

    def _attach_page_listeners(self, page: Page, session: BrowserSession) -> None:
        self.diagnostics.attach_page_listeners(page, session)

    async def _handle_download(self, session: BrowserSession, download: Any) -> None:
        return await self.diagnostics.handle_download(session, download)

    # ── Actions ──────────────────────────────────────────────────────────────

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

    async def _run_action(
        self,
        session: BrowserSession,
        action_name: str,
        target: dict[str, Any],
        operation,
    ) -> dict[str, Any]:
        return await self.actions.run_action(session, action_name, target, operation)

    async def _settle(self, page: Page) -> None:
        await self.actions.settle(page)

    async def _check_bot_challenge(self, session: BrowserSession) -> dict[str, Any] | None:
        return await self.bot_challenge.check(session)

    async def _maybe_handle_totp(self, session: BrowserSession) -> dict[str, Any] | None:
        return await self.actions.maybe_handle_totp(session)

    @staticmethod
    def _action_class(action_name: str) -> str:
        return BrowserActionService.action_class(action_name)

    @staticmethod
    def _action_verification(
        action_name: str,
        target: dict[str, Any],
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> dict[str, Any]:
        return BrowserActionService.action_verification(action_name, target, before, after)

    # ── URL policy ───────────────────────────────────────────────────────────

    def _assert_url_allowed(self, url: str) -> None:
        host = urlparse(url).hostname
        if not host:
            raise PermissionError(f"Could not determine hostname for URL: {url}")
        patterns = self.settings.allowed_host_patterns
        if not patterns or "*" in patterns:
            return
        for pattern in patterns:
            pattern = pattern.lower()
            normalized = pattern.removeprefix("*.")
            if fnmatch.fnmatch(host, pattern) or host == normalized or host.endswith(f".{normalized}"):
                return
        raise PermissionError(f"Host {host!r} is not allowlisted")

    def _assert_runtime_url_allowed(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme in {"about", "data", "blob", ""}:
            return
        self._assert_url_allowed(url)

    # ── Tabs ─────────────────────────────────────────────────────────────────

    async def list_tabs(self, session_id: str) -> list[dict[str, Any]]:
        return await self.tabs.list(session_id)

    async def open_tab(self, session_id: str, url: str | None, activate: bool) -> dict[str, Any]:
        return await self.tabs.open(session_id, url, activate)

    async def activate_tab(self, session_id: str, index: int) -> dict[str, Any]:
        return await self.tabs.activate(session_id, index)

    async def close_tab(self, session_id: str, index: int) -> dict[str, Any]:
        return await self.tabs.close(session_id, index)

    # ── Auth profiles & storage state ────────────────────────────────────────

    async def save_storage_state(self, session_id: str, path: str) -> dict[str, Any]:
        return await self.auth_profiles.save_storage_state(session_id, path)

    async def save_auth_profile(self, session_id: str, profile_name: str) -> dict[str, Any]:
        return await self.auth_profiles.save(session_id, profile_name)

    async def get_auth_profile(self, profile_name: str) -> dict[str, Any]:
        return await self.auth_profiles.get(profile_name)

    async def list_auth_profiles(self) -> list[dict[str, Any]]:
        return await self.auth_profiles.list()

    async def export_auth_profile(self, profile_name: str) -> dict[str, Any]:
        """Package an auth profile dir as a .tar.gz and return the artifact path."""
        return await self.auth_profiles.export(profile_name)

    async def import_auth_profile(self, archive_path: str, *, overwrite: bool = False) -> dict[str, Any]:
        """Extract a .tar.gz archive into the reusable auth profile root."""
        return await self.auth_profiles.import_profile(archive_path, overwrite=overwrite)

    async def get_auth_state_info(self, session_id: str) -> dict[str, Any]:
        return await self.auth_profiles.auth_state_info(session_id)

    # ── Takeover ─────────────────────────────────────────────────────────────

    async def request_human_takeover(self, session_id: str, reason: str) -> dict[str, Any]:
        return await self.takeover.request(session_id, reason)

    # ── Audit & witness ──────────────────────────────────────────────────────

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

    async def verify_witness_chain(self, session_id: str) -> dict[str, Any]:
        return await self.witness.verify(session_id)

    def _initial_witness_remote_state(self, protection_mode: str) -> WitnessRemoteState:
        return self.witness_bridge.initial_remote_state(protection_mode)

    async def _ensure_witness_remote_ready(self, session: BrowserSession, *, action: str) -> None:
        await self.witness_bridge.ensure_remote_ready(session, action=action)

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

    # ── Artifacts ────────────────────────────────────────────────────────────

    async def _append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        await self.artifacts.append_jsonl(path, payload)
