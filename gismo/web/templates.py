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
  .btn:disabled { opacity: 0.4; cursor: default; }
  .btn-red { border-color: var(--red); color: var(--red); }
  .btn-red:hover { background: rgba(248,81,73,0.15); }
  .btn-yellow { border-color: var(--yellow); color: var(--yellow); }
  .btn-yellow:hover { background: rgba(210,153,34,0.15); }
  .btn-green { border-color: var(--green); color: var(--green); }
  .btn-green:hover { background: rgba(63,185,80,0.15); }
  .btn-blue { border-color: var(--blue); color: var(--blue); }
  .btn-blue:hover { background: rgba(88,166,255,0.15); }

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

  /* Plan approval */
  .risk-LOW { color: var(--green); }
  .risk-MEDIUM { color: var(--yellow); font-weight: bold; }
  .risk-HIGH { color: var(--red); font-weight: bold; }
  .plan-detail { max-width: 800px; }
  .plan-detail h2 { font-size: 14px; margin-bottom: 12px; }
  .plan-meta { color: var(--dim); font-size: 11px; margin-bottom: 12px; }
  .action-row { display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid var(--border); }
  .action-row:last-child { border-bottom: none; }
  .action-idx { color: var(--dim); font-size: 11px; width: 24px; flex-shrink: 0; }
  .action-cmd { flex: 1; background: var(--bg); border: 1px solid var(--border); color: var(--purple); padding: 3px 6px; border-radius: 4px; font-family: inherit; font-size: 12px; }
  .action-cmd:focus { outline: none; border-color: var(--blue); }
  .action-why { color: var(--dim); font-size: 11px; padding-left: 30px; }
  .rationale-list { margin: 8px 0; padding-left: 16px; color: var(--dim); font-size: 11px; }

  /* Chat */
  #tab-chat.active { display: flex; flex-direction: column; overflow: hidden; }
  .chat-history { flex: 1; overflow-y: auto; padding: 12px 16px; display: flex; flex-direction: column; gap: 10px; min-height: 0; }
  .chat-msg { max-width: 75%; padding: 8px 12px; border-radius: 8px; font-size: 13px; line-height: 1.5; word-wrap: break-word; }
  .chat-msg.user { align-self: flex-end; background: rgba(88,166,255,0.12); border: 1px solid rgba(88,166,255,0.25); }
  .chat-msg.assistant { align-self: flex-start; background: var(--panel); border: 1px solid var(--border); }
  .chat-msg .msg-role { font-size: 10px; color: var(--dim); margin-bottom: 4px; text-transform: uppercase; letter-spacing: 1px; }
  .chat-msg.thinking .msg-content { color: var(--dim); font-style: italic; }
  .chat-empty { flex: 1; display: flex; align-items: center; justify-content: center; color: var(--dim); font-size: 12px; }
  .chat-input-bar { padding: 10px 12px; border-top: 1px solid var(--border); display: flex; gap: 8px; align-items: center; background: var(--panel); flex-shrink: 0; }
  .chat-input-bar input { flex: 1; background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 6px 10px; border-radius: 4px; font-family: inherit; font-size: 13px; }
  .chat-input-bar input:focus { outline: none; border-color: var(--blue); }
  .btn-mic { font-size: 15px; padding: 4px 9px; }
  .btn-mic.recording { border-color: var(--red); color: var(--red); background: rgba(248,81,73,0.1); animation: mic-pulse 1s ease-in-out infinite; }
  @keyframes mic-pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }

  /* Settings / TTS */
  .settings-section { padding: 20px; max-width: 700px; }
  .settings-section h2 { font-size: 14px; margin-bottom: 16px; color: var(--text); }
  .settings-group { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 16px; margin-bottom: 16px; }
  .settings-group h3 { font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: var(--dim); margin-bottom: 12px; }
  .voice-row { display: flex; align-items: center; gap: 10px; padding: 6px 0; border-bottom: 1px solid var(--border); }
  .voice-row:last-child { border-bottom: none; }
  .voice-name { flex: 1; font-size: 12px; }
  .voice-meta { color: var(--dim); font-size: 11px; width: 80px; }
  .voice-lang { color: var(--dim); font-size: 11px; width: 60px; }
  .badge { display: inline-block; padding: 1px 5px; border-radius: 3px; font-size: 10px; font-weight: bold; margin-left: 4px; }
  .badge-selected { background: rgba(88,166,255,0.2); color: var(--blue); border: 1px solid var(--blue); }
  .badge-downloaded { background: rgba(63,185,80,0.1); color: var(--green); border: 1px solid var(--green); }
  .badge-default { background: rgba(188,140,255,0.1); color: var(--purple); border: 1px solid var(--purple); }
  .tts-test-row { display: flex; gap: 8px; margin-top: 12px; align-items: center; }
  .tts-test-row input { flex: 1; background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 4px 8px; border-radius: 4px; font-family: inherit; font-size: 12px; }
  .tts-test-row input:focus { outline: none; border-color: var(--blue); }
  #tts-status { font-size: 11px; color: var(--dim); margin-top: 6px; min-height: 16px; }
  select { background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 3px 6px; border-radius: 4px; font-family: inherit; font-size: 12px; }
  select:focus { outline: none; border-color: var(--blue); }
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
      <div class="tab" data-tab="plans" onclick="switchTab('plans')">Plans</div>
      <div class="tab" data-tab="chat" onclick="switchTab('chat')">Chat</div>
      <div class="tab" data-tab="settings" onclick="switchTab('settings')">Settings</div>
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

    <!-- Plans tab -->
    <div class="tab-content" id="tab-plans">
      <div class="toolbar">
        <select id="plan-status-filter" onchange="refreshPlans()">
          <option value="">All statuses</option>
          <option value="PENDING" selected>Pending</option>
          <option value="APPROVED">Approved</option>
          <option value="REJECTED">Rejected</option>
        </select>
        <span class="spacer"></span>
        <span class="dim" id="plans-count"></span>
      </div>
      <div id="plans-list-view">
        <table id="plans-table">
          <thead><tr>
            <th>ID</th><th>Status</th><th>Risk</th><th>Intent</th><th>Actions</th><th>Age</th><th></th>
          </tr></thead>
          <tbody id="plans-body"></tbody>
        </table>
      </div>
      <div id="plan-detail-view" style="display:none; padding:16px; overflow:auto; height:100%;"></div>
    </div>

    <!-- Chat tab -->
    <div class="tab-content" id="tab-chat">
      <div class="chat-history" id="chat-history">
        <div class="chat-empty" id="chat-empty">Ask GISMO anything about your queues, runs, plans, or memory.</div>
      </div>
      <div class="chat-input-bar">
        <input type="text" id="chat-input" placeholder="Message GISMO…" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendChat()}" />
        <button class="btn btn-mic" id="btn-mic" onclick="toggleMic()" title="Click to speak">🎤</button>
        <button class="btn btn-blue" id="btn-chat-send" onclick="sendChat()">Send</button>
      </div>
    </div>

    <!-- Settings tab -->
    <div class="tab-content" id="tab-settings">
      <div class="settings-section">
        <h2>Settings</h2>
        <div class="settings-group" id="tts-settings">
          <h3>Voice (TTS)</h3>
          <div id="voice-list"><span class="dim">Loading voices…</span></div>
          <div class="tts-test-row" style="margin-top:14px;">
            <select id="tts-voice-select" onchange="onVoiceSelectChange()"></select>
            <button class="btn btn-blue" onclick="setVoice()">Set as Default</button>
            <button class="btn btn-green" onclick="downloadVoice()">Download</button>
          </div>
          <div class="tts-test-row">
            <input type="text" id="tts-test-text" value="Hello from GISMO." placeholder="Enter text to speak…" />
            <button class="btn btn-blue" id="btn-speak" onclick="speakTest()">▶ Speak</button>
          </div>
          <div id="tts-status"></div>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
