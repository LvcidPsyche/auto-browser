from __future__ import annotations

import asyncio
import fnmatch
import logging
import weakref
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse, urlsplit

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

from .action_errors import RefusedPageError, SessionNotFoundError
from .actions import BrowserActionPipeline
from .approvals import ApprovalStore
from .artifacts import SessionArtifactService
from .audit import AuditStore
from .auth_state import AuthStateManager
from .browser.element_ids import ElementIds
from .browser.page_provenance import CLOSED_PAGE_GRACE_SECONDS, REFUSED_FAILURES_KEPT, PageProvenance
from .browser.page_read import start_read
from .browser.request_gate import GateBlock, RequestGates
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
    HTTP_URL_SCHEMES,
    ApprovalDecider,
    ApprovalOrigin,
    BrowserActionDecision,
    SessionStatus,
    WitnessRemoteState,
)
from .network_inspector import NetworkInspector, url_origin
from .ocr import OCRExtractor
from .pii_scrub import PiiScrubber
from .session_isolation import DockerBrowserNodeProvisioner, IsolatedBrowserRuntime
from .session_ownership import as_system
from .session_store import DurableSessionStore
from .session_tunnel import IsolatedSessionTunnel, IsolatedSessionTunnelBroker
from .url_safety import browser_equivalent_url
from .utils import spawn_background_task
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


def _build_witness_signer(settings):
    """Ed25519 signer for witness receipts, or None if it cannot be created.

    Keys live beside the receipts they sign (`<witness_root>/keys`) and are
    generated on first use, so signing is on by default with no configuration.

    A failure here must not take the controller down: an unsigned chain is still
    a useful audit log, and refusing to boot over key material would make the
    security improvement a liability. It is logged at exception level, and
    verify() reports unsigned receipts, so the degraded state stays visible
    rather than being quietly assumed away.
    """
    try:
        from .witness_signing import WitnessSigner

        return WitnessSigner(Path(settings.witness_root) / "keys")
    except Exception:
        logger.exception("witness: could not initialise the signing key; receipts will be recorded unsigned")
        return None


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
    # True only for a browser this session alone holds (an isolated runtime's, or a
    # shadow-browse window), the one close_session may close. The shared node's
    # browser outlives any one session, and manager.browser can be swapped by
    # cdp_attach, so identity against manager.browser cannot tell the two apart.
    owns_browser: bool = False
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
    # Weak refs: a collected Page must not block re-attaching listeners to a
    # new Page that happens to reuse its id().
    attached_pages: weakref.WeakSet[Any] = field(default_factory=weakref.WeakSet)
    # Top-level requests through a URL off the list (to one, or redirected from one) that only this
    # session's context saw, before it reported their page (a popup's first navigation): see
    # BrowserDiagnosticsService.on_context_request.
    unframed_hops: list[Any] = field(default_factory=list)
    # Final URLs of those navigations that failed: Playwright reports the download a navigation of a page
    # it has not reported turns into on that page's opener, so the download is judged by this record.
    refused_failures: deque[str] = field(default_factory=lambda: deque(maxlen=REFUSED_FAILURES_KEPT))
    last_action: str | None = None
    proxy_persona: str | None = None
    last_auth_state_path: Path | None = None
    # The last screenshot a checked read returned (see capture_session_screenshot).
    last_screenshot: dict[str, str] | None = None
    auth_profile_name: str | None = None
    tunnel_error: str | None = None
    mouse_position: tuple[float, float] | None = None
    totp_secret: str | None = None
    totp_hosts: tuple[str, ...] = ()
    network_inspector: NetworkInspector | None = None
    # Headless/headed state — set to False to request headed mode on next fork
    headless: bool = True
    protection_mode: str = "normal"
    pending_witness_context: dict[str, Any] | None = None
    witness_remote_state: WitnessRemoteState = field(default_factory=WitnessRemoteState)
    metadata: dict[str, Any] = field(default_factory=dict)
    # The token-verified operator who created it; None for an unowned session
    # (see app/session_ownership.py).
    owner: str | None = None
    # The browserContextId the pre-request gate owns for this session's context (None while it is off).
    gate_context_id: str | None = None


