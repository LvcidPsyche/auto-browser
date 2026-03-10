from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from playwright.async_api import Browser, BrowserContext, Error as PlaywrightError, Page, Playwright, async_playwright

from .audit import AuditStore
from .approvals import ApprovalRequiredError, ApprovalStore
from .auth_state import AuthStateManager
from .config import Settings
from .models import ApprovalKind, BrowserActionDecision, SessionRecord, SessionStatus
from .ocr import OCRExtractor
from .session_store import DurableSessionStore
from .session_isolation import DockerBrowserNodeProvisioner, IsolatedBrowserRuntime
from .session_tunnel import IsolatedSessionTunnel, IsolatedSessionTunnelBroker

logger = logging.getLogger(__name__)

INTERACTABLES_SCRIPT = r"""
(limit) => {
  function isVisible(el) {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  }

  function getLabel(el) {
    const raw = el.getAttribute('aria-label')
      || el.getAttribute('placeholder')
      || el.innerText
      || el.value
      || el.getAttribute('name')
      || el.id
      || el.href
      || '';
    return String(raw).replace(/\s+/g, ' ').trim().slice(0, 160);
  }

  const selector = [
    'a',
    'button',
    'input',
    'textarea',
    'select',
    '[role="button"]',
    '[role="link"]',
    '[role="textbox"]',
    '[contenteditable="true"]',
    '[tabindex]'
  ].join(',');

  const out = [];
  for (const el of document.querySelectorAll(selector)) {
    if (!isVisible(el) || el.closest('[aria-hidden="true"]')) continue;
    if (!el.dataset.operatorId) {
      el.dataset.operatorId = `op-${Math.random().toString(36).slice(2, 10)}`;
    }
    const rect = el.getBoundingClientRect();
    out.push({
      element_id: el.dataset.operatorId,
      selector_hint: `[data-operator-id="${el.dataset.operatorId}"]`,
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type'),
      role: el.getAttribute('role') || el.tagName.toLowerCase(),
      label: getLabel(el),
      disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
      href: el.href || null,
      bbox: {
        x: Math.round(rect.x),
        y: Math.round(rect.y),
        width: Math.round(rect.width),
        height: Math.round(rect.height)
      }
    });
    if (out.length >= limit) break;
  }
  return out;
}
"""

ACTIVE_ELEMENT_SCRIPT = r"""
() => {
  const el = document.activeElement;
  if (!el) return null;
  return {
    tag: el.tagName.toLowerCase(),
    element_id: el.dataset?.operatorId || null,
    name: el.getAttribute('name'),
    id: el.id || null,
    label: (el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.innerText || el.value || '').toString().replace(/\s+/g, ' ').trim().slice(0, 120)
  };
}
"""