const API = (path, opts) => fetch(path, opts).then(r => {
  if (opts && opts.raw) return r;
  return r.json();
});
let _status = null;
let _paused = false;
let _voices = [];
let _currentVoice = null;

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
  if (name === 'settings') refreshVoices();
  if (name === 'plans') refreshPlans();
  if (name === 'chat') setTimeout(() => document.getElementById('chat-input')?.focus(), 50);
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

// ── TTS / Voice Settings ──────────────────────────────────────────────────

async function refreshVoices() {
  try {
    const data = await API('/api/tts/voices');
    _voices = data.voices || [];
    _currentVoice = data.current;

    // Populate select
    const sel = document.getElementById('tts-voice-select');
    sel.innerHTML = _voices.map(v =>
      `<option value="${v.id}" ${v.id === _currentVoice ? 'selected' : ''}>${v.name} (${v.lang}, ${v.quality})</option>`
    ).join('');

    // Render voice list table
    const list = document.getElementById('voice-list');
    list.innerHTML = `<table>
      <thead><tr><th>Voice</th><th>Lang</th><th>Quality</th><th>Status</th></tr></thead>
      <tbody>
      ${_voices.map(v => {
        const badges = [];
        if (v.is_selected) badges.push('<span class="badge badge-selected">selected</span>');
        if (v.is_default) badges.push('<span class="badge badge-default">default</span>');
        if (v.downloaded) badges.push('<span class="badge badge-downloaded">downloaded</span>');
        return `<tr>
          <td><span class="voice-name">${escHtml(v.name)}${badges.join('')}</span><br><span class="dim" style="font-size:10px">${escHtml(v.id)}</span></td>
          <td class="dim">${v.lang}</td>
          <td class="dim">${v.quality}</td>
          <td>${v.downloaded ? '<span style="color:var(--green)">ready</span>' : '<span class="dim">not downloaded</span>'}</td>
        </tr>`;
      }).join('')}
      </tbody>
    </table>`;
  } catch(e) {
    document.getElementById('voice-list').innerHTML = '<span class="error-text">Failed to load voices</span>';
  }
}