SessionCreatedHook = Callable[[str, Page], Awaitable[None]]
SessionClosedHook = Callable[[str], Awaitable[None]]
# Undoes what the created hook registered when create_session fails after it ran.
SessionCreateFailedHook = Callable[[str], Awaitable[None]]


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
        self.page_provenance = PageProvenance(url_refused=self._url_off_list)
        self._browser_lock = asyncio.Lock()
        self.request_gates = RequestGates(
            url_refused=self._url_off_list,
            enabled=self._pre_request_blocking_on,
            origin_switch_declared=lambda: self.settings.cdp_block_origin_on_redirect_disabled,
            default_attach_mode=lambda: bool(self.settings.cdp_connect_url),
            on_lost=lambda gate: self.runtime.gate_lost(gate),
            on_block=self._on_navigation_stopped,
            off_reason=self._pre_request_blocking_off_reason,
        )
        self.action_pipeline = BrowserActionPipeline()
        self.element_ids = ElementIds(settings)
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
        self.audit = AuditStore(
            self.settings.audit_root,
            db_path=self.settings.state_db_path,
            max_events=self.settings.audit_max_events,
        )
        self.approvals = ApprovalStore(
            self.settings.approval_root,
            db_path=self.settings.state_db_path,
            approval_ttl_minutes=self.settings.approval_ttl_minutes,
            audit=self.audit,
            rejection_receipt=self.approval_service.rejection_receipt,
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
        self.witness = WitnessRecorder(self.settings.witness_root, signer=_build_witness_signer(self.settings))
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
        self._session_create_failed_hook: SessionCreateFailedHook | None = None

    def register_extension_hooks(
        self,
        *,
        session_created: SessionCreatedHook | None = None,
        session_closed: SessionClosedHook | None = None,
        session_create_failed: SessionCreateFailedHook | None = None,
    ) -> None:
        self._session_created_hook = session_created
        self._session_closed_hook = session_closed
        self._session_create_failed_hook = session_create_failed

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
        # Every operator's sessions close, not only those of whoever is current.
        with as_system():
            for session_id in session_ids:
                try:
                    await self.close_session(session_id)
                except Exception as exc:  # pragma: no cover - best effort cleanup
                    logger.warning("failed to close session %s during shutdown: %s", session_id, exc)

        await self.request_gates.release_all()
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
        totp_hosts: list[str] | None = None,
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
            totp_hosts=totp_hosts,
        )

    async def get_session(self, session_id: str) -> BrowserSession:
        return await self.session_lifecycle.get(session_id)

    async def get_session_record(self, session_id: str) -> dict[str, Any]:
        return await self.session_lifecycle.get_record(session_id)

    async def ensure_session_accessible(self, session_id: str) -> None:
        await self.session_lifecycle.ensure_accessible(session_id)

    async def session_accessible(self, session_id: str) -> bool:
        return await self.session_lifecycle.accessible(session_id)

    async def session_owner(self, session_id: str) -> str | None:
        return await self.session_lifecycle.owner_of(session_id)

    async def session_owner_lookup(self, session_id: str) -> tuple[bool, str | None]:
        """(known, owner): unlike session_owner, tells a session nobody owns from one with no record."""
        return await self.session_lifecycle.find_owner(session_id)

    async def get_session_summary(self, session_id: str) -> dict[str, Any]:
        """Public API for getting a session summary by ID."""
        return await self.session_lifecycle.get_summary(session_id)

    async def close_session(self, session_id: str, *, reason: str | None = None) -> dict[str, Any]:
        return await self.session_lifecycle.close(session_id, reason=reason)

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

    async def approve(
        self,
        approval_id: str,
        comment: str | None = None,
        *,
        decided_via: ApprovalDecider = "operator",
    ) -> dict[str, Any]:
        return await self.approval_service.approve(approval_id, comment=comment, decided_via=decided_via)

    async def reject(
        self,
        approval_id: str,
        comment: str | None = None,
        *,
        decided_via: ApprovalDecider = "operator",
    ) -> dict[str, Any]:
        return await self.approval_service.reject(approval_id, comment=comment, decided_via=decided_via)

    async def approval_arguments(self, approval_id: str) -> dict[str, Any]:
        return await self.approval_service.arguments(approval_id)

    async def execute_approval(self, approval_id: str) -> dict[str, Any]:
        return await self.approval_service.execute(approval_id)

    async def require_governed_approval(
        self,
        session_id: str | None,
        decision: BrowserActionDecision,
        *,
        approval_id: str | None,
        reason: str | None = None,
        origin: ApprovalOrigin = "action",
        arguments: str | None = None,
    ):
        return await self.actions.require_governed_approval(
            session_id, decision, approval_id=approval_id, reason=reason, origin=origin, arguments=arguments
        )

    # ── Observation ──────────────────────────────────────────────────────────

    async def observe(self, session_id: str, limit: int = 40, preset: str | None = None) -> dict[str, Any]:
        return await self.observation.observe(session_id, limit=limit, preset=preset)

    async def share_snapshot(self, session_id: str) -> dict[str, Any]:
        return await self.observation.share_snapshot(session_id)

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

    async def read_download_text(self, session_id: str, download_id: str | None = None) -> dict[str, Any]:
        return await self.diagnostics.read_download_text(session_id, download_id)

    async def screenshot_diff(self, session_id: str) -> dict[str, Any]:
        return await self.diagnostics.screenshot_diff(session_id)

    def get_pii_scrubber_status(self) -> dict[str, Any]:
        """Return current PII scrubber configuration."""
        return self.pii_scrubber.summary()

    def _attach_page_listeners(self, page: Page, session: BrowserSession) -> None:
        self.diagnostics.attach_page_listeners(page, session)

    def _attach_popup(self, page: Page, session: BrowserSession) -> None:
        """Attach to a page the context reports (a popup, a new tab), judged by its opener too."""
        self.diagnostics.attach_popup(page, session)

    async def _handle_download(self, session: BrowserSession, download: Any, *, page_refused: bool = False) -> None:
        return await self.diagnostics.handle_download(session, download, page_refused=page_refused)

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
        *,
        change_made=None,
        began=None,
    ) -> dict[str, Any]:
        return await self.actions.run_action(
            session, action_name, target, operation, change_made=change_made, began=began
        )

    async def _settle(self, page: Page) -> None:
        await self.actions.settle(page)

    async def _check_bot_challenge(self, session: BrowserSession) -> dict[str, Any] | None:
        try:
            read = start_read(self, session)
        except PermissionError:
            # A refused page is not probed: the probe reads its title and iframes, and the
            # pipeline's own checks refuse the page after this.
            return None
        challenge = await self.bot_challenge.check(session)
        if challenge is not None:
            # The probe's title and iframe sources go to the agent in the error: a page
            # that moved while it was probed is refused instead of described.
            read.check()
        return challenge

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
        # Parsed as the browser will parse it, not as urllib does — see
        # app/url_safety.py for the backslash host confusion this closes.
        parsed = urlparse(browser_equivalent_url(url))
        # The scheme is decided here, not left to each request model: the REST
        # fork route had none, and file://localhost/... passed this gate on the
        # strength of its host and opened local files in the browser.
        if parsed.scheme.lower() not in HTTP_URL_SCHEMES:
            raise PermissionError(f"URL scheme {parsed.scheme!r} is not allowed; only http and https are")
        host = parsed.hostname
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
        # A blob: URL is a file its origin's page generated (blob:https://host/uuid),
        # so that origin is held to the list; an opaque one (blob:null/...) names no
        # host, and the page that made it is judged instead (PageProvenance).
        if parsed.scheme == "blob":
            inner = urlsplit(parsed.path)
            if inner.scheme.lower() in HTTP_URL_SCHEMES:
                self._assert_url_allowed(parsed.path)
            return
        # Where a page may legitimately be after an action: a blank or
        # script-built document, or Chromium's own page for a failed load.
        if parsed.scheme in {"about", "data", "chrome-error", ""}:
            return
        self._assert_url_allowed(url)

    def _url_off_list(self, url: str) -> bool:
        """Whether a page may not be at ``url`` under ALLOWED_HOSTS (see _assert_runtime_url_allowed)."""
        try:
            self._assert_runtime_url_allowed(url)
        except PermissionError:
            return True
        return False

    def _on_navigation_stopped(self, block: GateBlock) -> None:
        """Audit a navigation the gate stopped, by its origin only, once the gate has answered Chromium.

        A block with no context (a frame the gate could not look up) goes to the live session whose page has that
        targetId, as far as targetIds were read already; otherwise to no session."""
        sessions = list(self.sessions.values())
        session = next((s for s in sessions if block.context_id and s.gate_context_id == block.context_id), None)
        if session is None and block.target_id:
            session = next(
                (
                    s
                    for s in sessions
                    if any(
                        self.request_gates.known_target_id(page) == block.target_id
                        for page in list(getattr(s.context, "pages", []))
                    )
                ),
                None,
            )
        spawn_background_task(
            self.audit.append(
                event_type="navigation_blocked",
                status="blocked",
                action="navigate",
                session_id=session.id if session is not None else None,
                details={"url": block.origin, "source": "request", "reason": block.reason},
            )
        )

    def _pre_request_blocking_on(self) -> bool:
        """Whether browsers get the pre-request gate: PRE_REQUEST_BLOCKING, and an ALLOWED_HOSTS that refuses something."""
        return self._pre_request_blocking_off_reason() is None

    def _pre_request_blocking_off_reason(self) -> str | None:
        """Why there is no gate: ``allows_every_host`` (an empty ALLOWED_HOSTS, or one with ``*``: nothing to stop,
        whatever PRE_REQUEST_BLOCKING says) or ``disabled`` (PRE_REQUEST_BLOCKING=false); None while it is on."""
        patterns = self.settings.allowed_host_patterns
        if not patterns or "*" in patterns:
            return "allows_every_host"
        if not self.settings.pre_request_blocking:
            return "disabled"
        return None

    def _cookie_off_list(self, domain: str) -> bool:
        """Whether no host ALLOWED_HOSTS allows would receive a cookie stored for ``domain``.

        A host-only cookie goes to its host alone. A domain cookie (".example.com") also
        goes to every host under its domain, so it is kept for its domain or for a host
        the list allows under it: "app.example.com" keeps ".example.com". An entry with a
        leading dot allows no host (_assert_url_allowed matches none), so it keeps none.
        """
        if not self._url_off_list(f"https://{domain.lstrip('.')}/"):
            return False
        if not domain.startswith("."):
            return True
        under = domain.lower()
        return not any(
            entry.lower().endswith(under) and not entry.startswith(".") for entry in self.settings.allowed_host_patterns
        )

    def _partition_off_list(self, partition_key: str | None) -> bool:
        """Whether a partitioned (CHIPS) cookie's top-level site is one no listed host belongs to.

        Such a cookie is sent only under that top-level site, so one keyed to a refused site was set
        by a frame on the refused page and never reaches an allowed top-level page. The key is a
        site (https://example.com), so it is judged like a domain cookie for that site. A key that
        names no host is refused.
        """
        if not partition_key:
            return False
        host = urlsplit(partition_key).hostname
        if host is None:
            return True
        # An IPv6 host goes back into a URL in brackets (https://[::1]/): unbracketed it names no host.
        return self._cookie_off_list(f".[{host}]" if ":" in host else f".{host}")

    def _page_refused(self, page: Any) -> bool:
        """Whether ALLOWED_HOSTS refuses this page: its URL is off the list, or where it came from is.

        A page that committed a refused URL stays refused on the controller's blank
        until the agent navigates it to an allowed URL, and a blank window a refused page opened is
        refused with it (see app/browser/page_provenance.py).
        """
        return self._url_off_list(page.url) or self.page_provenance.refused(page)

    def _shown_url(self, page: Any) -> str:
        """The URL a listing or record shows for a page: its origin only while ALLOWED_HOSTS refuses it.

        A refused page chose its URL's path and query, which can carry what it read, so they are
        cut as the network log and blocked downloads cut them (url_origin).
        """
        url = page.url
        if url == "about:blank" or not self._page_refused(page):
            return url
        return url_origin(url)

    def _page_off_list(self, session: BrowserSession) -> bool:
        """Whether ALLOWED_HOSTS refuses the session's active page (see _page_refused)."""
        return self._page_refused(session.page)

    def _session_has_refused_page(self, session: BrowserSession, *, besides: Any = None) -> bool:
        """Whether a page of the session, its active one or another tab, can run a refused host's code.

        Its URL is off the list, or PageProvenance.counts_for_its_session: not every page _page_refused
        refuses, since one refused only because another tab was, or only waiting for its opener's
        verdict, has its document from an allowed origin while it is on an allowed http(s) URL, and none
        on the controller's own blank. A page that counted still counts for CLOSED_PAGE_GRACE_SECONDS
        after it closed: what it did just before closing is delivered after the close. ``besides`` is a page
        left out: the one whose commit is judged by what the others could do to it.
        """
        return self.page_provenance.counting_page_closed_recently(session.id) or self._open_page_counts(
            session, besides=besides
        )

    def _open_page_counts(self, session: BrowserSession, *, besides: Any = None) -> bool:
        """Whether an open page of the session, other than ``besides``, counts for it (_session_has_refused_page)."""
        return any(
            page is not besides and (self._url_off_list(page.url) or self.page_provenance.counts_for_its_session(page))
            for page in self.tabs.pages(session)
        )

    async def _wait_out_closed_page_grace(self, session: BrowserSession) -> None:
        """Before the agent's own action on a tab: wait out the grace after a page that counted closed (bounded).

        The refusal's advice is to close the refused tab and then act; what the tab commits at once would count
        as the closed tab's choice. Not while a page that counts is still open: its commits are judged that way
        whether the action waits or not, and the advice is to close it first.
        """
        if not self.page_provenance.counting_page_closed_recently(session.id) or self._open_page_counts(session):
            return
        await self.page_provenance.after_the_closed_page_grace(session.id)

    def _assert_page_allowed(self, session: BrowserSession) -> None:
        """Refuse to read a page ALLOWED_HOSTS refuses (see _page_refused).

        The list is enforced where the controller moves the page. A page can also
        move itself after an action has finished (a timer, a late redirect), and
        what it shows there must not reach a caller. The page guard in
        diagnostics sends such a page to about:blank; this covers the moments
        before it has, and every path that returns page content calls it first.
        """
        self._assert_page_object_allowed(session.page)

    def _assert_page_object_allowed(self, page: Any) -> None:
        try:
            self._assert_runtime_url_allowed(page.url)
        except PermissionError as exc:
            raise RefusedPageError(
                f"The page navigated on its own to a host ALLOWED_HOSTS does not allow ({exc}) "
                "and is being moved to about:blank. It is not read until it is navigated to an allowed URL."
            ) from exc
        if self.page_provenance.refused_by_another_page(page):
            # The cause is another tab, and while it stays open this page's own navigations are refused too;
            # or one that closed within the grace, already gone.
            raise RefusedPageError(
                "The page loaded while another tab of this session was refused, or within "
                f"{CLOSED_PAGE_GRACE_SECONDS:g} seconds after a refused tab closed, so it is sent back to "
                "about:blank and not read: that tab can send this one to a URL it chose. Close the refused "
                "tab if it is still open, then navigate this one to an allowed URL. A tab counts here while it "
                "is on a host ALLOWED_HOSTS does not allow, came from one, or is navigating through one, and "
                "while it is refused on a blank, blob: or data: page other than the controller's own blank; a "
                "tab on an allowed page refused only because of another tab, or waiting for its opener, does not."
            )
        if self.page_provenance.refused(page):
            raise RefusedPageError(
                "The page was on a host ALLOWED_HOSTS does not allow, was opened by a page there, or "
                "loaded a blank, blob: or data: page while another tab of this session was refused, "
                "and is not read until it is navigated to an allowed URL."
            )

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
        # A hash chain only proves internal consistency — anyone holding the file
        # can rewrite it and recompute every hash, so "valid" includes the
        # signatures and "valid: true" is never mistaken for "attested".
        await self._require_witness_scope(session_id)
        return await self.witness.verify_with_signatures(session_id)

    async def export_witness_bundle(self, session_id: str) -> dict[str, Any]:
        """Third-party-verifiable evidence bundle for a session."""
        await self._require_witness_scope(session_id)
        return await self.witness.export_bundle(session_id)

    async def _require_witness_scope(self, session_id: str) -> None:
        """Refuse an id with no session record, no chain and no anchor, as the session routes do.

        Without named credentials a chain or an anchor alone is enough: the auth-profile
        receipts (scope ``auth-profiles``) have a chain but no session record. Under named
        credentials the session check every route and tool runs first
        (ensure_session_accessible) refuses such an id before this runs, that chain's
        included, for SYSTEM too: no owner can be checked for it, and refusing it keeps one
        operator from another's profile-export receipts.
        """
        if not self.witness.has_evidence(session_id) and not (await self.session_owner_lookup(session_id))[0]:
            raise SessionNotFoundError(session_id)

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
