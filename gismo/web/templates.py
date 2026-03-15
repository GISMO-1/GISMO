"""Embedded HTML for the GISMO web dashboard (zero external dependencies)."""
from __future__ import annotations

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GISMO Dashboard</title>
<style>
  :root {
    --bg: #0d1117; --panel: #161b22; --border: #30363d;
    --text: #c9d1d9; --dim: #8b949e; --green: #3fb950;
    --yellow: #d29922; --red: #f85149; --blue: #58a6ff;
    --purple: #bc8cff;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Cascadia Code', 'Consolas', monospace; font-size: 13px; }
  header { background: var(--panel); border-bottom: 1px solid var(--border); padding: 10px 20px; display: flex; align-items: center; gap: 16px; }
  header h1 { font-size: 16px; color: var(--blue); letter-spacing: 2px; }
  header .db-path { color: var(--dim); font-size: 11px; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #refresh-indicator { font-size: 11px; color: var(--dim); }
  #refresh-indicator.active { color: var(--green); }

  .layout { display: flex; height: calc(100vh - 45px); }
  .sidebar { width: 220px; background: var(--panel); border-right: 1px solid var(--border); padding: 12px; overflow-y: auto; flex-shrink: 0; }
  .main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }

  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 10px; margin-bottom: 10px; }
  .card h3 { font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: var(--dim); margin-bottom: 8px; }
  .stat-row { display: flex; justify-content: space-between; padding: 2px 0; }
  .stat-label { color: var(--dim); }

  .daemon-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
  .dot-green { background: var(--green); }
  .dot-yellow { background: var(--yellow); }
  .dot-red { background: var(--red); }
  .dot-grey { background: var(--dim); }

  .tabs { display: flex; border-bottom: 1px solid var(--border); background: var(--panel); }
  .tab { padding: 8px 16px; cursor: pointer; color: var(--dim); font-size: 12px; border-bottom: 2px solid transparent; transition: color 0.15s; }
  .tab:hover { color: var(--text); }
  .tab.active { color: var(--blue); border-bottom-color: var(--blue); }

  .tab-content { display: none; flex: 1; overflow: auto; }
  .tab-content.active { display: block; }

  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th { text-align: left; padding: 6px 10px; color: var(--dim); font-weight: normal; border-bottom: 1px solid var(--border); position: sticky; top: 0; background: var(--bg); z-index: 1; }
  td { padding: 5px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
  tr:hover td { background: rgba(255,255,255,0.02); }

  .status-QUEUED, .status-pending { color: var(--dim); }
  .status-IN_PROGRESS, .status-running { color: var(--yellow); font-weight: bold; }
  .status-SUCCEEDED, .status-succeeded { color: var(--green); }
  .status-FAILED, .status-failed { color: var(--red); font-weight: bold; }
  .status-CANCELLED { color: var(--dim); text-decoration: line-through; }

  .btn { padding: 3px 8px; border: 1px solid var(--border); border-radius: 4px; cursor: pointer; font-size: 11px; font-family: inherit; background: transparent; color: var(--text); transition: background 0.1s; }
  .btn:hover { background: var(--border); }
  .btn-red { border-color: var(--red); color: var(--red); }
  .btn-red:hover { background: rgba(248,81,73,0.15); }
  .btn-yellow { border-color: var(--yellow); color: var(--yellow); }
  .btn-yellow:hover { background: rgba(210,153,34,0.15); }

  .toolbar { padding: 8px 12px; display: flex; gap: 8px; align-items: center; border-bottom: 1px solid var(--border); background: var(--panel); }
  .toolbar .spacer { flex: 1; }

  .mono { font-family: 'Cascadia Code', 'Consolas', monospace; }
  .id-col { color: var(--blue); font-size: 11px; }
  .cmd-col { color: var(--purple); max-width: 400px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .error-text { color: var(--red); font-size: 11px; }
  .dim { color: var(--dim); }

  #run-detail { padding: 16px; }
  #run-detail h2 { font-size: 14px; margin-bottom: 12px; }
  .back-btn { color: var(--blue); cursor: pointer; font-size: 12px; margin-bottom: 12px; display: inline-block; }
  .back-btn:hover { text-decoration: underline; }
  .section-title { font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: var(--dim); margin: 16px 0 6px; }

  .ns-block { margin-bottom: 16px; }
  .ns-header { font-size: 12px; color: var(--blue); padding: 4px 10px; background: rgba(88,166,255,0.08); border-left: 2px solid var(--blue); }
  .ns-retired { opacity: 0.5; }

  .empty-state { padding: 40px; text-align: center; color: var(--dim); }
</style>
</head>
<body>
<header>
  <h1>GISMO</h1>
  <span class="db-path" id="db-path-label">Loading…</span>
  <span id="refresh-indicator">⟳</span>
</header>
<div class="layout">
  <div class="sidebar">
    <div class="card" id="daemon-card">
      <h3>Daemon</h3>
      <div id="daemon-body"><span class="dim">Loading…</span></div>
    </div>
    <div class="card" id="queue-stats-card">
      <h3>Queue</h3>
      <div id="queue-stats-body"><span class="dim">Loading…</span></div>
    </div>
    <div style="margin-top:8px;">
      <button class="btn btn-yellow" id="btn-pause" onclick="togglePause()">Pause Daemon</button>
    </div>
  </div>
  <div class="main">
    <div class="tabs">
      <div class="tab active" data-tab="queue" onclick="switchTab('queue')">Queue</div>
      <div class="tab" data-tab="runs" onclick="switchTab('runs')">Runs</div>
      <div class="tab" data-tab="memory" onclick="switchTab('memory')">Memory</div>
    </div>

    <!-- Queue tab -->
    <div class="tab-content active" id="tab-queue">
      <div class="toolbar">
        <button class="btn btn-red" onclick="purgeFailed()">Purge Failed</button>
        <span class="spacer"></span>
        <span class="dim" id="queue-count"></span>
      </div>
      <table id="queue-table">
        <thead><tr>
          <th>ID</th><th>Status</th><th>Att</th><th>Age</th><th>Command</th><th></th>
        </tr></thead>
        <tbody id="queue-body"></tbody>
      </table>
    </div>

    <!-- Runs tab -->
    <div class="tab-content" id="tab-runs">
      <div class="toolbar">
        <span class="dim" id="runs-count"></span>
      </div>
      <div id="runs-view">
        <table id="runs-table">
          <thead><tr>
            <th>ID</th><th>Status</th><th>Tasks</th><th>Age</th><th>Label</th>
          </tr></thead>
          <tbody id="runs-body"></tbody>
        </table>
      </div>
      <div id="run-detail" style="display:none;"></div>
    </div>

    <!-- Memory tab -->
    <div class="tab-content" id="tab-memory">
      <div class="toolbar">
        <span class="dim" id="memory-count"></span>
      </div>
      <div id="memory-body"></div>
    </div>
  </div>
</div>

<script>
const API = (path, opts) => fetch(path, opts).then(r => r.json());
let _status = null;
let _paused = false;

function ageStr(isoStr) {
  if (!isoStr) return '-';
  const secs = Math.max(0, Math.floor((Date.now() - new Date(isoStr).getTime()) / 1000));
  if (secs < 60) return secs + 's';
  if (secs < 3600) return Math.floor(secs/60) + 'm';
  return Math.floor(secs/3600) + 'h';
}

function trunc(s, n) {
  if (!s) return '';
  return s.length <= n ? s : s.slice(0, n-1) + '…';
}

function statusClass(s) { return 'status-' + s; }

function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.toggle('active', c.id === 'tab-' + name));
}

