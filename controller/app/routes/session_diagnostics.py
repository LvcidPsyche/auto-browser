from __future__ import annotations

import asyncio
import html as _html
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.responses import StreamingResponse

from .. import events as _events
from ..playwright_export import export_session_script
from ._utils import internal_error, require_safe_segment

logger = logging.getLogger(__name__)


def create_session_diagnostics_router(*, manager: Any, settings: Any) -> APIRouter:
    router = APIRouter()

    @router.get("/sessions/{session_id}/events")
    async def session_events(session_id: str, request: Request):
        await manager.get_session(session_id)

        queue = _events.subscribe(session_id)

        async def event_generator():
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        payload = await asyncio.wait_for(
                            queue.get(),
                            timeout=settings.sse_keepalive_seconds,
                        )
                        yield f"data: {payload}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                _events.unsubscribe(session_id, queue)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @router.post("/sessions/{session_id}/screenshot/compare")
    async def screenshot_compare(session_id: str) -> dict[str, Any]:
        try:
            return await manager.screenshot_diff(session_id)
        except Exception:
            raise internal_error(logger, "screenshot diff failed for session %s", session_id) from None

    @router.get("/sessions/{session_id}/replay", response_class=HTMLResponse)
    async def session_replay(session_id: str) -> HTMLResponse:
        safe_session_id = require_safe_segment(session_id, field="session_id")

        artifact_root = Path(settings.artifact_root).resolve()
        artifact_dir: Path | None = None
        if artifact_root.is_dir():
            for child in artifact_root.iterdir():
                if child.name == safe_session_id and child.is_dir():
                    candidate = child.resolve()
                    if candidate.is_relative_to(artifact_root):
                        artifact_dir = candidate
                    break
        screenshots: list[tuple[str, str]] = []
        if artifact_dir is not None:
            for f in sorted(artifact_dir.glob("*.png")):
                label = f.stem.replace("-", " ")
                screenshots.append((f"/artifacts/{safe_session_id}/{f.name}", label))

        try:
            events = await manager.list_audit_events(session_id=safe_session_id, limit=200)
        except Exception:
            logger.debug("failed to load replay audit events for session %s", safe_session_id, exc_info=True)
            events = []

        session_info: dict[str, Any] = {}
        try:
            session = manager.sessions.get(safe_session_id)
            if session:
                session_info = await manager._session_summary(session)
            else:
                record = await manager.session_store.get(safe_session_id)
                session_info = record.model_dump()
        except Exception:
            logger.debug("failed to load replay session info for session %s", safe_session_id, exc_info=True)
            pass

        def esc(s: object) -> str:
            return _html.escape(str(s or ""))

        screenshots_html = "".join(
            f'<figure><img src="{esc(url)}" loading="lazy"><figcaption>{esc(lbl)}</figcaption></figure>'
            for url, lbl in screenshots
        ) or "<p class=muted>No screenshots captured yet.</p>"

        events_html = "".join(
            f'<tr><td class=muted>{esc(e.get("timestamp","")[:19])}</td>'
            f'<td>{esc(e.get("event_type",""))}</td>'
            f'<td>{esc(e.get("operator_id",""))}</td>'
            f'<td>{esc(str(e.get("data",""))[:120])}</td></tr>'
            for e in events
        ) or '<tr><td colspan=4 class=muted>No audit events.</td></tr>'

        status = esc(session_info.get("status", "unknown"))
        current_url = esc(session_info.get("url", ""))
        title = esc(session_info.get("title", session_id))
        created = esc(str(session_info.get("created_at", ""))[:19])

        body = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Replay — {esc(session_id)}</title>