function onVoiceSelectChange() {
  // Nothing automatic — user presses "Set as Default" or "Download"
}

function getTtsStatus() { return document.getElementById('tts-status'); }

async function setVoice() {
  const voice = document.getElementById('tts-voice-select').value;
  if (!voice) return;
  try {
    await API('/api/tts/voices/set', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({voice})
    });
    getTtsStatus().innerHTML = `<span style="color:var(--green)">Voice set to ${escHtml(voice)}</span>`;
    await refreshVoices();
  } catch(e) {
    getTtsStatus().innerHTML = `<span class="error-text">Failed to set voice: ${e}</span>`;
  }
}

async function downloadVoice() {
  const voice = document.getElementById('tts-voice-select').value;
  if (!voice) return;
  const btn = document.querySelector('button[onclick="downloadVoice()"]');
  btn.disabled = true;
  getTtsStatus().innerHTML = `<span class="dim">Downloading ${escHtml(voice)}… (this may take a minute)</span>`;
  try {
    // Trigger download by synthesizing a silent/empty text — server will download the model
    const resp = await fetch('/api/tts/speak', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text: ' ', voice})
    });
    if (resp.ok) {
      getTtsStatus().innerHTML = `<span style="color:var(--green)">Downloaded ${escHtml(voice)}</span>`;
      await refreshVoices();
    } else {
      const err = await resp.json();
      getTtsStatus().innerHTML = `<span class="error-text">Download failed: ${escHtml(err.error || 'unknown error')}</span>`;
    }
  } catch(e) {
    getTtsStatus().innerHTML = `<span class="error-text">Download error: ${e}</span>`;
  } finally {
    btn.disabled = false;
  }
}

async function speakTest() {
  const text = document.getElementById('tts-test-text').value.trim();
  const voice = document.getElementById('tts-voice-select').value;
  if (!text) { getTtsStatus().textContent = 'Enter text first.'; return; }

  const btn = document.getElementById('btn-speak');
  btn.disabled = true;
  getTtsStatus().innerHTML = '<span class="dim">Synthesizing…</span>';

  try {
    const resp = await fetch('/api/tts/speak', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text, voice: voice || null})
    });
    if (!resp.ok) {
      const err = await resp.json();
      getTtsStatus().innerHTML = `<span class="error-text">${escHtml(err.error || 'Synthesis failed')}</span>`;
      return;
    }
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    audio.play();
    audio.onended = () => URL.revokeObjectURL(url);
    getTtsStatus().innerHTML = `<span style="color:var(--green)">▶ Playing (${escHtml(voice)})</span>`;
  } catch(e) {
    getTtsStatus().innerHTML = `<span class="error-text">Error: ${e}</span>`;
  } finally {
    btn.disabled = false;
  }
}

// ── Plans ─────────────────────────────────────────────────────────────────

async function refreshPlans() {
  try {
    const status = document.getElementById('plan-status-filter').value;
    const url = '/api/plans' + (status ? '?status=' + status : '');
    const plans = await API(url);
    document.getElementById('plans-count').textContent = plans.length + ' plan(s)';
    const body = document.getElementById('plans-body');
    if (!plans.length) {
      body.innerHTML = '<tr><td colspan="7" class="empty-state">No plans</td></tr>';
      return;
    }
    body.innerHTML = plans.map(p => {
      const flags = (p.risk_flags || []).join(', ');
      const flagStr = flags ? ` <span class="dim">[${escHtml(flags)}]</span>` : '';
      const isPending = p.status === 'PENDING';
      const approveBtn = isPending ? `<button class="btn btn-green" onclick="quickApprovePlan('${p.id}',event)">Approve</button>` : '';
      const rejectBtn = isPending ? `<button class="btn btn-red" onclick="quickRejectPlan('${p.id}',event)">Reject</button>` : '';
      return `<tr style="cursor:pointer" onclick="showPlanDetail('${p.id}')">
        <td class="id-col">${p.id.slice(0,8)}</td>
        <td class="${statusClass(p.status)}">${p.status.toLowerCase()}</td>
        <td class="risk-${p.risk_level}">${p.risk_level}${flagStr}</td>
        <td>${escHtml(trunc(p.intent, 40))}</td>
        <td>${p.action_count}</td>
        <td>${ageStr(p.created_at)}</td>
        <td onclick="event.stopPropagation()" style="display:flex;gap:4px">${approveBtn}${rejectBtn}</td>
      </tr>`;
    }).join('');
  } catch(e) { console.error('plans refresh error', e); }
}