// ── Status / Sidebar ──────────────────────────────────────────────────────

async function refreshStatus() {
  try {
    const data = await API('/api/status');
    _status = data;
    renderDaemon(data.daemon);
    renderQueueStats(data.queue);
    _paused = data.daemon.paused;
    document.getElementById('btn-pause').textContent = _paused ? 'Resume Daemon' : 'Pause Daemon';
    document.getElementById('db-path-label').textContent = data.db_path || '';
  } catch(e) {
    document.getElementById('daemon-body').innerHTML = '<span class="error-text">Error loading status</span>';
  }
}

function dotClass(d) {
  if (!d.running) return 'dot-grey';
  if (d.stale) return 'dot-red';
  if (d.paused) return 'dot-yellow';
  return 'dot-green';
}

function renderDaemon(d) {
  const body = document.getElementById('daemon-body');
  if (!d.running) {
    body.innerHTML = '<span class="daemon-dot dot-grey"></span><span class="dim">not running</span>';
    return;
  }
  const dot = `<span class="daemon-dot ${dotClass(d)}"></span>`;
  const health = d.stale ? '<span style="color:var(--red)">stale</span>' : '<span style="color:var(--green)">healthy</span>';
  const ctrl = d.paused ? '<span style="color:var(--yellow)">paused</span>' : '<span style="color:var(--green)">active</span>';
  body.innerHTML = `${dot}${health}<br><span class="dim">pid</span> ${d.pid}<br><span class="dim">hb</span> ${d.age_secs}s ago<br>${ctrl}`;
}