<style>
  :root {{--bg:#0d1117;--surface:#161b22;--border:#30363d;--text:#c9d1d9;--muted:#8b949e;--accent:#58a6ff}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;padding:24px}}
  h1{{font-size:18px;font-weight:600;margin-bottom:4px}}
  h2{{font-size:14px;font-weight:600;margin:24px 0 12px;border-bottom:1px solid var(--border);padding-bottom:6px}}
  .meta{{color:var(--muted);font-size:12px;margin-bottom:20px}}
  .meta span{{margin-right:16px}}
  .gallery{{display:flex;flex-wrap:wrap;gap:12px}}
  figure{{background:var(--surface);border:1px solid var(--border);border-radius:6px;overflow:hidden;max-width:340px}}
  figure img{{width:100%;display:block}}
  figcaption{{font-size:11px;color:var(--muted);padding:6px 8px}}
  table{{width:100%;border-collapse:collapse;font-size:12px}}
  th{{text-align:left;padding:6px 8px;color:var(--muted);border-bottom:1px solid var(--border);font-weight:500}}
  td{{padding:6px 8px;border-bottom:1px solid var(--border);vertical-align:top;word-break:break-word}}
  .muted{{color:var(--muted)}}
  a{{color:var(--accent);text-decoration:none}}
</style>
</head>
<body>
<h1>Session Replay</h1>
<div class="meta">
  <span>ID: <strong>{esc(safe_session_id)}</strong></span>
  <span>Status: <strong>{status}</strong></span>
  <span>Created: {created}</span>
  <span>Title: {title}</span>
  {f'<span>URL: <a href="{current_url}" target="_blank">{current_url}</a></span>' if current_url else ''}
</div>
<h2>Screenshots ({len(screenshots)})</h2>
<div class="gallery">{screenshots_html}</div>
<h2>Audit Events ({len(events)})</h2>
<table>
  <thead><tr><th>Time</th><th>Type</th><th>Operator</th><th>Data</th></tr></thead>
  <tbody>{events_html}</tbody>
</table>
</body>
</html>"""
        return HTMLResponse(content=body)

    @router.get("/sessions/{session_id}/network-log")
    async def get_network_log(
        session_id: str,
        limit: int = 100,
        method: str | None = None,
        url_contains: str | None = None,
    ) -> dict[str, Any]:
        return await manager.get_network_log(
            session_id, limit=limit, method=method, url_contains=url_contains
        )

    @router.post("/sessions/{session_id}/shadow-browse")
    async def enable_shadow_browse(session_id: str) -> dict[str, Any]:
        try:
            return await manager.enable_shadow_browse(session_id)
        except RuntimeError:
            raise HTTPException(status_code=400, detail="Invalid request") from None

    @router.get("/sessions/{session_id}/audit")
    async def get_session_audit(
        session_id: str,
        limit: int = 200,
        event_type: str | None = None,
    ) -> dict[str, Any]:
        events = await manager.audit.list(
            session_id=session_id,
            limit=min(limit, 5000),
            event_type=event_type or None,
        )
        return {
            "session_id": session_id,
            "count": len(events),
            "events": [e.model_dump() for e in events],
        }

    @router.get("/sessions/{session_id}/witness")
    async def get_session_witness(session_id: str, limit: int = 100) -> dict[str, Any]:
        receipts = await manager.list_witness_receipts(session_id, limit=min(limit, 5000))
        return {
            "session_id": session_id,
            "count": len(receipts),
            "receipts": receipts,
        }

    @router.get("/sessions/{session_id}/witness/verify")
    async def verify_session_witness(session_id: str) -> dict[str, Any]:
        return await manager.verify_witness_chain(session_id)

    @router.get("/sessions/{session_id}/export-script")
    async def export_script(session_id: str) -> dict[str, Any]:
        session = await manager.get_session(session_id)
        return await export_session_script(
            session_id,
            manager.audit,
            start_url=session.page.url,
            viewport_w=settings.default_viewport_width,
            viewport_h=settings.default_viewport_height,
        )

    @router.get("/sessions/{session_id}/trace")
    async def get_trace(session_id: str) -> dict[str, Any]:
        session = await manager.get_session(session_id)
        trace_path = Path(str(session.trace_path)) if hasattr(session, "trace_path") else None
        if trace_path and trace_path.exists():
            return {
                "session_id": session_id,
                "trace_path": str(trace_path),
                "trace_url": f"/artifacts/{session_id}/{trace_path.name}",
                "trace_size_bytes": trace_path.stat().st_size,
                "viewer_url": f"https://trace.playwright.dev/?trace=/artifacts/{session_id}/{trace_path.name}",
            }
        return {"session_id": session_id, "trace_path": None, "trace_url": None, "viewer_url": None}

    return router
