"""Operator dashboard HTML template (served by routes.extensions.dashboard)."""
from __future__ import annotations

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
  <a href="#agent-jobs">Agent Jobs</a>
  <a href="#peers">Peers</a>
  <a href="#audit">Audit Log</a>
</nav>
<main>
  <!-- Stats -->
  <div class="grid" id="stats">
    <div class="card"><h3>Active Sessions</h3><div class="value" id="stat-sessions">—</div></div>
    <div class="card"><h3>Workflow Runs</h3><div class="value" id="stat-workflows">—</div></div>
    <div class="card"><h3>Agent Jobs</h3><div class="value" id="stat-agent-jobs">—</div></div>
    <div class="card"><h3>Mesh Peers</h3><div class="value" id="stat-peers">—</div></div>
    <div class="card"><h3>Audit Events</h3><div class="value" id="stat-audit">—</div></div>
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

  <!-- Workflows -->
  <div class="section" id="workflows">
    <div class="section-header">
      <h2>Workflow Runs</h2>
      <button class="refresh-btn" id="refresh-workflows">↻ Refresh</button>
    </div>
    <table><thead><tr>
      <th>Run ID</th><th>Workflow</th><th>Status</th><th>Started</th><th>Duration</th>
    </tr></thead><tbody id="workflows-tbody"><tr><td colspan="5" class="empty">Loading...</td></tr></tbody></table>
  </div>

  <!-- Agent Jobs -->
  <div class="section" id="agent-jobs">
    <div class="section-header">
      <h2>Agent Jobs</h2>
      <button class="refresh-btn" id="refresh-agent-jobs">↻ Refresh</button>
    </div>
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
    <table><thead><tr>
      <th>Node ID</th><th>Display Name</th><th>Endpoint</th><th>Last Seen</th><th>Grants</th><th>Actions</th>
    </tr></thead><tbody id="peers-tbody"><tr><td colspan="6" class="empty">No peers registered</td></tr></tbody></table>
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
const api = (path, options = {}) => {
  const headers = {..._authHeaders(), ...(options.headers || {})};
  return fetch(path, {...options, headers})
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
const safeHttpUrl = (value) => {
  if (!value) return null;
  try {
    const parsed = new URL(String(value), window.location.origin);
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') return parsed.href;
  } catch (_) {}
  return null;
};

async function loadAll() {
  await Promise.all([loadIdentity(), loadSessions(), loadWorkflows(), loadAgentJobs(), loadPeers(), loadAudit(), loadAuthProfiles()]);
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

async function resumeAgentJob(jobId) {
  const result = await apiPost('/agent/jobs/' + encodeURIComponent(jobId) + '/resume', {});
  if (result && result.id) loadAgentJobs();
}

async function discardAgentJob(jobId) {
  if (!confirm('Discard agent job ' + jobId + '?')) return;
  const result = await apiPost('/agent/jobs/' + encodeURIComponent(jobId) + '/discard');
  if (result && result.id) loadAgentJobs();
}

async function cancelAgentJob(jobId) {
  if (!confirm('Cancel running agent job ' + jobId + '?')) return;
  const result = await apiPost('/agent/jobs/' + encodeURIComponent(jobId) + '/cancel');
  if (result && result.id) loadAgentJobs();
}

async function loadAgentJobs() {
  const jobs = await api('/agent/jobs');
  const items = Array.isArray(jobs) ? jobs : [];
  document.getElementById('stat-agent-jobs').textContent = items.length;
  const tbody = document.getElementById('agent-jobs-tbody');
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
    statusEl.textContent = `Could not load job ${jobId}.`;
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
      appendEmptyRow(apprBody, 3, 'No approvals for this run.');
    } else {
      forRun.forEach((a) => {
        const row = document.createElement('tr');
        appendCell(row, a.kind);
        appendCell(row, a.reason);
        appendNodeCell(row, statusBadge(a.status));
        apprBody.appendChild(row);
      });
    }
  } catch (err) {
    /* approvals are best-effort in replay */
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
    statusEl.textContent = 'Could not start a login session.';
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
    statusEl.textContent = 'Could not save the auth profile (is the login complete?).';
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
    statusEl.textContent = 'Could not reopen a session from that profile.';
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

document.getElementById('refresh-sessions').addEventListener('click', loadSessions);
document.getElementById('refresh-workflows').addEventListener('click', loadWorkflows);
document.getElementById('refresh-agent-jobs').addEventListener('click', loadAgentJobs);
document.getElementById('refresh-peers').addEventListener('click', loadPeers);
document.getElementById('refresh-audit').addEventListener('click', loadAudit);

// Auto-refresh every 15s
loadAll();
setInterval(loadAll, 15000);
</script>
</body>
</html>"""