function renderQueueStats(q) {
  const by = q.by_status || {};
  const total = q.total || 0;
  const ip = by.IN_PROGRESS || 0;
  const f = by.FAILED || 0;
  document.getElementById('queue-stats-body').innerHTML = `
    <div class="stat-row"><span class="stat-label">total</span><span>${total}</span></div>
    <div class="stat-row"><span class="stat-label">queued</span><span>${by.QUEUED||0}</span></div>
    <div class="stat-row"><span class="stat-label">in-progress</span><span style="color:${ip?'var(--yellow)':'inherit'}">${ip}</span></div>
    <div class="stat-row"><span class="stat-label">succeeded</span><span>${by.SUCCEEDED||0}</span></div>
    <div class="stat-row"><span class="stat-label">failed</span><span style="color:${f?'var(--red)':'inherit'}">${f}</span></div>
    <div class="stat-row"><span class="stat-label">cancelled</span><span>${by.CANCELLED||0}</span></div>
  `;
}

async function togglePause() {
  const newPaused = !_paused;
  await API('/api/daemon/' + (newPaused ? 'pause' : 'resume'), {method:'POST'});
  await refreshStatus();
}

// ── Queue ─────────────────────────────────────────────────────────────────

async function refreshQueue() {
  try {
    const items = await API('/api/queue');
    const body = document.getElementById('queue-body');
    document.getElementById('queue-count').textContent = items.length + ' items';
    if (!items.length) {
      body.innerHTML = '<tr><td colspan="6" class="empty-state">No queue items</td></tr>';
      return;
    }
    body.innerHTML = items.map(item => {
      const canCancel = item.status === 'QUEUED' || item.status === 'IN_PROGRESS';
      const cancelBtn = canCancel
        ? `<button class="btn btn-red" onclick="cancelItem('${item.id}')">Cancel</button>`
        : '';
      return `<tr>
        <td class="id-col">${item.id.slice(0,8)}</td>
        <td class="${statusClass(item.status)}">${item.status}</td>
        <td>${item.attempt_count}/${item.max_retries+1}</td>
        <td>${ageStr(item.created_at)}</td>
        <td class="cmd-col" title="${escHtml(item.command_text)}">${escHtml(trunc(item.command_text, 60))}</td>
        <td>${cancelBtn}</td>
      </tr>`;
    }).join('');
  } catch(e) { console.error('queue refresh error', e); }
}

async function cancelItem(id) {
  await API('/api/queue/' + id + '/cancel', {method:'POST'});
  await refreshQueue();
}

async function purgeFailed() {
  const r = await API('/api/queue/purge-failed', {method:'POST'});
  if (r.deleted > 0) await refreshQueue();
}

// ── Runs ──────────────────────────────────────────────────────────────────

async function refreshRuns() {
  try {
    const runs = await API('/api/runs');
    const body = document.getElementById('runs-body');
    document.getElementById('runs-count').textContent = runs.length + ' runs';
    if (!runs.length) {
      body.innerHTML = '<tr><td colspan="5" class="empty-state">No runs</td></tr>';
      return;
    }
    body.innerHTML = runs.map(r => `<tr style="cursor:pointer" onclick="showRunDetail('${r.id}')">
      <td class="id-col">${r.id.slice(0,8)}</td>
      <td class="${statusClass(r.status)}">${r.status}</td>
      <td>✓${r.task_succeeded} ✗${r.task_failed} / ${r.task_total}</td>
      <td>${ageStr(r.created_at)}</td>
      <td>${escHtml(trunc(r.label, 50))}</td>
    </tr>`).join('');
  } catch(e) { console.error('runs refresh error', e); }
}

