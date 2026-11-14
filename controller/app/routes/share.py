from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from ..action_errors import SCREENSHOT_WITHHELD, BrowserActionError
from ..browser.services.observation import SCRUB_DISCLOSURE
from ..models import ShareSessionRequest
from ..rate_limits import SlidingWindowRateLimiter
from ..session_ownership import as_system
from ..session_share import (
    ShareAuditError,
    ShareRevocationStoreError,
    ensure_links_revocable,
    mint_share_token,
    revoke_share_links,
    token_digest,
)
from ..utils import error_text
from ._utils import OFF_LIST_PAGE_DETAIL, internal_error, log_hidden_cause, require_safe_segment

logger = logging.getLogger(__name__)

# Anyone who holds a link can poll it, with no API token: a link is answered this many polls in a window, and 429
# until the window has room again. The page polls every five seconds, so this leaves room for ten pages open on one
# link: they ask for 120 a minute, and more in one window when their polls arrive late. Their polls share the
# session's frames (observation.share_snapshot).
SHARE_POLL_LIMIT = 150
SHARE_POLL_WINDOW_SECONDS = 60
SHARE_POLL_LIMIT_DETAIL = "This share link is polled too often. Try again shortly."
# PII_SCRUB_SCREENSHOT_STRICT kept back a frame that could not be scrubbed. The viewer holds no API token: told that
# and no more, where the error's own text names the setting and how to turn it off.
SHARE_FRAME_WITHHELD_DETAIL = (
    "The frame is withheld: it could not be PII-scrubbed, and this controller shows no frame that was not."
)


