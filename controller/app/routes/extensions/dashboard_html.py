"""Operator dashboard HTML template (served by routes.extensions.dashboard)."""

from __future__ import annotations

import json

from ...approval_display import APPROVAL_DISPLAY

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Auto Browser — Operator Dashboard</title>
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
  .btn-row { display: flex; gap: 6px; flex-wrap: wrap; }
  .timeline { display: flex; flex-direction: column; gap: 4px; max-width: 280px; }
  .timeline-item { color: var(--muted); font-size: 12px; }
  .timeline-item strong { color: var(--text); font-weight: 600; }
  .empty { padding: 32px; text-align: center; color: var(--muted); }
  .section-note { padding: 10px 20px; color: var(--muted); font-size: 12px; border-bottom: 1px solid var(--border); }
  .wrap { white-space: pre-wrap; word-break: break-word; max-width: 360px; }
  .arguments-viewer pre { margin: 0; padding: 12px 20px; max-height: 50vh; overflow: auto;
                          white-space: pre-wrap; word-break: break-word; }
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
  <a href="#approvals">Approvals</a>
  <a href="#agent-jobs">Agent Jobs</a>
  <a href="#peers">Peers</a>
  <a href="#audit">Audit Log</a>
</nav>
<main>
  <!-- Stats -->
  <div class="grid" id="stats">
    <div class="card"><h3>Active Sessions</h3><div class="value" id="stat-sessions">—</div></div>
    <div class="card"><h3>Pending Approvals</h3><div class="value" id="stat-approvals">—</div></div>
    <div class="card"><h3>Agent Jobs</h3><div class="value" id="stat-agent-jobs">—</div></div>
    <div class="card"><h3>Mesh Peers</h3><div class="value" id="stat-peers">—</div></div>
    <div class="card"><h3>Audit Events</h3><div class="value" id="stat-audit">—</div></div>
  </div>

  <!-- Pending approvals: an agent blocked on one waits here for an operator -->
  <div class="section" id="approvals">
    <div class="section-header">
      <h2>Pending Approvals</h2>
      <button class="refresh-btn" id="refresh-approvals">↻ Refresh</button>
    </div>
    <div class="section-note" id="approvals-status">Once approved, the waiting agent retries its action with the approval id.</div>
    <table><thead><tr>
      <th>Requested</th><th>Approval</th><th>Session</th><th>Kind</th><th>Action</th><th>Reason</th><th>Decision</th>
    </tr></thead><tbody id="approvals-tbody"><tr><td colspan="7" class="empty">Loading...</td></tr></tbody></table>
    <div class="arguments-viewer" id="approval-arguments" hidden>
      <div class="section-header">
        <h2 id="approval-arguments-title">Arguments</h2>
        <button class="refresh-btn" id="approval-arguments-close" type="button">Close</button>
      </div>
      <pre class="mono" id="approval-arguments-text"></pre>
    </div>
  </div>

  <!-- Sessions -->
  <div class="section" id="sessions">
    <div class="section-header">
      <h2>Sessions</h2>
      <button class="refresh-btn" id="refresh-sessions">↻ Refresh</button>
    </div>
    <table><thead><tr>
      <th>Session ID</th><th>Name</th><th>Status</th><th>Operator</th><th>URL</th><th>Actions</th>
    </tr></thead><tbody id="sessions-tbody"><tr><td colspan="6" class="empty">Loading...</td></tr></tbody></table>
  </div>

  <!-- Agent Jobs -->
  <div class="section" id="agent-jobs">
    <div class="section-header">
      <h2>Agent Jobs</h2>
      <button class="refresh-btn" id="refresh-agent-jobs">↻ Refresh</button>
    </div>
    <div class="section-note" id="agent-jobs-status"></div>
    <table><thead><tr>
      <th>Job ID</th><th>Kind</th><th>Status</th><th>Profile</th><th>Checkpoints</th><th>Timeline</th><th>Actions</th>
    </tr></thead><tbody id="agent-jobs-tbody"><tr><td colspan="7" class="empty">Loading...</td></tr></tbody></table>
  </div>

  <!-- Peers -->
  <div class="section" id="peers">
    <div class="section-header">
      <h2>Mesh Peers</h2>
      <button class="refresh-btn" id="refresh-peers">↻ Refresh</button>
    </div>
    <div class="section-note">Peers are listed here and managed in the peers file on the host (MESH_PEERS_PATH).</div>
    <table><thead><tr>
      <th>Node ID</th><th>Display Name</th><th>Endpoint</th><th>Last Seen</th><th>Grants</th>
    </tr></thead><tbody id="peers-tbody"><tr><td colspan="5" class="empty">No peers registered</td></tr></tbody></table>
  </div>

  <!-- Audit -->
  <div class="section" id="audit">
    <div class="section-header">
      <h2>Audit Log</h2>
      <button class="refresh-btn" id="refresh-audit">↻ Refresh</button>
    </div>
    <table><thead><tr>
      <th>Time</th><th>Operator</th><th>Action</th><th>Session</th><th>Status</th>
    </tr></thead><tbody id="audit-tbody"><tr><td colspan="5" class="empty">Loading...</td></tr></tbody></table>
  </div>

  <div class="section" id="replay">
    <div class="section-header">
      <h2>Run Replay</h2>
    </div>
    <div class="replay-controls">
      <input id="replay-job-id" type="text" placeholder="agent job id" />
      <button class="refresh-btn" id="load-replay">&#9654; Load replay</button>
    </div>
    <div id="replay-status" class="empty">Enter a completed agent job id to replay its actions, approvals, screenshots, and final status.</div>
    <div id="replay-output"></div>
  </div>

  <div class="section" id="auth-wizard">
    <div class="section-header">
      <h2>Auth Profile Wizard</h2>
      <button class="refresh-btn" id="refresh-auth-profiles">&#8635; Refresh profiles</button>
    </div>
    <ol class="wizard-steps">
      <li>
        <strong>1. Name the profile &amp; start login.</strong>
        <div class="wizard-controls">
          <input id="wizard-profile-name" type="text" placeholder="profile name (e.g. shop-account)" maxlength="120" />
          <input id="wizard-start-url" type="text" placeholder="login URL (optional)" />
          <button class="refresh-btn" id="wizard-start">Start login session</button>
        </div>
      </li>
      <li>
        <strong>2. Complete the login by hand</strong> in the takeover window, then come back.
        <div id="wizard-takeover"></div>
      </li>
      <li>
        <strong>3. Save the captured auth state</strong> under the profile name.
        <div class="wizard-controls">
          <button class="refresh-btn" id="wizard-save" disabled>Save auth profile</button>
        </div>
      </li>
      <li>
        <strong>4. Reopen a session</strong> from a saved profile.
        <div class="wizard-controls">
          <select id="wizard-profile-select"><option value="">— saved profiles —</option></select>
          <button class="refresh-btn" id="wizard-reopen">Reopen session</button>
        </div>
      </li>
    </ol>
    <div id="wizard-status" class="empty">Name a profile and start a login session to begin.</div>
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
// What the server said went wrong, as one line. FastAPI errors carry `detail` (a
// string, a list of validation errors, or an object such as an approval-required
// payload); a BrowserActionError body carries `error` and `code`.
const errorText = (body, status) => {
  if (body && typeof body === 'object') {
    const d = body.detail;
    if (typeof d === 'string' && d) return d;
    if (Array.isArray(d) && d.length) {
      return d.map((item) => (item && item.msg) ? item.msg : JSON.stringify(item)).join('; ');
    }
    if (d && typeof d === 'object') return String(d.message || d.error || JSON.stringify(d));
    if (typeof body.error === 'string' && body.error) {
      return typeof body.code === 'string' && body.code ? `${body.error} (${body.code})` : body.error;
    }
    if (typeof body.code === 'string' && body.code) return body.code;
  }
  return `request failed (HTTP ${status})`;
};
// Resolves with the parsed body of a successful request and rejects with an Error
// carrying the server's own text otherwise, so no caller can mistake a failed
// request for a done one. Callers show err.message.
const api = async (path, options = {}) => {
  const headers = {..._authHeaders(), ...(options.headers || {})};
  let r;
  try {
    r = await fetch(path, {...options, headers});
  } catch (e) {
    throw new Error('network error: ' + ((e && e.message) || 'request failed'));
  }
  if (r.status === 401) {
    sessionStorage.removeItem('ab_token');
    alert('Auth failed — token cleared. Reload and try again.');
    throw new Error('unauthorized: token cleared, reload and try again');
  }
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    if (r.status === 400 && String((body && body.detail) || '').includes('operator header')) {
      sessionStorage.removeItem('ab_operator_id');
      sessionStorage.removeItem('ab_operator_name');
      alert('Operator identity missing. Reload and provide an operator id.');
    }
    throw new Error(errorText(body, r.status));
  }
  return r.json();
};
const apiPost = (path, payload = null) => {
  const options = {method: 'POST'};
  if (payload !== null) {
    options.headers = {'Content-Type': 'application/json'};
    options.body = JSON.stringify(payload);
  }
  return api(path, options);
};
const asText = (value, fallback = '—') => {
  if (value === null || value === undefined || value === '') return fallback;
  return String(value);
};
// What would hide or reorder text written as its code point, as the server's shown_code_points
// does (app.approval_display): format characters (bidi controls, zero-width marks), controls (a
// newline shows as \\n), line and paragraph separators, and default-ignorable code points (variation
// selectors, Hangul fillers). Agents and pages write approval reasons and actions, so what the
// operator reads could otherwise differ from what they approve. Letters of other scripts stay.
const shownText = (value, fallback = '—') => {
  return asText(value, fallback).replace(
    /[\\p{Cf}\\p{Cc}\\p{Zl}\\p{Zp}\\p{Default_Ignorable_Code_Point}]/gu,
    (char) => (char === '\\n' ? '\\\\n' : `[U+${char.codePointAt(0).toString(16).toUpperCase().padStart(4, '0')}]`));
};
const statusBadge = (s) => {
  const map = {active:'green', running:'green', completed:'green', ok:'green',
                failed:'red', error:'red', rejected:'red',
                pending:'yellow', queued:'yellow', cancelling:'yellow', approval_required:'yellow',
                interrupted:'gray', cancelled:'gray', discarded:'gray', closed:'gray'};
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
// A table whose endpoint failed says so; an empty-state row would claim there is nothing to show.
const appendLoadError = (tbody, colspan, what, err) => {
  appendEmptyRow(tbody, colspan, `Could not load ${what}: ${err.message}`);
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
  // Each loader reports its own failure, so one dead endpoint cannot blank the others.
  await Promise.all([loadIdentity(), loadApprovals(), loadSessions(), loadAgentJobs(), loadPeers(), loadAudit(), loadAuthProfiles()]);
}

async function loadIdentity() {
  const nodeEl = document.getElementById('node-id');
  let d;
  try {
    d = await api('/mesh/identity');
  } catch (err) {
    nodeEl.textContent = `Node identity unavailable: ${err.message}`;
    return;
  }
  nodeEl.textContent = `Node: ${(d.node_id||'').slice(0,12)}...`;
}

async function loadSessions() {
  const tbody = document.getElementById('sessions-tbody');
  let d;
  try {
    d = await api('/sessions');
  } catch (err) {
    document.getElementById('stat-sessions').textContent = '—';
    appendLoadError(tbody, 6, 'sessions', err);
    return;
  }
  const sessions = d.sessions || d || [];
  document.getElementById('stat-sessions').textContent = sessions.filter?.(s=>s.status==='active').length||0;
  if (!sessions.length) { appendEmptyRow(tbody, 6, 'No sessions'); return; }
  tbody.replaceChildren();
  sessions.forEach(s => {
    const row = document.createElement('tr');
    const sessionId = asText(s.session_id || s.id, '').slice(0, 12);
    appendCell(row, sessionId ? `${sessionId}...` : '—', {className: 'mono'});
    appendCell(row, s.name);
    appendNodeCell(row, statusBadge(s.status || 'unknown'));
    appendCell(row, s.owner);
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

const checkpointTimeline = (job) => {
  const container = document.createElement('div');
  container.className = 'timeline';
  const checkpoints = Array.isArray(job.checkpoints) ? job.checkpoints.slice(-4) : [];
  if (!checkpoints.length) {
    const empty = document.createElement('span');
    empty.className = 'timeline-item';
    empty.textContent = 'No checkpoints';
    container.appendChild(empty);
    return container;
  }
  checkpoints.forEach(checkpoint => {
    const item = document.createElement('span');
    item.className = 'timeline-item';
    const step = document.createElement('strong');
    step.textContent = `#${checkpoint.step_index || '?'}`;
    item.appendChild(step);
    item.appendChild(document.createTextNode(` ${asText(checkpoint.status, 'unknown')}`));
    if (checkpoint.action) item.appendChild(document.createTextNode(` · ${checkpoint.action}`));
    if (checkpoint.title || checkpoint.url) {
      item.appendChild(document.createTextNode(` · ${checkpoint.title || checkpoint.url}`));
    }
    container.appendChild(item);
  });
  return container;
};

const jobActionButton = (label, className, handler) => {
  const button = document.createElement('button');
  button.className = className;
  button.type = 'button';
  button.textContent = label;
  button.addEventListener('click', handler);
  return button;
};

async function changeAgentJob(jobId, verb, payload = null) {
  const statusEl = document.getElementById('agent-jobs-status');
  statusEl.textContent = '';
  try {
    await apiPost('/agent/jobs/' + encodeURIComponent(jobId) + '/' + verb, payload);
  } catch (err) {
    statusEl.textContent = `Could not ${verb} agent job ${jobId}: ${err.message}`;
    return;
  }
  loadAgentJobs();
}

async function resumeAgentJob(jobId) {
  await changeAgentJob(jobId, 'resume', {});
}

async function discardAgentJob(jobId) {
  if (!confirm('Discard agent job ' + jobId + '?')) return;
  await changeAgentJob(jobId, 'discard');
}

async function cancelAgentJob(jobId) {
  if (!confirm('Cancel running agent job ' + jobId + '?')) return;
  await changeAgentJob(jobId, 'cancel');
}

async function loadAgentJobs() {
  const tbody = document.getElementById('agent-jobs-tbody');
  let jobs;
  try {
    jobs = await api('/agent/jobs');
  } catch (err) {
    document.getElementById('stat-agent-jobs').textContent = '—';
    appendLoadError(tbody, 7, 'agent jobs', err);
    return;
  }
  const items = Array.isArray(jobs) ? jobs : [];
  document.getElementById('stat-agent-jobs').textContent = items.length;
  if (!items.length) { appendEmptyRow(tbody, 7, 'No agent jobs'); return; }
  tbody.replaceChildren();
  items.slice(0,50).forEach(job => {
    const row = document.createElement('tr');
    const jobId = asText(job.id, '');
    appendCell(row, jobId ? `${jobId.slice(0, 12)}...` : '—', {className: 'mono'});
    appendCell(row, job.kind);
    appendNodeCell(row, statusBadge(job.status || 'unknown'));
    appendCell(row, job.request && job.request.workflow_profile);
    appendCell(row, job.checkpoint_count ?? (job.checkpoints || []).length);
    appendNodeCell(row, checkpointTimeline(job));

    const actions = document.createElement('div');
    actions.className = 'btn-row';
    if (job.status === 'running') {
      actions.appendChild(jobActionButton(
        'Cancel',
        'btn btn-danger btn-sm',
        () => cancelAgentJob(jobId)
      ));
    }
    if (job.resumable) {
      actions.appendChild(jobActionButton(
        'Resume',
        'btn btn-primary btn-sm',
        () => resumeAgentJob(jobId)
      ));
    }
    if (!['running', 'cancelling', 'cancelled', 'discarded'].includes(job.status)) {
      actions.appendChild(jobActionButton(
        'Discard',
        'btn btn-danger btn-sm',
        () => discardAgentJob(jobId)
      ));
    }
    if (!actions.childNodes.length) actions.textContent = '—';
    appendNodeCell(row, actions);
    tbody.appendChild(row);
  });
}

// --- Pending approvals -----------------------------------------------------
// Reasons and actions come from pages and agents, so they are written as text
// only. An approval stores the exact action it authorises, typed text
// included; a type action marked sensitive never shows that text here.
// The fields and how they read come from the server (app.approval_display), so
// the queue says what the webhook and SSE summary say, field for field.
const APPROVAL_DISPLAY = __APPROVAL_DISPLAY__;
// A string's first `cut` characters (code points, never half a pair), and how many more there are.
const cutChars = (value, cut) => {
  const chars = Array.from(value);
  return chars.length <= cut ? [value, 0] : [chars.slice(0, cut).join(''), chars.length - cut];
};
const leftOut = (count) => (count ? `… (${count} more characters not shown)` : '');
// A field's value as JSON, so a string shows its quotes and a newline in it shows as \\n.
const shownValue = (value, cut) => {
  // As String(n), as the server's js_number: JSON.stringify writes Infinity (an integer past a double,
  // as JSON.parse reads it) as null.
  if (typeof value === 'number') return String(value);
  if (typeof value !== 'string' || cut === undefined) return JSON.stringify(value);
  const [kept, more] = cutChars(value, cut);
  return JSON.stringify(kept) + leftOut(more);
};
// A URL's host as written, as the server's _WRITTEN_HOST reads it, and the URL with a non-ASCII host as a
// browser sends it, that host as written beside it (the server's url_as_sent).
const WRITTEN_HOST = /^[A-Za-z][A-Za-z0-9+.-]*:\\/\\/(?:[^\\/?#]*@)?(\\[[^\\]]*\\]|[^:\\/?#]*)/;
const urlAsSent = (url) => {
  const match = WRITTEN_HOST.exec(url);
  if (!match || /^[\\x00-\\x7f]*$/.test(match[1])) return [url, null];
  let sent;
  try {
    sent = new URL(url).hostname;
  } catch {
    return [url, null];
  }
  const start = match[0].length - match[1].length;
  return [url.slice(0, start) + sent + url.slice(match[0].length), match[1]];
};
const shownField = (name, value, cut) => {
  if (name !== 'url' || typeof value !== 'string') return `${name}=${shownValue(value, cut)}`;
  const [sent, written] = urlAsSent(value);
  const shown = `${name}=${shownValue(sent, cut)}`;
  return written === null ? shown : `${shown} (host as written: ${written})`;
};
const describeAction = (action, origin, cut) => {
  if (!action || typeof action !== 'object') return '—';
  // A governed tool call (eval_js, set_cookies, ...) is approved through a
  // stand-in action whose text is "<tool> sha256:<digest of its arguments>"
  // (McpToolGateway._governed_call_decision); its reason previews the arguments.
  // Only the server's origin says the gateway built it: a caller can write the
  // same action, and then it is shown as the action it is.
  const toolCall = origin === 'tool_call'
    && /^([a-z_.]+) sha256:[0-9a-f]{64}$/.exec(String(action.text || ''));
  if (toolCall) return shownText(`tool call ${toolCall[1]} (arguments under Reason)`);
  // Everything the approval covers (it must match exactly) is shown, the target first: a click by
  // coordinates, or an option picked behind an opaque element id, read as a bare "click".
  const verb = asText(action.action, 'unknown');
  const parts = [verb];
  const reads = Object.hasOwn(APPROVAL_DISPLAY.reads, verb) ? APPROVAL_DISPLAY.reads[verb] : [];
  // An empty string is set: only a value and a label can be one, and either runs (select_option).
  const isSet = (value) => value !== null && value !== undefined;
  // Why a set field does not act, as the server's unused_note says: the action does not read it, or
  // a field set beside it runs instead.
  const unusedNote = (name) => {
    if (!reads.includes(name)) return APPROVAL_DISPLAY.notUsedBy.replace('{action}', verb);
    const others = Object.hasOwn(APPROVAL_DISPLAY.overriddenBy, name) ? APPROVAL_DISPLAY.overriddenBy[name] : [];
    const runs = others.find((other) => reads.includes(other) && isSet(action[other]));
    return runs === undefined ? null : APPROVAL_DISPLAY.notUsedFor.replace('{field}', runs);
  };
  APPROVAL_DISPLAY.fields.forEach(([name, actions]) => {
    const value = action[name];
    if ((actions && !actions.includes(action.action)) || !isSet(value)) return;
    const part = name === 'text' && action.sensitive
      ? APPROVAL_DISPLAY.sensitiveTextHidden
      : shownField(name, value, cut);
    const note = unusedNote(name)
      ?? (verb === 'select_option' && name === 'value' ? APPROVAL_DISPLAY.valueOrLabel : null);
    parts.push(note ? `${part} ${note}` : part);
  });
  return shownText(parts.join(' · '));
};
// What confirm() asks: short, the target first, each string cut with what it leaves out. Chromium
// cuts a long dialog itself without saying so; the queue row shows the whole approval. A tool call's
// reason is the server's preview of its arguments, which the row shows too: the viewer has the rest.
// The note the server ends a cut tool-call preview with (McpToolGateway._governed_call_decision).
const SERVER_CUT_NOTE = /… \\(\\d+ characters in all: View all shows them\\)$/;
// A tool call's preview cut for the dialog: the server's note kept, and no count of the dialog's own,
// which counted the preview, not the call, beside the note's total.
const cutPreview = (preview) => {
  const note = SERVER_CUT_NOTE.exec(preview);
  const [kept, more] = cutChars(note ? preview.slice(0, note.index) : preview, APPROVAL_DISPLAY.cut);
  if (note) return kept + note[0];
  return more ? `${kept}…` : kept;
};
const approvalConfirmText = (approval) => {
  const toolCall = approval.origin === 'tool_call';
  const text = asText(approval.reason, '');
  const [reason, more] = cutChars(text, APPROVAL_DISPLAY.cut);
  const shown = toolCall ? cutPreview(text) : reason + leftOut(more);
  let whole = 'The queue row shows the whole approval.';
  if (toolCall) {
    whole = approval.arguments_size
      ? `View all ${approval.arguments_size} characters in the queue shows the whole call, credentials left out.`
      : 'Nothing more of the call is kept than this preview.';
  }
  return `Approve this ${asText(approval.kind, 'unknown')} action in session ${asText(approval.session_id)}?\\n\\n`
    + `${describeAction(approval.action, approval.origin, APPROVAL_DISPLAY.cut)}\\n\\n`
    + `${toolCall ? 'Arguments (server preview)' : 'Reason, as the agent wrote it'}: `
    + `${shownText(shown)}\\n\\n${whole}`;
};

// What the queue says after a decision: the server's message when it gives one (a reject that waits
// on a claimed action, an approve that withdraws it), else the record's new status.
const decisionStatusText = (approvalId, result) => {
  const message = result && result.message;
  if (message) return shownText(asText(message, ''));
  return `Approval ${approvalId} ${asText(result && result.status, 'updated')}.`;
};

async function decideApproval(approval, decision) {
  const statusEl = document.getElementById('approvals-status');
  const approvalId = asText(approval.id, '');
  let comment = null;
  if (decision === 'approve') {
    if (!confirm(approvalConfirmText(approval))) return;
  } else {
    comment = prompt('Reason for rejecting (optional):', '');
    if (comment === null) return;
  }
  try {
    const result = await apiPost(`/approvals/${encodeURIComponent(approvalId)}/${decision}`, {comment: comment || null});
    statusEl.textContent = decisionStatusText(approvalId, result);
  } catch (err) {
    statusEl.textContent = `Could not ${decision} approval ${approvalId}: ${err.message}`;
  }
  loadApprovals();
}

// A tool call's whole arguments, which its reason only previews: fetched when asked for and shown as
// text, each line with what would hide or reorder it written as code points (the server writes ASCII).
const argumentsText = (text) => asText(text, '').split('\\n').map((line) => shownText(line, '')).join('\\n');
// One viewer serves every row, so an answer is shown only while its approval is the one viewed: a slow
// one used to land under the title of the approval the operator had moved on to.
async function viewArguments(approval) {
  const approvalId = asText(approval.id, '');
  const viewer = document.getElementById('approval-arguments');
  const shown = document.getElementById('approval-arguments-text');
  viewer.dataset.approvalId = approvalId;
  document.getElementById('approval-arguments-title').textContent = `Arguments of approval ${approvalId}`;
  shown.textContent = 'Loading...';
  viewer.hidden = false;
  let text;
  try {
    const result = await api(`/approvals/${encodeURIComponent(approvalId)}/arguments`);
    text = argumentsText(result && result.arguments);
  } catch (err) {
    text = `Could not load the arguments: ${err.message}`;
  }
  if (viewer.dataset.approvalId === approvalId) shown.textContent = text;
}
const closeArguments = () => {
  const viewer = document.getElementById('approval-arguments');
  viewer.hidden = true;
  delete viewer.dataset.approvalId;
  document.getElementById('approval-arguments-text').textContent = '';
};

// A queue row's cells, as text and class: the agent's reason with what would hide or reorder it shown.
const approvalCells = (a) => {
  return [
    [formatEventTime(a.created_at), 'mono'],
    [asText(a.id, ''), 'mono'],
    [asText(a.session_id, '').slice(0, 12), 'mono'],
    [a.kind, undefined],
    [describeAction(a.action, a.origin), 'mono wrap'],
    [shownText(a.reason), 'wrap'],
  ];
};

async function loadApprovals() {
  const tbody = document.getElementById('approvals-tbody');
  let d;
  try {
    d = await api('/approvals?status=pending');
  } catch (err) {
    document.getElementById('stat-approvals').textContent = '—';
    appendLoadError(tbody, 7, 'approvals', err);
    return;
  }
  const items = Array.isArray(d) ? d : [];
  document.getElementById('stat-approvals').textContent = items.length;
  // Its approval decided, or gone: the viewer closes rather than stay open beside rows it is not.
  const viewing = document.getElementById('approval-arguments').dataset.approvalId;
  if (viewing !== undefined && !items.some((a) => asText(a.id, '') === viewing)) closeArguments();
  if (!items.length) { appendEmptyRow(tbody, 7, 'No approvals waiting'); return; }
  tbody.replaceChildren();
  items.forEach(a => {
    const row = document.createElement('tr');
    approvalCells(a).forEach(([text, className]) => appendCell(row, text, {className}));
    const actions = document.createElement('div');
    actions.className = 'btn-row';
    actions.appendChild(jobActionButton('Approve', 'btn btn-primary btn-sm', () => decideApproval(a, 'approve')));
    actions.appendChild(jobActionButton('Reject', 'btn btn-danger btn-sm', () => decideApproval(a, 'reject')));
    if (a.arguments_size) {
      const view = `View all ${a.arguments_size} characters`;
      actions.appendChild(jobActionButton(view, 'refresh-btn', () => viewArguments(a)));
    }
    appendNodeCell(row, actions);
    tbody.appendChild(row);
  });
}

async function loadPeers() {
  const tbody = document.getElementById('peers-tbody');
  let d;
  try {
    d = await api('/mesh/peers');
  } catch (err) {
    document.getElementById('stat-peers').textContent = '—';
    appendLoadError(tbody, 5, 'peers', err);
    return;
  }
  const peers = d.peers || [];
  document.getElementById('stat-peers').textContent = peers.length;
  if (!peers.length) { appendEmptyRow(tbody, 5, 'No peers registered'); return; }
  tbody.replaceChildren();
  peers.forEach(p => {
    const row = document.createElement('tr');
    const nodeId = asText(p.node_id, '').slice(0, 16);
    appendCell(row, nodeId ? `${nodeId}...` : '—', {className: 'mono'});
    appendCell(row, p.display_name);
    appendCell(row, p.endpoint, {className: 'mono'});
    appendCell(row, timeAgo(p.last_seen));
    appendCell(row, `${(p.grants||[]).length} grants`);
    tbody.appendChild(row);
  });
}

async function loadAudit() {
  const tbody = document.getElementById('audit-tbody');
  let d;
  try {
    d = await api('/audit/events?limit=50');
  } catch (err) {
    document.getElementById('stat-audit').textContent = '—';
    appendLoadError(tbody, 5, 'audit events', err);
    return;
  }
  const events = d.events || d || [];
  if (Array.isArray(events)) document.getElementById('stat-audit').textContent = events.length;
  if (!events.length) { appendEmptyRow(tbody, 5, 'No audit events'); return; }
  tbody.replaceChildren();
  events.slice(0,50).forEach(e => {
    const row = document.createElement('tr');
    appendCell(row, formatEventTime(e.timestamp), {className: 'mono'});
    // An AuditEvent carries `operator` ({id, name, source}); there is no top-level operator_id.
    appendCell(row, e.operator && e.operator.id);
    appendCell(row, e.action || e.event_type);
    appendCell(row, asText(e.session_id, '').slice(0, 8) || '—', {className: 'mono'});
    appendNodeCell(row, statusBadge(e.status || 'ok'));
    tbody.appendChild(row);
  });
}

// --- Run replay ------------------------------------------------------------
// Renders a completed agent run from existing artifacts. All values come from
// untrusted run data, so everything is written with text nodes and the shared
// safe cell helpers -- never via raw HTML assignment.
function replaySection(title) {
  const header = document.createElement('h3');
  header.textContent = title;
  document.getElementById('replay-output').appendChild(header);
  const table = document.createElement('table');
  const tbody = document.createElement('tbody');
  table.appendChild(tbody);
  document.getElementById('replay-output').appendChild(table);
  return tbody;
}
function renderReplayScreenshots(steps) {
  const urls = [];
  steps.forEach((step) => {
    const exec = step.execution || {};
    const obs = step.observation || {};
    [exec.screenshot, exec.screenshot_url, obs.screenshot, obs.screenshot_url].forEach((v) => {
      const safe = safeHttpUrl(v);
      if (safe) urls.push(safe);
    });
  });
  const header = document.createElement('h3');
  header.textContent = `Screenshots (${urls.length})`;
  document.getElementById('replay-output').appendChild(header);
  if (!urls.length) {
    const note = document.createElement('div');
    note.className = 'empty';
    note.textContent = 'No screenshot artifacts available for this run.';
    document.getElementById('replay-output').appendChild(note);
    return;
  }
  urls.forEach((url) => {
    const img = document.createElement('img');
    img.src = url;
    img.alt = 'run screenshot';
    img.style.maxWidth = '320px';
    img.style.margin = '4px';
    document.getElementById('replay-output').appendChild(img);
  });
}
async function loadReplay() {
  const jobId = (document.getElementById('replay-job-id').value || '').trim();
  const statusEl = document.getElementById('replay-status');
  const out = document.getElementById('replay-output');
  out.replaceChildren();
  if (!jobId) { statusEl.textContent = 'Enter an agent job id.'; return; }
  statusEl.textContent = 'Loading replay...';
  let job;
  try {
    job = await api(`/agent/jobs/${encodeURIComponent(jobId)}`);
  } catch (err) {
    statusEl.textContent = `Could not load job ${jobId}: ${err.message}`;
    return;
  }
  const result = job.result || job;
  const sessionId = (result.final_session && result.final_session.id) || job.session_id || '';

  const statusLine = document.createElement('div');
  statusLine.appendChild(document.createTextNode('Final status: '));
  statusLine.appendChild(statusBadge(result.status));
  out.appendChild(statusLine);

  const steps = Array.isArray(result.steps) ? result.steps : [];
  const actionsBody = replaySection(`Actions (${steps.length})`);
  if (!steps.length) {
    appendEmptyRow(actionsBody, 3, 'No steps recorded.');
  } else {
    steps.forEach((step, index) => {
      const decision = step.decision || {};
      const row = document.createElement('tr');
      appendCell(row, index + 1);
      appendCell(row, decision.action);
      appendNodeCell(row, statusBadge(step.status));
      actionsBody.appendChild(row);
    });
  }

  try {
    const approvals = await api('/approvals');
    const list = Array.isArray(approvals) ? approvals : (approvals.approvals || []);
    const forRun = list.filter((a) => !sessionId || a.session_id === sessionId);
    const apprBody = replaySection(`Approvals (${forRun.length})`);
    if (!forRun.length) {
      appendEmptyRow(apprBody, 5, 'No approvals for this run.');
    } else {
      forRun.forEach((a) => {
        const row = document.createElement('tr');
        // Which approval, and what it bound, as the queue describes it: the reason is the agent's own
        // account, which the approval does not bind.
        appendCell(row, a.id);
        appendCell(row, a.kind);
        appendCell(row, describeAction(a.action, a.origin));
        // As the queue shows it: what would hide or reorder the agent's words, as its code point.
        appendCell(row, shownText(a.reason));
        appendNodeCell(row, statusBadge(a.status));
        apprBody.appendChild(row);
      });
    }
  } catch (err) {
    // The run's actions and screenshots still replay; say the approvals are missing.
    const note = document.createElement('div');
    note.className = 'empty';
    note.textContent = `Could not load approvals: ${err.message}`;
    out.appendChild(note);
  }

  renderReplayScreenshots(steps);
  statusEl.textContent = `Replay for job ${jobId}.`;
}
document.getElementById('load-replay').addEventListener('click', loadReplay);

// --- Auth profile wizard ---------------------------------------------------
// Guides an operator through: name a profile -> log in by hand in the takeover
// window -> save the captured auth state -> reopen a session from that profile.
// Profile names and session/takeover URLs are untrusted, so they are rendered
// with text nodes and safeHttpUrl, never raw HTML.
let wizardSessionId = null;
async function wizardStart() {
  const name = (document.getElementById('wizard-profile-name').value || '').trim();
  const startUrl = (document.getElementById('wizard-start-url').value || '').trim();
  const statusEl = document.getElementById('wizard-status');
  if (!name) { statusEl.textContent = 'Enter a profile name first.'; return; }
  statusEl.textContent = 'Starting a login session...';
  const body = { name: `login-${name}` };
  const safeStart = safeHttpUrl(startUrl);
  if (safeStart) body.start_url = safeStart;
  let session;
  try {
    session = await apiPost('/sessions', body);
  } catch (err) {
    statusEl.textContent = `Could not start a login session: ${err.message}`;
    return;
  }
  wizardSessionId = session.id || session.session_id || null;
  document.getElementById('wizard-save').disabled = !wizardSessionId;

  const takeover = document.getElementById('wizard-takeover');
  takeover.replaceChildren();
  const url = safeHttpUrl(session.takeover_url);
  if (url) {
    const link = document.createElement('a');
    link.href = url;
    link.target = '_blank';
    link.rel = 'noopener';
    link.textContent = 'Open takeover window to log in';
    takeover.appendChild(link);
  } else {
    const note = document.createElement('span');
    note.className = 'empty';
    note.textContent = 'No takeover URL available for this session.';
    takeover.appendChild(note);
  }
  statusEl.textContent = `Session ${wizardSessionId} ready. Log in by hand, then save.`;
}
async function wizardSave() {
  const name = (document.getElementById('wizard-profile-name').value || '').trim();
  const statusEl = document.getElementById('wizard-status');
  if (!wizardSessionId || !name) { statusEl.textContent = 'Start a login session first.'; return; }
  statusEl.textContent = 'Saving auth profile...';
  try {
    await apiPost(`/sessions/${encodeURIComponent(wizardSessionId)}/auth-profiles`,
      { profile_name: name });
  } catch (err) {
    statusEl.textContent = `Could not save the auth profile: ${err.message}`;
    return;
  }
  statusEl.textContent = `Saved auth profile "${name}".`;
  await loadAuthProfiles();
}
async function loadAuthProfiles() {
  const select = document.getElementById('wizard-profile-select');
  let profiles;
  try {
    profiles = await api('/auth-profiles');
  } catch (err) {
    document.getElementById('wizard-status').textContent = `Could not load saved profiles: ${err.message}`;
    return;
  }
  const list = Array.isArray(profiles) ? profiles : (profiles.profiles || []);
  select.replaceChildren();
  const placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.textContent = '— saved profiles —';
  select.appendChild(placeholder);
  list.forEach((p) => {
    const name = asText(typeof p === 'string' ? p : (p.name || p.profile_name), '');
    if (!name) return;
    const opt = document.createElement('option');
    opt.value = name;       // value set as a property, never interpolated into HTML
    opt.textContent = name;
    select.appendChild(opt);
  });
}
async function wizardReopen() {
  const name = document.getElementById('wizard-profile-select').value;
  const statusEl = document.getElementById('wizard-status');
  if (!name) { statusEl.textContent = 'Pick a saved profile to reopen.'; return; }
  statusEl.textContent = `Reopening a session from "${name}"...`;
  let session;
  try {
    session = await apiPost('/sessions', { name: `reopen-${name}`, auth_profile: name });
  } catch (err) {
    statusEl.textContent = `Could not reopen a session from that profile: ${err.message}`;
    return;
  }
  const sid = session.id || session.session_id || '';
  statusEl.textContent = `Reopened session ${sid} from profile "${name}".`;
  loadSessions();
}
document.getElementById('wizard-start').addEventListener('click', wizardStart);
document.getElementById('wizard-save').addEventListener('click', wizardSave);
document.getElementById('wizard-reopen').addEventListener('click', wizardReopen);
document.getElementById('refresh-auth-profiles').addEventListener('click', loadAuthProfiles);

document.getElementById('refresh-approvals').addEventListener('click', loadApprovals);
document.getElementById('approval-arguments-close').addEventListener('click', closeArguments);
document.getElementById('refresh-sessions').addEventListener('click', loadSessions);
document.getElementById('refresh-agent-jobs').addEventListener('click', loadAgentJobs);
document.getElementById('refresh-peers').addEventListener('click', loadPeers);
document.getElementById('refresh-audit').addEventListener('click', loadAudit);

// Auto-refresh every 15s
loadAll();
setInterval(loadAll, 15000);
</script>
</body>
</html>"""
# The server's account of an approval, so the queue renders the same fields (app.approval_display).
_DASHBOARD_HTML = _DASHBOARD_HTML.replace("__APPROVAL_DISPLAY__", json.dumps(APPROVAL_DISPLAY))