async function showPlanDetail(planId) {
  document.getElementById('plans-list-view').style.display = 'none';
  const view = document.getElementById('plan-detail-view');
  view.style.display = 'block';
  view.innerHTML = '<span class="dim">Loading…</span>';
  try {
    const p = await API('/api/plans/' + planId);
    const isPending = p.status === 'PENDING';
    const actions = (p.plan || {}).actions || [];
    const rationale = (p.risk || {}).rationale || [];
    const riskFlags = (p.risk || {}).risk_flags || [];

    const actionsHtml = actions.map((a, i) => `
      <div class="action-row" id="action-row-${i}">
        <span class="action-idx">[${i}]</span>
        <input class="action-cmd" id="action-cmd-${i}" value="${escHtml(a.command||'')}" ${isPending ? '' : 'readonly'} />
        ${isPending ? `<button class="btn btn-red" onclick="removeAction('${planId}',${i})">×</button>` : ''}
      </div>
      ${a.why ? `<div class="action-why">why: ${escHtml(a.why)}</div>` : ''}
    `).join('');

    const rationaleHtml = rationale.length
      ? `<ul class="rationale-list">${rationale.map(r => `<li>${escHtml(r)}</li>`).join('')}</ul>`
      : '';

    const approveRejectHtml = isPending ? `
      <div style="display:flex;gap:8px;margin-top:16px;">
        <button class="btn btn-green" onclick="saveAndApprovePlan('${planId}')">✓ Save edits &amp; Approve</button>
        <button class="btn btn-red" onclick="rejectPlanDetail('${planId}')">✗ Reject</button>
      </div>` : '';

    view.innerHTML = `
      <div class="plan-detail">
        <span class="back-btn" onclick="backToPlans()">← Back to Plans</span>
        <h2>${escHtml(p.intent)}</h2>
        <div class="plan-meta">
          ID: ${p.id} &nbsp;|&nbsp;
          Status: <span class="${statusClass(p.status)}">${p.status.toLowerCase()}</span> &nbsp;|&nbsp;
          Risk: <span class="risk-${p.risk_level}">${p.risk_level}</span>
          ${riskFlags.length ? `[${escHtml(riskFlags.join(', '))}]` : ''}
        </div>
        <div class="plan-meta">Prompt: <em>${escHtml(p.user_text)}</em></div>
        ${rationaleHtml}
        <div class="section-title">Actions (${actions.length})</div>
        <div id="actions-list">${actionsHtml || '<div class="dim">No actions</div>'}</div>
        ${approveRejectHtml}
        ${p.rejection_reason ? `<div class="error-text" style="margin-top:8px">Rejected: ${escHtml(p.rejection_reason)}</div>` : ''}
      </div>`;
  } catch(e) {
    view.innerHTML = '<span class="back-btn" onclick="backToPlans()">← Back</span><div class="error-text">Failed to load plan</div>';
  }
}

function backToPlans() {
  document.getElementById('plan-detail-view').style.display = 'none';
  document.getElementById('plans-list-view').style.display = 'block';
}

async function removeAction(planId, idx) {
  try {
    await fetch('/api/plans/' + planId, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action_index: idx, remove_action: true})
    });
    await showPlanDetail(planId);
  } catch(e) { alert('Error: ' + e); }
}

async function saveAndApprovePlan(planId) {
  // Save any edited commands first
  const inputs = document.querySelectorAll('[id^="action-cmd-"]');
  for (const input of inputs) {
    const idx = parseInt(input.id.replace('action-cmd-', ''));
    const newCmd = input.value.trim();
    if (newCmd) {
      await fetch('/api/plans/' + planId, {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action_index: idx, command: newCmd})
      });
    }
  }
  const r = await fetch('/api/plans/' + planId + '/approve', {method: 'POST'});
  const data = await r.json();
  if (r.ok) {
    await showPlanDetail(planId);
    await refreshPlans();
  } else {
    alert('Approve failed: ' + (data.error || 'unknown error'));
  }
}