async function showRunDetail(runId) {
  document.getElementById('runs-view').style.display = 'none';
  const detail = document.getElementById('run-detail');
  detail.style.display = 'block';
  detail.innerHTML = '<span class="dim">Loading…</span>';
  try {
    const r = await API('/api/runs/' + runId);
    detail.innerHTML = `
      <span class="back-btn" onclick="backToRuns()">← Back to Runs</span>
      <h2>${escHtml(r.label || r.id)}</h2>
      <div class="dim">Run ID: ${r.id}</div>
      <div class="dim">Created: ${r.created_at}</div>
      <div class="section-title">Tasks (${r.tasks.length})</div>
      <table>
        <thead><tr><th>ID</th><th>Status</th><th>Title</th><th>Age</th><th>Error</th></tr></thead>
        <tbody>${r.tasks.map(t => `<tr>
          <td class="id-col">${t.id.slice(0,8)}</td>
          <td class="${statusClass(t.status)}">${t.status}</td>
          <td>${escHtml(t.title)}</td>
          <td>${ageStr(t.created_at)}</td>
          <td class="error-text">${escHtml(t.error || '')}</td>
        </tr>`).join('')}</tbody>
      </table>
      <div class="section-title">Tool Calls (${r.tool_calls.length})</div>
      <table>
        <thead><tr><th>ID</th><th>Tool</th><th>Status</th><th>Started</th><th>Error</th></tr></thead>
        <tbody>${r.tool_calls.map(tc => `<tr>
          <td class="id-col">${tc.id.slice(0,8)}</td>
          <td>${escHtml(tc.tool_name)}</td>
          <td class="${statusClass(tc.status)}">${tc.status}</td>
          <td>${ageStr(tc.started_at)}</td>
          <td class="error-text">${escHtml(tc.error || '')}</td>
        </tr>`).join('')}</tbody>
      </table>
    `;
  } catch(e) {
    detail.innerHTML = '<span class="back-btn" onclick="backToRuns()">← Back</span><div class="error-text">Failed to load run detail</div>';
  }
}

function backToRuns() {
  document.getElementById('run-detail').style.display = 'none';
  document.getElementById('runs-view').style.display = 'block';
}

// ── Memory ────────────────────────────────────────────────────────────────

async function refreshMemory() {
  try {
    const data = await API('/api/memory');
    const body = document.getElementById('memory-body');
    const namespaces = data.namespaces || [];
    const items = data.items || {};
    const total = Object.values(items).reduce((s,a) => s+a.length, 0);
    document.getElementById('memory-count').textContent = namespaces.length + ' namespaces, ' + total + ' items';

    if (!namespaces.length) {
      body.innerHTML = '<div class="empty-state">No memory items</div>';
      return;
    }
    body.innerHTML = namespaces.map(ns => {
      const nsItems = items[ns.namespace] || [];
      const retiredClass = ns.retired ? ' ns-retired' : '';
      const retiredBadge = ns.retired ? ' <span class="dim">[retired]</span>' : '';
      const rows = nsItems.map(item => `<tr>
        <td class="id-col">${item.key}</td>
        <td class="dim">${item.kind}</td>
        <td>${escHtml(String(item.value).slice(0,80))}</td>
        <td class="dim">${item.confidence}</td>
        <td class="dim">${item.source}</td>
        <td class="dim">${item.updated_at ? item.updated_at.slice(0,19) : '-'}</td>
      </tr>`).join('');
      return `<div class="ns-block${retiredClass}">
        <div class="ns-header">${escHtml(ns.namespace)}${retiredBadge} <span class="dim">(${ns.item_count} items)</span></div>
        ${nsItems.length ? `<table>
          <thead><tr><th>Key</th><th>Kind</th><th>Value</th><th>Conf</th><th>Source</th><th>Updated</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>` : '<div class="dim" style="padding:6px 10px">No items</div>'}
      </div>`;
    }).join('');
  } catch(e) { console.error('memory refresh error', e); }
}

// ── Auto-refresh ──────────────────────────────────────────────────────────

function escHtml(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

async function refreshAll() {
  const ind = document.getElementById('refresh-indicator');
  ind.classList.add('active');
  await Promise.all([refreshStatus(), refreshQueue(), refreshRuns(), refreshMemory()]);
  ind.classList.remove('active');
}

refreshAll();
setInterval(refreshAll, 5000);
</script>
</body>
</html>
"""