PAGE_SUMMARY_SCRIPT = r"""
(textLimit) => {
  const squash = (value, maxLength = textLimit) =>
    String(value || '').replace(/\s+/g, ' ').trim().slice(0, maxLength);

  const headings = Array.from(document.querySelectorAll('h1,h2,h3'))
    .slice(0, 8)
    .map((el) => ({
      level: el.tagName.toLowerCase(),
      text: squash(el.innerText, 160)
    }))
    .filter((item) => item.text);

  const forms = Array.from(document.forms)
    .slice(0, 3)
    .map((form) => ({
      action: form.getAttribute('action') || null,
      method: (form.getAttribute('method') || 'get').toLowerCase(),
      fields: Array.from(form.querySelectorAll('input, textarea, select, button'))
        .slice(0, 8)
        .map((field) => ({
          tag: field.tagName.toLowerCase(),
          type: field.getAttribute('type') || null,
          name: field.getAttribute('name') || null,
          label: squash(
            field.getAttribute('aria-label')
              || field.getAttribute('placeholder')
              || field.innerText
              || field.value
              || field.getAttribute('name')
              || field.id,
            80
          ),
          disabled: Boolean(field.disabled || field.getAttribute('aria-disabled') === 'true')
        }))
    }));

  return {
    text_excerpt: squash(document.body?.innerText || '', textLimit),
    dom_outline: {
      headings,
      forms,
      counts: {
        links: document.querySelectorAll('a').length,
        buttons: document.querySelectorAll('button, [role=\"button\"]').length,
        inputs: document.querySelectorAll('input, textarea, select').length,
        forms: document.forms.length
      }
    }
  };
}
"""

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
    last_action: str | None = None
    last_auth_state_path: Path | None = None
    tunnel_error: str | None = None


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
        self.approvals = ApprovalStore(self.settings.approval_root, db_path=self.settings.state_db_path)
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
    ) -> dict[str, Any]:
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
        auth_dir = self._session_auth_root(session_id)
        upload_dir = self._session_upload_root(session_id)
        auth_dir.mkdir(parents=True, exist_ok=True)
        upload_dir.mkdir(parents=True, exist_ok=True)
        prepared_auth_state = None

        context_kwargs: dict[str, Any] = {
            "viewport": {
                "width": self.settings.default_viewport_width,
                "height": self.settings.default_viewport_height,
            },
            "accept_downloads": True,
        }
        if storage_state_path:
            source_path = self._safe_auth_path(storage_state_path, must_exist=True)
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
            )
            self._attach_page_listeners(page, session)
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
                    "isolation_mode": session.isolation_mode,
                    "browser_node": session.browser_node_name,
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

    async def navigate(self, session_id: str, url: str) -> dict[str, Any]:
        self._assert_url_allowed(url)
        session = await self.get_session(session_id)

        async def operation() -> None:
            await session.page.goto(url, wait_until="domcontentloaded")
            await self._settle(session.page)

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
                await session.page.mouse.click(float(x), float(y))
            else:
                locator = session.page.locator(target["selector"]).first
                await locator.scroll_into_view_if_needed()
                await locator.click()
            await self._settle(session.page)

        return await self._run_action(session, "click", target, operation)

    async def type(
        self,
        session_id: str,
        *,
        text: str,
        selector: str | None = None,
        element_id: str | None = None,
        clear_first: bool = True,
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        target = self._resolve_target(selector=selector, element_id=element_id)

        async def operation() -> None:
            locator = session.page.locator(target["selector"]).first
            await locator.scroll_into_view_if_needed()
            await locator.click()
            if clear_first:
                try:
                    await locator.fill(text)
                except Exception:
                    await session.page.keyboard.press("Control+A")
                    await session.page.keyboard.type(text, delay=self.settings.typing_delay_ms)
            else:
                await session.page.keyboard.type(text, delay=self.settings.typing_delay_ms)
            await self._settle(session.page)

        return await self._run_action(
            session,
            "type",
            {**target, "clear_first": clear_first, "text_preview": text[:80]},
            operation,
        )

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

    async def execute_decision(
        self,
        session_id: str,
        decision: BrowserActionDecision,
        *,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
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
        elif decision.action == "type":
            result = await self.type(
                session_id,
                selector=decision.selector,
                element_id=decision.element_id,
                text=decision.text or "",
                clear_first=decision.clear_first,
            )
        elif decision.action == "press":
            result = await self.press(session_id, decision.key or "")
        elif decision.action == "scroll":
            result = await self.scroll(session_id, decision.delta_x, decision.delta_y)
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
                self._assert_runtime_url_allowed(session.page.url)
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
                raise ValueError(
                    f"Action failed for {action_name}. Refresh observation and retry. Details: {exc}"
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
        elif action_name in {"click", "press", "scroll"}:
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
        elif action_name == "type":
            verified = bool({"active_element_changed", "text_excerpt_changed", "accessibility_focus_changed"} & set(signals))
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
            "last_action": session.last_action,
            "trace_path": str(session.trace_path),
        }

    async def _persist_session(self, session: BrowserSession, *, status: SessionStatus) -> None:
        summary = await self._session_summary(
            session,
            status=status,
            live=status == "active",
        )
        await self.session_store.upsert(SessionRecord.model_validate(summary))

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
        if action_name in {"navigate", "scroll"}:
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

    def _bounded_append(self, items: list[Any], value: Any, limit: int = 50) -> None:
        items.append(value)
        if len(items) > limit:
            del items[: len(items) - limit]

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