async function rejectPlanDetail(planId) {
  const reason = prompt('Rejection reason (optional):') || null;
  const r = await fetch('/api/plans/' + planId + '/reject', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({reason})
  });
  if (r.ok) {
    await showPlanDetail(planId);
    await refreshPlans();
  } else {
    const data = await r.json();
    alert('Reject failed: ' + (data.error || 'unknown error'));
  }
}

async function quickApprovePlan(planId, event) {
  event.stopPropagation();
  const r = await fetch('/api/plans/' + planId + '/approve', {method: 'POST'});
  if (r.ok) await refreshPlans();
  else { const d = await r.json(); alert('Error: ' + (d.error||'unknown')); }
}

async function quickRejectPlan(planId, event) {
  event.stopPropagation();
  const r = await fetch('/api/plans/' + planId + '/reject', {
    method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}'
  });
  if (r.ok) await refreshPlans();
  else { const d = await r.json(); alert('Error: ' + (d.error||'unknown')); }
}

// ── Chat ──────────────────────────────────────────────────────────────────

let _chatHistory = [];
let _micActive = false;
let _recognition = null;

function _appendChatMsg(role, content, opts = {}) {
  const empty = document.getElementById('chat-empty');
  if (empty) empty.remove();
  const hist = document.getElementById('chat-history');
  const el = document.createElement('div');
  el.className = 'chat-msg ' + role + (opts.thinking ? ' thinking' : '');
  if (opts.id) el.id = opts.id;
  el.innerHTML = `<div class="msg-role">${role === 'user' ? 'You' : 'GISMO'}</div><div class="msg-content">${escHtml(content)}</div>`;
  hist.appendChild(el);
  hist.scrollTop = hist.scrollHeight;
  return el;
}

async function sendChat() {
  const input = document.getElementById('chat-input');
  const message = input.value.trim();
  if (!message) return;
  input.value = '';
  input.disabled = true;
  document.getElementById('btn-chat-send').disabled = true;

  _appendChatMsg('user', message);
  const historyToSend = [..._chatHistory];
  _chatHistory.push({role: 'user', content: message});

  const thinkingId = 'chat-thinking-' + Date.now();
  _appendChatMsg('assistant', '…', {thinking: true, id: thinkingId});

  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message, history: historyToSend})
    });
    const data = await resp.json();
    document.getElementById(thinkingId)?.remove();
    if (!resp.ok) {
      _appendChatMsg('assistant', 'Error: ' + escHtml(data.error || 'unknown error'));
      _chatHistory.pop(); // remove the user message we added optimistically
      return;
    }
    const reply = data.reply || '';
    _appendChatMsg('assistant', reply);
    _chatHistory.push({role: 'assistant', content: reply});
    _speakChatReply(reply);
  } catch(e) {
    document.getElementById(thinkingId)?.remove();
    _appendChatMsg('assistant', 'Error: ' + e);
    _chatHistory.pop();
  } finally {
    input.disabled = false;
    document.getElementById('btn-chat-send').disabled = false;
    input.focus();
  }
}

async function _speakChatReply(text) {
  if (!text) return;
  try {
    const resp = await fetch('/api/tts/speak', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text})
    });
    if (!resp.ok) return;
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    audio.play();
    audio.onended = () => URL.revokeObjectURL(url);
  } catch(e) { /* TTS is best-effort */ }
}

function toggleMic() {
  if (_micActive) { stopMic(); } else { startMic(); }
}

function startMic() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) {
    alert('Speech recognition is not supported in this browser. Try Chrome or Edge.');
    return;
  }
  _recognition = new SR();
  _recognition.continuous = false;
  _recognition.interimResults = false;
  _recognition.lang = 'en-US';
  _recognition.onstart = () => {
    _micActive = true;
    document.getElementById('btn-mic').classList.add('recording');
  };
  _recognition.onresult = (event) => {
    document.getElementById('chat-input').value = event.results[0][0].transcript;
  };
  _recognition.onend = () => {
    _micActive = false;
    document.getElementById('btn-mic').classList.remove('recording');
    const val = document.getElementById('chat-input').value.trim();
    if (val) sendChat();
  };
  _recognition.onerror = () => {
    _micActive = false;
    document.getElementById('btn-mic').classList.remove('recording');
  };
  _recognition.start();
}

function stopMic() {
  if (_recognition) { _recognition.stop(); _recognition = null; }
  _micActive = false;
  document.getElementById('btn-mic').classList.remove('recording');
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