def create_share_router(*, manager: Any, share_manager: Any, settings: Any) -> APIRouter:
    router = APIRouter()
    # Asked by the token's digest, for a token that is good: the limiter holds its keys for a window, and never a
    # token.
    poll_limiter = SlidingWindowRateLimiter(limit=SHARE_POLL_LIMIT, window_seconds=SHARE_POLL_WINDOW_SECONDS)

    def _shared_session_id(token: str) -> str:
        info = share_manager.token_info(token)
        if not info.get("valid"):
            raise HTTPException(status_code=403, detail="Invalid token")
        return require_safe_segment(str(info["session_id"]), field="session_id")

    async def _audit_first_view(token: str, session_id: str) -> None:
        """Called once a route has something of the session to show the token's holder, before it answers.

        The token is checked again on each side of the event. A route waits after its own check (a poll for its
        frame, as long as the session's agent holds the lock; the page for the session's lookup), and a first view
        waits for its event, and for any other token's before it: a link revoked or expired during either wait is
        refused as it is now, and one refused before its event is no view. One revoked while its event is written is
        refused with the event written.
        """
        _shared_session_id(token)
        try:
            await share_manager.record_view(manager.audit, token, session_id)
        except Exception:
            raise internal_error(
                logger, "the first view of a share link of session %s was not audited", session_id
            ) from None
        _shared_session_id(token)

    @router.post("/sessions/{session_id}/share")
    async def share_session(session_id: str, payload: ShareSessionRequest | None = None) -> dict[str, Any]:
        try:
            await manager.get_session(session_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown session") from None

        ttl_minutes = payload.ttl_minutes if payload is not None else 60
        try:
            return await mint_share_token(share_manager, manager.audit, session_id, ttl_seconds=ttl_minutes * 60)
        except ShareRevocationStoreError as cause:
            raise HTTPException(status_code=503, detail=error_text(cause)) from None
        except ShareAuditError:
            raise internal_error(
                logger,
                "the audit of the mint of a share link of session %s failed (its event may be in the log), and no "
                "link was handed out",
                session_id,
            ) from None
        except ValueError as cause:
            # create_token's refusal of the request; an audit that failed with a ValueError is a ShareAuditError.
            log_hidden_cause(logger, 400, cause, "share failed for session %s", session_id)
            raise HTTPException(status_code=400, detail="Invalid request") from None

    @router.delete("/sessions/{session_id}/share")
    async def revoke_session_share(session_id: str) -> dict[str, Any]:
        try:
            await ensure_links_revocable(manager, session_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown session") from None
        try:
            return await revoke_share_links(share_manager, manager.audit, session_id)
        except ShareRevocationStoreError as cause:
            # Its fixed text: the path and the cause are in the log, where the store wrote them.
            raise HTTPException(status_code=503, detail=error_text(cause)) from None

    # The /share/{token}/... routes are exempt from the bearer token
    # (app/middleware/http.py): the signed, expiring share token *is* the
    # credential, and the people a link is handed to do not hold the API token.
    # Before, every one of them got a 401, and the screenshot the page renders
    # came from /artifacts, which needs the API token too — so a share link only
    # ever worked for someone who did not need it. Because these routes are
    # unauthenticated, they return only what the viewer renders: not the full
    # observation (DOM text, interactables, console), and screenshots from this
    # session only, through the token rather than /artifacts.

    @router.get("/share/{token}/observe")
    async def shared_observe(token: str) -> dict[str, Any]:
        session_id = _shared_session_id(token)
        polls = await poll_limiter.evaluate(token_digest(token))
        if polls.exceeded:
            raise HTTPException(
                status_code=429,
                detail=SHARE_POLL_LIMIT_DETAIL,
                headers={"Retry-After": str(polls.retry_after_seconds)},
            )
        try:
            # The token is the credential, issued by someone who could use the
            # session; the viewer is no operator, so this reads as the system.
            # The page polls this, so it takes a frame, not a full observation.
            with as_system():
                observation = await manager.share_snapshot(session_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown session") from None
        except PermissionError as cause:
            # One the operating system raised carries an errno (a frame the disk refuses) and is no refused
            # page: the server's fault, logged.
            if cause.errno is not None:
                raise internal_error(logger, "shared observe failed") from None
            # A refused page, or one that moved while the frame was taken: refused as observe
            # refuses it, not a 500 whose log line names the host. Not logged as observe's is: the
            # shared page asks again every few seconds for as long as it is open.
            raise HTTPException(status_code=403, detail=OFF_LIST_PAGE_DETAIL) from None
        except BrowserActionError as cause:
            if cause.code != SCREENSHOT_WITHHELD:
                raise internal_error(logger, "shared observe failed") from None
            # No fault of the server's, and it comes back with every poll: not logged here. /readiness fails its
            # screenshot_scrubbing check for an OCR that cannot run, and a redaction that failed logged its own
            # warning.
            raise HTTPException(status_code=503, detail=SHARE_FRAME_WITHHELD_DETAIL) from None
        except Exception:
            raise internal_error(logger, "shared observe failed") from None
        await _audit_first_view(token, session_id)
        screenshot_path = observation.get("screenshot_path")
        answer = {
            "session": {"id": session_id},
            "url": observation.get("url"),
            "title": observation.get("title"),
            "screenshot_url": (
                f"/share/{token}/screenshots/{Path(str(screenshot_path)).name}" if screenshot_path else None
            ),
        }
        # Present only for a frame that went out as captured, as in every other screenshot's result: the page
        # says so above it.
        if SCRUB_DISCLOSURE in observation:
            answer[SCRUB_DISCLOSURE] = observation[SCRUB_DISCLOSURE]
        return answer

    @router.get("/share/{token}/screenshots/{name}")
    async def shared_screenshot(token: str, name: str) -> FileResponse:
        session_id = _shared_session_id(token)
        safe_name = require_safe_segment(name, field="name")
        if not safe_name.endswith(".png"):
            raise HTTPException(status_code=404, detail="Not found")
        session_dir = (Path(settings.artifact_root) / session_id).resolve()
        candidate = (session_dir / safe_name).resolve()
        if candidate.parent != session_dir or not candidate.is_file():
            raise HTTPException(status_code=404, detail="Not found")
        await _audit_first_view(token, session_id)
        return FileResponse(path=str(candidate), media_type="image/png", headers={"Cache-Control": "no-store"})

    @router.get("/share/{token}", response_class=HTMLResponse)
    async def shared_session_view(token: str) -> HTMLResponse:
        session_id = _shared_session_id(token)
        try:
            with as_system():
                await manager.get_session(session_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown session") from None
        await _audit_first_view(token, session_id)

        html = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Shared Session Observer</title>
    <style>
      :root {
        color-scheme: dark;
        --bg: #111418;
        --panel: #1a2027;
        --panel-border: #2b3440;
        --text: #f5f7fa;
        --muted: #9aa6b2;
        --accent: #7dd3fc;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        font-family: ui-sans-serif, system-ui, sans-serif;
        background:
          radial-gradient(circle at top, rgba(125, 211, 252, 0.16), transparent 28%),
          linear-gradient(180deg, #0b0f14, var(--bg));
        color: var(--text);
      }
      main {
        max-width: 1180px;
        margin: 0 auto;
        padding: 24px;
      }
      header {
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 18px;
      }
      h1 {
        margin: 0;
        font-size: 1.2rem;
      }
      .meta {
        color: var(--muted);
        font-size: 0.95rem;
      }
      .panel {
        background: rgba(26, 32, 39, 0.92);
        border: 1px solid var(--panel-border);
        border-radius: 18px;
        overflow: hidden;
        box-shadow: 0 18px 40px rgba(0, 0, 0, 0.24);
      }
      .toolbar {
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
        align-items: center;
        justify-content: space-between;
        padding: 14px 18px;
        border-bottom: 1px solid var(--panel-border);
      }
      .status {
        color: var(--muted);
        font-size: 0.95rem;
      }
      .status strong {
        color: var(--accent);
      }
      .frame {
        aspect-ratio: 16 / 10;
        width: 100%;
        background: #0b0f14;
        display: flex;
        align-items: center;
        justify-content: center;
      }
      img {
        width: 100%;
        height: 100%;
        object-fit: contain;
        display: block;
      }
      .error {
        padding: 24px;
        color: #fecaca;
      }
      .notice {
        padding: 10px 18px;
        border-bottom: 1px solid var(--panel-border);
        color: #fde68a;
      }
      a {
        color: var(--accent);
      }
    </style>
  </head>
  <body>
    <main>
      <header>
        <div>
          <h1>Shared Session Observer</h1>
          <div class="meta">Session <code id="session-id"></code></div>
        </div>
        <div class="meta" id="url">Waiting for first snapshot…</div>
      </header>
      <section class="panel">
        <div class="toolbar">
          <div class="status"><strong id="state">Connecting</strong> <span id="detail">Fetching shared observe payload…</span></div>
          <div class="meta" id="updated">Never updated</div>
        </div>
        <div class="notice" id="scrub" hidden></div>
        <div class="frame" id="frame">
          <div class="meta">Loading screenshot…</div>
        </div>
      </section>
    </main>
    <script>
      const token = window.location.pathname.split("/").filter(Boolean).pop() || "";
      const observeUrl = `/share/${token}/observe`;
      const imageEl = document.createElement("img");
      const frameEl = document.getElementById("frame");
      const stateEl = document.getElementById("state");
      const detailEl = document.getElementById("detail");
      const updatedEl = document.getElementById("updated");
      const urlEl = document.getElementById("url");
      const sessionIdEl = document.getElementById("session-id");
      const scrubEl = document.getElementById("scrub");
      const safeHttpUrl = (value) => {
        if (!value) return null;
        try {
          const parsed = new URL(String(value), window.location.origin);
          if (parsed.protocol === "http:" || parsed.protocol === "https:") return parsed.href;
        } catch (_) {}
        return null;
      };
      const setSnapshotUrl = (value) => {
        urlEl.replaceChildren();
        const href = safeHttpUrl(value);
        if (!href) {
          urlEl.textContent = "No URL available";
          return;
        }
        const link = document.createElement("a");
        link.href = href;
        link.target = "_blank";
        link.rel = "noreferrer";
        link.textContent = href;
        urlEl.appendChild(link);
      };
      const showFrameError = (message) => {
        const errorEl = document.createElement("div");
        errorEl.className = "error";
        errorEl.textContent = `Unable to refresh shared session: ${message}`;
        frameEl.replaceChildren(errorEl);
      };
      // Given for a frame that screenshot scrubbing is on for and could not scrub: it is shown as captured.
      const showScrubStatus = (scrub) => {
        scrubEl.hidden = !scrub;
        scrubEl.textContent = scrub
          ? `Not PII-scrubbed: this frame is shown as it was captured (${scrub.reason}).`
          : "";
      };
      // The answer's own text where it has one: a frame withheld because it could not be scrubbed says so.
      const failureText = async (response) => {
        let detail = null;
        try {
          detail = (await response.json()).detail;
        } catch (_) {}
        return typeof detail === "string" ? `${detail} (HTTP ${response.status})` : `HTTP ${response.status}`;
      };

      async function refresh() {
        try {
          const response = await fetch(observeUrl, { cache: "no-store" });
          if (!response.ok) {
            throw new Error(await failureText(response));
          }
          const payload = await response.json();
          const sessionId = payload.session && payload.session.id ? payload.session.id : "Shared session";
          sessionIdEl.textContent = sessionId;
          imageEl.src = `${payload.screenshot_url}?ts=${Date.now()}`;
          imageEl.alt = payload.title || payload.url || sessionId;
          showScrubStatus(payload.screenshot_pii_scrub);
          if (!imageEl.isConnected) {
            frameEl.replaceChildren(imageEl);
          }
          stateEl.textContent = "Live";
          detailEl.textContent = payload.title || "Shared observe payload loaded";
          setSnapshotUrl(payload.url);
          updatedEl.textContent = `Updated ${new Date().toLocaleTimeString()}`;
        } catch (error) {
          showFrameError(error.message);
          showScrubStatus(null);
          stateEl.textContent = "Error";
          detailEl.textContent = "Retrying every 5 seconds";
          updatedEl.textContent = `Last attempt ${new Date().toLocaleTimeString()}`;
        }
      }

      refresh();
      setInterval(refresh, 5000);
    </script>
  </body>
</html>"""
        return HTMLResponse(content=html)

    return router
