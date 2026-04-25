"""
routes.extensions — FastAPI route definitions for all 1.0 pillars.

Registers: /mesh, /network, /cdp, /social, /workflow, /dashboard
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ===========================================================================
# Pillar 1 — Mesh routes
# ===========================================================================

mesh_router = APIRouter(prefix="/mesh", tags=["mesh"])


class MeshReceiveRequest(BaseModel):
    sender_node_id: str
    recipient_node_id: str
    nonce: str
    timestamp: float
    payload: dict[str, Any]
    signature_b64: str


@mesh_router.post("/receive")
async def mesh_receive(body: MeshReceiveRequest, request: Request):
    """
    Inbound delegation endpoint. Peers POST signed envelopes here.
    Calls DelegationManager.receive_inbound() and returns the reply envelope.
    """
    app = request.app
    delegation_mgr = getattr(app.state, "delegation_manager", None)
    if delegation_mgr is None:
        raise HTTPException(503, "Mesh not initialized")

    from app.mesh import DelegationRejected, DelegationReplayError, SignedEnvelope, make_envelope

    envelope = SignedEnvelope(**body.model_dump())
    try:
        response = await delegation_mgr.receive_inbound(envelope)
    except (DelegationRejected, DelegationReplayError) as exc:
        raise HTTPException(403, str(exc))
    except Exception as exc:
        logger.error("mesh.receive error: %s", exc)
        raise HTTPException(500, str(exc))

    identity = app.state.mesh_identity
    reply_payload = response.model_dump(mode="json")
    reply_envelope = make_envelope(
        identity=identity,
        payload=reply_payload,
        recipient_node_id=envelope.sender_node_id,
    )
    return reply_envelope.model_dump()


@mesh_router.get("/peers")
async def mesh_list_peers(request: Request):
    peers = getattr(request.app.state, "peer_registry", None)
    if peers is None:
        raise HTTPException(503, "Mesh not initialized")
    return {"peers": [p.model_dump() for p in peers.all()]}


@mesh_router.post("/peers")
async def mesh_add_peer(body: dict[str, Any], request: Request):
    from app.mesh import PeerRecord
    peers = getattr(request.app.state, "peer_registry", None)
    if peers is None:
        raise HTTPException(503, "Mesh not initialized")
    try:
        peer = PeerRecord(**body)
    except Exception as exc:
        raise HTTPException(422, str(exc))
    peers.add(peer)
    return {"status": "added", "node_id": peer.node_id}


@mesh_router.delete("/peers/{node_id}")
async def mesh_remove_peer(node_id: str, request: Request):
    peers = getattr(request.app.state, "peer_registry", None)
    if peers is None:
        raise HTTPException(503, "Mesh not initialized")
    removed = peers.remove(node_id)
    if not removed:
        raise HTTPException(404, f"Peer {node_id!r} not found")
    return {"status": "removed", "node_id": node_id}


@mesh_router.get("/identity")
async def mesh_identity(request: Request):
    identity = getattr(request.app.state, "mesh_identity", None)
    if identity is None:
        raise HTTPException(503, "Mesh not initialized")
    return {"node_id": identity.node_id, "pubkey_b64": identity.pubkey_b64}


# ===========================================================================
# Pillar 3 — Network inspector routes
# ===========================================================================

network_router = APIRouter(prefix="/sessions/{session_id}/network", tags=["network"])


@network_router.get("/requests")
async def network_get_requests(
    session_id: str,
    request: Request,
    url_filter: str = "",
    method: str = "",
    resource_type: str = "",
    limit: int = 50,
):
    inspector = _get_inspector(request.app, session_id)
    if inspector is None:
        raise HTTPException(404, f"No network inspector for session {session_id!r}")
    entries = inspector.entries(limit=limit, method=method or None, url_contains=url_filter or None)
    if resource_type:
        entries = [entry for entry in entries if entry.get("resource_type") == resource_type]
    return {"requests": entries, "summary": inspector.summary()}


@network_router.post("/hooks")
async def network_register_hook(session_id: str, body: dict[str, Any], request: Request):
    inspector = _get_inspector(request.app, session_id)
    if inspector is None:
        raise HTTPException(404, f"No network inspector for session {session_id!r}")
    pattern = body.get("url_pattern", "")
    if not pattern:
        raise HTTPException(422, "url_pattern required")
    # Hooks via API are logged-only (no external callback for security)

    async def _log_hook(req: dict):
        logger.info("network.hook pattern=%r matched url=%r", pattern, req.get("url"))

    inspector.register_hook(pattern, _log_hook)
    return {"status": "registered", "pattern": pattern}


@network_router.get("/hooks")
async def network_list_hooks(session_id: str, request: Request):
    inspector = _get_inspector(request.app, session_id)
    if inspector is None:
        raise HTTPException(404, f"No network inspector for session {session_id!r}")
    return {"hooks": inspector.list_hooks()}


@network_router.delete("/hooks/{pattern}")
async def network_remove_hook(session_id: str, pattern: str, request: Request):
    inspector = _get_inspector(request.app, session_id)
    if inspector is None:
        raise HTTPException(404, f"No network inspector for session {session_id!r}")
    removed = inspector.remove_hook(pattern)
    return {"removed": removed, "pattern": pattern}


def _get_inspector(app, session_id: str):
    inspectors = getattr(app.state, "network_inspectors", {})
    inspector = inspectors.get(session_id)
    if inspector is not None:
        return inspector
    manager = getattr(app.state, "browser_manager", None)
    if manager is None:
        return None
    session = manager.sessions.get(session_id)
    return getattr(session, "network_inspector", None) if session is not None else None


# ===========================================================================
# Pillar 3 — CDP routes
# ===========================================================================

cdp_router = APIRouter(prefix="/sessions/{session_id}/cdp", tags=["cdp"])


@cdp_router.get("/element")
async def cdp_element_intelligence(session_id: str, selector: str, request: Request):
    cdp = _get_cdp(request.app, session_id)
    if cdp is None:
        raise HTTPException(404, f"No CDP session for {session_id!r}")
    result = await cdp.get_element_intelligence(selector)
    return result


@cdp_router.post("/raw")
async def cdp_raw_command(session_id: str, body: dict[str, Any], request: Request):
    cdp = _get_cdp(request.app, session_id)
    if cdp is None:
        raise HTTPException(404, f"No CDP session for {session_id!r}")
    method = body.get("method", "")
    params = body.get("params", {})
    try:
        result = await cdp.raw_cdp_command(method, params)
    except ValueError as exc:
        raise HTTPException(403, str(exc))
    return result


def _get_cdp(app, session_id: str):
    cdps = getattr(app.state, "cdp_sessions", {})
    return cdps.get(session_id)


# ===========================================================================
# Pillar 5 — Workflow routes
# ===========================================================================

workflow_router = APIRouter(prefix="/workflows", tags=["workflows"])


class WorkflowRunRequest(BaseModel):
    workflow_id: str
    steps: list[dict[str, Any]]
    initial_context: dict[str, Any] = {}


@workflow_router.post("/run")
async def workflow_run(body: WorkflowRunRequest, request: Request):
    engine = getattr(request.app.state, "workflow_engine", None)
    if engine is None:
        raise HTTPException(503, "Workflow engine not initialized")
    run = await engine.run(
        workflow_id=body.workflow_id,
        steps=body.steps,
        initial_context=body.initial_context,
    )
    return {
        "run_id": run.run_id,
        "status": run.status.value,
        "step_statuses": {k: v.value for k, v in run.step_statuses.items()},
        "context": run.context,
        "error": run.error,
    }


@workflow_router.get("/runs")
async def workflow_list_runs(request: Request, workflow_id: str = ""):
    engine = getattr(request.app.state, "workflow_engine", None)
    if engine is None:
        raise HTTPException(503, "Workflow engine not initialized")
    return {"runs": engine.list_runs(workflow_id=workflow_id)}


@workflow_router.get("/runs/{run_id}")
async def workflow_get_run(run_id: str, request: Request):
    engine = getattr(request.app.state, "workflow_engine", None)
    if engine is None:
        raise HTTPException(503, "Workflow engine not initialized")
    runs = engine.list_runs()
    for run in runs:
        if run.get("run_id") == run_id:
            return run
    raise HTTPException(404, f"Run {run_id!r} not found")


# ===========================================================================
# Pillar 5 — Social empire routes
# ===========================================================================

social_router = APIRouter(prefix="/social/empire", tags=["social-empire"])


class ViralResearchRequest(BaseModel):
    niche: str
    subreddits: list[str] = []
    yt_results: int = 20


class VideoGenerateRequest(BaseModel):
    prompt: str
    output_filename: str = ""
    duration_seconds: int = 8
    aspect_ratio: str = "16:9"


class YouTubeUploadRequest(BaseModel):
    file_path: str
    title: str
    description: str = ""
    tags: list[str] = []
    privacy: str = "public"
    make_short: bool = False


class CrossPostRequest(BaseModel):
    video_url: str
    title: str
    description: str = ""
    platforms: list[str] = ["reddit", "x"]
    subreddits: list[str] = ["videos"]


@social_router.post("/research")
async def social_research(body: ViralResearchRequest, request: Request):
    engine = getattr(request.app.state, "viral_engine", None)
    if engine is None:
        raise HTTPException(503, "Viral research engine not initialized")
    result = await engine.research(
        niche=body.niche,
        subreddits=body.subreddits,
        yt_results=body.yt_results,
    )
    return result


@social_router.post("/generate")
async def social_generate_video(body: VideoGenerateRequest, request: Request):
    veo3 = getattr(request.app.state, "veo3_client", None)
    if veo3 is None:
        raise HTTPException(503, "Veo3 client not initialized")
    output = body.output_filename or f"/data/social/generated/{uuid.uuid4().hex}.mp4"
    path = await veo3.generate(
        prompt=body.prompt,
        output_path=output,
        duration_seconds=body.duration_seconds,
        aspect_ratio=body.aspect_ratio,
    )
    return {"status": "generated", "path": path, "prompt": body.prompt}


@social_router.post("/youtube/upload")
async def social_youtube_upload(body: YouTubeUploadRequest, request: Request):
    yt = getattr(request.app.state, "youtube_client", None)
    if yt is None:
        raise HTTPException(503, "YouTube client not initialized")
    if body.make_short:
        result = await yt.create_short(
            file_path=body.file_path,
            title=body.title,
            description=body.description,
            tags=body.tags,
        )
    else:
        result = await yt.upload_video(
            file_path=body.file_path,
            title=body.title,
            description=body.description,
            tags=body.tags,
            privacy=body.privacy,
        )
    return {"status": "uploaded", "video_id": result.get("id"), "result": result}


@social_router.post("/crosspost")
async def social_crosspost(body: CrossPostRequest, request: Request):
    results = {}
    if "reddit" in body.platforms:
        reddit = getattr(request.app.state, "reddit_client", None)
        if reddit:
            for sr in (body.subreddits or ["videos"]):
                try:
                    r = await reddit.submit_link(sr, body.title, body.video_url)
                    results[f"reddit/{sr}"] = r
                except Exception as exc:
                    results[f"reddit/{sr}"] = {"error": str(exc)}
    if "x" in body.platforms:
        x = getattr(request.app.state, "x_client", None)
        if x:
            try:
                r = await x.post_tweet(f"{body.title}\n\n{body.video_url}")
                results["x"] = r
            except Exception as exc:
                results["x"] = {"error": str(exc)}
    if "instagram" in body.platforms:
        ig = getattr(request.app.state, "instagram_client", None)
        if ig:
            try:
                r = await ig.post_reel(body.video_url, body.description)
                results["instagram"] = r
            except Exception as exc:
                results["instagram"] = {"error": str(exc)}
    return {"status": "crossposted", "results": results}


# ===========================================================================
# Pillar 6 — Operator Dashboard
# ===========================================================================

dashboard_router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Auto Browser — Operator Dashboard</title>
<script src="https://unpkg.com/htmx.org@1.9.12"></script>
<style>
  :root { --bg: #0f1117; --surface: #1a1d27; --border: #2a2d3a; --accent: #6c63ff;
          --text: #e2e8f0; --muted: #64748b; --green: #10b981; --red: #ef4444; --yellow: #f59e0b; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Inter', system-ui, sans-serif;
         font-size: 14px; line-height: 1.5; }
  header { background: var(--surface); border-bottom: 1px solid var(--border);
           padding: 12px 24px; display: flex; align-items: center; gap: 16px; }
  header h1 { font-size: 18px; font-weight: 600; color: var(--accent); }
  header .node-id { color: var(--muted); font-size: 12px; font-family: monospace; }
  nav { display: flex; gap: 2px; padding: 0 24px; background: var(--surface);
        border-bottom: 1px solid var(--border); }
  nav a { padding: 10px 16px; color: var(--muted); text-decoration: none; font-size: 13px;
          border-bottom: 2px solid transparent; }
  nav a.active, nav a:hover { color: var(--text); border-color: var(--accent); }
  main { padding: 24px; max-width: 1200px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 20px; }
  .card h3 { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }
  .card .value { font-size: 32px; font-weight: 700; }
  .section { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; margin-bottom: 16px; }
  .section-header { padding: 16px 20px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; }
  .section-header h2 { font-size: 15px; font-weight: 600; }
  table { width: 100%; border-collapse: collapse; }
  th { padding: 10px 20px; text-align: left; font-size: 11px; color: var(--muted);
       text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid var(--border); }
  td { padding: 12px 20px; border-bottom: 1px solid var(--border); font-size: 13px; }
  tr:last-child td { border-bottom: none; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 500; }
  .badge-green { background: rgba(16,185,129,.15); color: var(--green); }
  .badge-red { background: rgba(239,68,68,.15); color: var(--red); }
  .badge-yellow { background: rgba(245,158,11,.15); color: var(--yellow); }
  .badge-gray { background: rgba(100,116,139,.15); color: var(--muted); }
  .mono { font-family: monospace; font-size: 12px; }
  .btn { padding: 6px 14px; border-radius: 6px; border: none; cursor: pointer; font-size: 13px; font-weight: 500; }
  .btn-primary { background: var(--accent); color: white; }
  .btn-danger { background: var(--red); color: white; }
  .btn-sm { padding: 3px 10px; font-size: 12px; }
  .empty { padding: 32px; text-align: center; color: var(--muted); }
  .refresh-btn { background: none; border: 1px solid var(--border); color: var(--muted);
                 padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 12px; }
  .refresh-btn:hover { color: var(--text); }
</style>
</head>
<body>
<header>
  <h1>⚡ Auto Browser</h1>
  <span class="node-id" id="node-id">Loading node identity...</span>
</header>
<nav>
  <a href="#sessions" class="active">Sessions</a>
  <a href="#workflows">Workflows</a>
  <a href="#peers">Peers</a>
  <a href="#audit">Audit Log</a>
</nav>
<main>
  <!-- Stats -->
  <div class="grid" id="stats">
    <div class="card"><h3>Active Sessions</h3><div class="value" id="stat-sessions">—</div></div>
    <div class="card"><h3>Workflow Runs</h3><div class="value" id="stat-workflows">—</div></div>
    <div class="card"><h3>Mesh Peers</h3><div class="value" id="stat-peers">—</div></div>
    <div class="card"><h3>Audit Events</h3><div class="value" id="stat-audit">—</div></div>
  </div>

  <!-- Sessions -->
  <div class="section" id="sessions">
    <div class="section-header">
      <h2>Sessions</h2>
      <button class="refresh-btn" onclick="loadSessions()">↻ Refresh</button>
    </div>
    <table><thead><tr>
      <th>Session ID</th><th>Name</th><th>Status</th><th>Operator</th><th>URL</th><th>Actions</th>
    </tr></thead><tbody id="sessions-tbody"><tr><td colspan="6" class="empty">Loading...</td></tr></tbody></table>
  </div>

  <!-- Workflows -->
  <div class="section" id="workflows">
    <div class="section-header">
      <h2>Workflow Runs</h2>
      <button class="refresh-btn" onclick="loadWorkflows()">↻ Refresh</button>
    </div>
    <table><thead><tr>
      <th>Run ID</th><th>Workflow</th><th>Status</th><th>Started</th><th>Duration</th>
    </tr></thead><tbody id="workflows-tbody"><tr><td colspan="5" class="empty">Loading...</td></tr></tbody></table>
  </div>

  <!-- Peers -->
  <div class="section" id="peers">
    <div class="section-header">
      <h2>Mesh Peers</h2>
      <button class="refresh-btn" onclick="loadPeers()">↻ Refresh</button>
    </div>
    <table><thead><tr>
      <th>Node ID</th><th>Display Name</th><th>Endpoint</th><th>Last Seen</th><th>Grants</th><th>Actions</th>
    </tr></thead><tbody id="peers-tbody"><tr><td colspan="6" class="empty">No peers registered</td></tr></tbody></table>
  </div>

  <!-- Audit -->
  <div class="section" id="audit">
    <div class="section-header">
      <h2>Audit Log</h2>
      <button class="refresh-btn" onclick="loadAudit()">↻ Refresh</button>
    </div>
    <table><thead><tr>
      <th>Time</th><th>Operator</th><th>Action</th><th>Session</th><th>Status</th>
    </tr></thead><tbody id="audit-tbody"><tr><td colspan="5" class="empty">Loading...</td></tr></tbody></table>
  </div>
</main>

<script>
// --- Auth bootstrap --------------------------------------------------------
// The parent server applies bearer-token middleware to API routes, while the
// dashboard page itself bootstraps credentials in-browser. Token and operator
// identity are stored in sessionStorage only and cleared when the tab closes.
// Supports #token=... in the URL hash for one-click bookmarkable access.
(function initAuthState() {
  const h = window.location.hash || '';
  const m = h.match(/token=([^&]+)/);
  if (m) {
    sessionStorage.setItem('ab_token', decodeURIComponent(m[1]));
    history.replaceState(null, '', window.location.pathname);  // strip from URL
  }
  if (!sessionStorage.getItem('ab_token')) {
    const t = prompt('Enter API bearer token (leave blank for dev/no-auth):', '');
    if (t !== null) sessionStorage.setItem('ab_token', t);
  }
  if (!sessionStorage.getItem('ab_operator_id')) {
    const operatorId = prompt('Enter operator id for audit attribution:', '');
    if (operatorId !== null) sessionStorage.setItem('ab_operator_id', operatorId);
  }
  if (!sessionStorage.getItem('ab_operator_name')) {
    const operatorName = prompt('Enter operator name (optional):', '');
    if (operatorName !== null) sessionStorage.setItem('ab_operator_name', operatorName);
  }
})();

const _authHeaders = () => {
  const headers = {};
  const t = sessionStorage.getItem('ab_token') || '';
  const operatorId = sessionStorage.getItem('ab_operator_id') || '';
  const operatorName = sessionStorage.getItem('ab_operator_name') || '';
  if (t) headers['Authorization'] = 'Bearer ' + t;
  if (operatorId) headers['__OPERATOR_ID_HEADER__'] = operatorId;
  if (operatorName) headers['__OPERATOR_NAME_HEADER__'] = operatorName;
  return headers;
};
const api = (path) =>
  fetch(path, {headers: _authHeaders()})
    .then(async r => {
      if (r.status === 401) {
        sessionStorage.removeItem('ab_token');
        alert('Auth failed — token cleared. Reload and try again.');
        throw new Error('unauthorized');
      }
      if (r.status === 400) {
        const body = await r.json().catch(() => ({}));
        if ((body.detail || '').includes('operator header')) {
          sessionStorage.removeItem('ab_operator_id');
          sessionStorage.removeItem('ab_operator_name');
          alert('Operator identity missing. Reload and provide an operator id.');
        }
        throw new Error('bad_request');
      }
      return r.json();
    })
    .catch(e => ({}));
const asText = (value, fallback = '—') => {
  if (value === null || value === undefined || value === '') return fallback;
  return String(value);
};
const statusBadge = (s) => {
  const map = {active:'green', running:'green', completed:'green', ok:'green',
                failed:'red', error:'red', rejected:'red',
                pending:'yellow', approval_required:'yellow',
                interrupted:'gray', closed:'gray'};
  const label = asText(s, 'unknown');
  const span = document.createElement('span');
  span.className = `badge badge-${map[label] || 'gray'}`;
  span.textContent = label;
  return span;
};
const timeAgo = (ts) => {
  if (!ts) return '—';
  const s = Math.floor(Date.now()/1000 - ts);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s/60)}m ago`;
  return `${Math.floor(s/3600)}h ago`;
};
const formatEventTime = (ts) => {
  if (!ts) return '—';
  const numeric = Number(ts);
  const date = Number.isFinite(numeric) ? new Date(numeric * 1000) : new Date(String(ts));
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleTimeString();
};
const appendCell = (row, value, options = {}) => {
  const td = document.createElement('td');
  if (options.className) td.className = options.className;
  if (options.maxWidth) {
    td.style.maxWidth = options.maxWidth;
    td.style.overflow = 'hidden';
    td.style.textOverflow = 'ellipsis';
  }
  td.textContent = asText(value);
  row.appendChild(td);
  return td;
};
const appendNodeCell = (row, node) => {
  const td = document.createElement('td');
  td.appendChild(node);
  row.appendChild(td);
  return td;
};
const appendEmptyRow = (tbody, colspan, message) => {
  tbody.replaceChildren();
  const row = document.createElement('tr');
  const cell = document.createElement('td');
  cell.colSpan = colspan;
  cell.className = 'empty';
  cell.textContent = message;
  row.appendChild(cell);
  tbody.appendChild(row);
};
const safeHttpUrl = (value) => {
  if (!value) return null;
  try {
    const parsed = new URL(String(value), window.location.origin);
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') return parsed.href;
  } catch (_) {}
  return null;
};

async function loadAll() {
  await Promise.all([loadIdentity(), loadSessions(), loadWorkflows(), loadPeers(), loadAudit()]);
}

async function loadIdentity() {
  const d = await api('/mesh/identity');
  document.getElementById('node-id').textContent = `Node: ${(d.node_id||'').slice(0,12)}...`;
}

async function loadSessions() {
  const d = await api('/sessions');
  const sessions = d.sessions || d || [];
  document.getElementById('stat-sessions').textContent = sessions.filter?.(s=>s.status==='active').length||0;
  const tbody = document.getElementById('sessions-tbody');
  if (!sessions.length) { appendEmptyRow(tbody, 6, 'No sessions'); return; }
  tbody.replaceChildren();
  sessions.forEach(s => {
    const row = document.createElement('tr');
    const sessionId = asText(s.session_id || s.id, '').slice(0, 12);
    appendCell(row, sessionId ? `${sessionId}...` : '—', {className: 'mono'});
    appendCell(row, s.name);
    appendNodeCell(row, statusBadge(s.status || 'unknown'));
    appendCell(row, s.operator_id);
    appendCell(row, s.current_url || s.start_url, {className: 'mono', maxWidth: '200px'});
    const actionCell = document.createElement('td');
    const href = safeHttpUrl(s.takeover_url);
    if (href) {
      const link = document.createElement('a');
      link.href = href;
      link.target = '_blank';
      link.rel = 'noreferrer';
      link.style.color = 'var(--accent)';
      link.style.textDecoration = 'none';
      link.textContent = 'Open noVNC';
      actionCell.appendChild(link);
    } else {
      actionCell.textContent = '—';
    }
    row.appendChild(actionCell);
    tbody.appendChild(row);
  });
}

async function loadWorkflows() {
  const d = await api('/workflows/runs');
  const runs = d.runs || [];
  document.getElementById('stat-workflows').textContent = runs.length;
  const tbody = document.getElementById('workflows-tbody');
  if (!runs.length) { appendEmptyRow(tbody, 5, 'No runs yet'); return; }
  tbody.replaceChildren();
  runs.slice(0,50).forEach(r => {
    const dur = r.finished_at && r.started_at ? `${((r.finished_at-r.started_at)).toFixed(1)}s` : '—';
    const row = document.createElement('tr');
    const runId = asText(r.run_id, '').slice(0, 12);
    appendCell(row, runId ? `${runId}...` : '—', {className: 'mono'});
    appendCell(row, r.workflow_id);
    appendNodeCell(row, statusBadge(r.status || 'unknown'));
    appendCell(row, timeAgo(r.started_at));
    appendCell(row, dur);
    tbody.appendChild(row);
  });
}

async function loadPeers() {
  const d = await api('/mesh/peers');
  const peers = d.peers || [];
  document.getElementById('stat-peers').textContent = peers.length;
  const tbody = document.getElementById('peers-tbody');
  if (!peers.length) { appendEmptyRow(tbody, 6, 'No peers registered'); return; }
  tbody.replaceChildren();
  peers.forEach(p => {
    const row = document.createElement('tr');
    const nodeId = asText(p.node_id, '').slice(0, 16);
    appendCell(row, nodeId ? `${nodeId}...` : '—', {className: 'mono'});
    appendCell(row, p.display_name);
    appendCell(row, p.endpoint, {className: 'mono'});
    appendCell(row, timeAgo(p.last_seen));
    appendCell(row, `${(p.grants||[]).length} grants`);
    const actionCell = document.createElement('td');
    const button = document.createElement('button');
    button.className = 'btn btn-danger btn-sm';
    button.type = 'button';
    button.textContent = 'Remove';
    button.addEventListener('click', () => removePeer(asText(p.node_id, '')));
    actionCell.appendChild(button);
    row.appendChild(actionCell);
    tbody.appendChild(row);
  });
}

async function removePeer(nodeId) {
  if (!confirm('Remove peer ' + nodeId + '?')) return;
  await fetch('/mesh/peers/' + encodeURIComponent(nodeId), {method:'DELETE', headers: _authHeaders()});
  loadPeers();
}

async function loadAudit() {
  const d = await api('/audit/events?limit=50');
  const events = d.events || d || [];
  if (Array.isArray(events)) document.getElementById('stat-audit').textContent = events.length;
  const tbody = document.getElementById('audit-tbody');
  if (!events.length) { appendEmptyRow(tbody, 5, 'No audit events'); return; }
  tbody.replaceChildren();
  events.slice(0,50).forEach(e => {
    const row = document.createElement('tr');
    appendCell(row, formatEventTime(e.timestamp), {className: 'mono'});
    appendCell(row, e.operator_id);
    appendCell(row, e.action || e.event_type);
    appendCell(row, asText(e.session_id, '').slice(0, 8) || '—', {className: 'mono'});
    appendNodeCell(row, statusBadge(e.status || 'ok'));
    tbody.appendChild(row);
  });
}

// Auto-refresh every 15s
loadAll();
setInterval(loadAll, 15000);
</script>
</body>
</html>"""


@dashboard_router.get("", response_class=HTMLResponse)
async def dashboard_root(request: Request):
    html = _DASHBOARD_HTML.replace("__OPERATOR_ID_HEADER__", request.app.state.settings.operator_id_header)
    html = html.replace("__OPERATOR_NAME_HEADER__", request.app.state.settings.operator_name_header)
    if "__OPERATOR_ID_HEADER__" in html or "__OPERATOR_NAME_HEADER__" in html:
        raise HTTPException(500, "dashboard header placeholder rendering failed")
    return HTMLResponse(html)


# ===========================================================================
# Registration helper
# ===========================================================================

def register_all_routers(app) -> None:
    """Call from main.py startup to register all 1.0 routers."""
    app.include_router(mesh_router)
    app.include_router(network_router)
    app.include_router(cdp_router)
    app.include_router(workflow_router)
    app.include_router(social_router)
    app.include_router(dashboard_router)
    logger.info("routes.extensions: all 1.0 routers registered")
