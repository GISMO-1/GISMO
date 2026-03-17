"""GISMO web dashboard — mission control layout."""
from __future__ import annotations

HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GISMO — Mission Control</title>
<style>
:root {
  --bg: #0e0e11;
  --panel: #141417;
  --border: #1f1f26;
  --text: #e2e2e8;
  --dim: #64647a;
  --accent: #4ecdc4;
  --accent-dim: rgba(78,205,196,0.10);
  --accent-glow: rgba(78,205,196,0.22);
  --green: #4ade80;
  --yellow: #fbbf24;
  --red: #f87171;
  --blue: #60a5fa;
  --gray: #3a3a48;
  --font: 'Cascadia Code','Consolas','SF Mono','Courier New',monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden}
body{background:var(--bg);color:var(--text);font-family:var(--font);font-size:13px;display:flex;flex-direction:column}

/* TOP BAR */
#topbar{display:flex;align-items:center;gap:14px;padding:0 20px;height:50px;background:var(--panel);border-bottom:1px solid var(--border);flex-shrink:0;z-index:10}
#logo{font-size:15px;font-weight:700;letter-spacing:3px;color:var(--accent);text-shadow:0 0 18px var(--accent-glow);flex-shrink:0}
#status-pill{display:flex;align-items:center;gap:6px;padding:3px 12px;border-radius:20px;font-size:10px;font-weight:700;letter-spacing:1px;flex-shrink:0;transition:all .3s}
.pill-online {background:rgba(74,222,128,.10);color:var(--green); border:1px solid rgba(74,222,128,.28)}
.pill-offline{background:rgba(248,113,113,.10);color:var(--red);   border:1px solid rgba(248,113,113,.28)}
.pill-paused {background:rgba(251,191,36,.10); color:var(--yellow);border:1px solid rgba(251,191,36,.28)}
.pill-dot{width:6px;height:6px;border-radius:50%}
.dot-green {background:var(--green); box-shadow:0 0 6px var(--green);animation:pglow 2s infinite}
.dot-yellow{background:var(--yellow)}
.dot-red   {background:var(--red)}
@keyframes pglow{0%,100%{box-shadow:0 0 4px var(--green)}50%{box-shadow:0 0 10px var(--green)}}
#search{flex:1;max-width:340px;margin:0 auto;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:6px 16px;border-radius:20px;font-family:var(--font);font-size:12px;outline:none;transition:border-color .2s}
#search:focus{border-color:var(--accent)}
#search::placeholder{color:var(--dim)}
#op-badge{margin-left:auto;font-size:11px;color:var(--dim);flex-shrink:0}
#op-badge span{color:var(--accent)}
#top-actions{margin-left:auto;display:flex;align-items:center;gap:10px}
.top-icon{width:32px;height:32px}

/* GRID */
#grid{display:grid;grid-template-columns:260px 1fr 240px;flex:1;overflow:hidden;min-height:0}

/* PANEL BASE */
.panel{background:var(--panel);display:flex;flex-direction:column;overflow:hidden;min-height:0}
#left-panel {border-right:1px solid var(--border)}
#right-panel{border-left: 1px solid var(--border)}
#center{background:var(--bg)}
.sec-hdr{padding:10px 14px 8px;border-bottom:1px solid var(--border);flex-shrink:0}
.sec-ttl{font-size:9px;font-weight:700;letter-spacing:2px;color:var(--dim);text-transform:uppercase}

/* DEVICES */
#dev-scroll{flex:1;overflow-y:auto;padding:8px;min-height:0;display:flex;flex-direction:column;gap:8px}
.dev-card{padding:10px;border:1px solid var(--border);border-radius:10px;background:rgba(255,255,255,.02)}
.dev-head{display:flex;gap:10px;align-items:flex-start}
.dev-thumb{width:84px;height:52px;border-radius:8px;overflow:hidden;background:var(--bg);border:1px solid var(--border);flex-shrink:0;cursor:pointer}
.dev-thumb img{width:100%;height:100%;object-fit:cover;display:block}
.dev-thumb-empty{width:100%;height:100%;display:flex;align-items:center;justify-content:center;color:var(--dim);font-size:10px}
.d-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.d-on {background:var(--green);box-shadow:0 0 5px var(--green)}
.d-alt{background:var(--yellow)}
.d-off{background:var(--gray)}
.d-info{flex:1;min-width:0}
.d-title{display:flex;align-items:center;gap:8px}
.d-name{font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.d-type{font-size:10px;color:var(--dim);margin-top:4px}
.d-ip{font-size:10px;color:var(--dim);margin-top:3px}
.dev-actions{display:flex;gap:6px;margin-top:8px}
.mini-btn{padding:6px 8px;border-radius:7px;border:1px solid var(--border);background:transparent;color:var(--text);font-family:var(--font);font-size:10px;cursor:pointer}
.mini-btn:hover{border-color:var(--accent);color:var(--accent)}
.mini-btn[disabled]{opacity:.45;cursor:default;border-color:var(--border);color:var(--dim)}
#add-dev-btn{margin:6px;padding:8px;background:transparent;border:1px dashed var(--border);color:var(--dim);border-radius:7px;cursor:pointer;font-family:var(--font);font-size:11px;text-align:center;transition:all .2s;flex-shrink:0}
#add-dev-btn:hover{border-color:var(--accent);color:var(--accent)}

/* HEALTH */
.health-sec{padding:12px 14px;border-top:1px solid var(--border);flex-shrink:0}
.hb{margin-bottom:9px}
.hb:last-child{margin-bottom:0}
.hb-row{display:flex;justify-content:space-between;font-size:10px;margin-bottom:4px}
.hb-key{color:var(--dim)}.hb-val{color:var(--text)}
.hb-track{height:3px;background:var(--bg);border-radius:2px;overflow:hidden}
.hb-fill{height:100%;border-radius:2px;transition:width .6s ease}
.fill-cpu{background:var(--accent)}
.fill-ram{background:var(--blue)}
.fill-net{background:var(--green)}

/* DAEMON */
.daemon-sec{padding:10px 14px;border-top:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-shrink:0}
.daemon-lbl{font-size:10px;color:var(--dim);margin-bottom:2px}
.daemon-val{font-size:12px}
.ctrl-btn{padding:4px 12px;border-radius:5px;border:1px solid var(--border);background:transparent;color:var(--dim);font-family:var(--font);font-size:11px;cursor:pointer;transition:all .15s}
.ctrl-btn:hover{border-color:var(--accent);color:var(--accent)}

/* CHAT */
#chat-feed{flex:1;overflow-y:auto;padding:20px 24px;display:flex;flex-direction:column;gap:14px;min-height:0}
.msg{display:flex;gap:10px;max-width:78%}
.msg-gismo{align-self:flex-start}
.msg-user {align-self:flex-end;flex-direction:row-reverse}
.msg-av{width:30px;height:30px;border-radius:7px;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;flex-shrink:0}
.av-g{background:var(--accent-dim);color:var(--accent);border:1px solid var(--accent-glow)}
.av-u{background:rgba(96,165,250,.10);color:var(--blue);border:1px solid rgba(96,165,250,.25)}
.msg-body{display:flex;flex-direction:column}
.msg-user .msg-body{align-items:flex-end}
.bubble{padding:10px 14px;border-radius:10px;line-height:1.6;font-size:13px;max-width:100%;word-break:break-word;white-space:pre-wrap}
.bbl-g{background:var(--panel);border:1px solid var(--border)}
.bbl-u{background:var(--accent-dim);border:1px solid var(--accent-glow)}
.msg-ts{font-size:10px;color:var(--dim);margin-top:4px}
.typing-dots{display:flex;gap:4px;padding:2px 0}
.td{width:7px;height:7px;border-radius:50%;background:var(--dim);animation:tdp 1.2s infinite}
.td:nth-child(2){animation-delay:.2s}.td:nth-child(3){animation-delay:.4s}
@keyframes tdp{0%,80%,100%{opacity:.3;transform:scale(.8)}40%{opacity:1;transform:scale(1.1)}}

/* CHAT INPUT */
#chat-bar{padding:12px 20px;border-top:1px solid var(--border);display:flex;gap:8px;align-items:flex-end;background:var(--panel);flex-shrink:0}
#chat-input{flex:1;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:9px 14px;border-radius:20px;font-family:var(--font);font-size:13px;outline:none;resize:none;line-height:1.4;max-height:100px;overflow-y:auto;transition:border-color .2s}
#chat-input:focus{border-color:var(--accent)}
#chat-input::placeholder{color:var(--dim)}
.icon-btn{width:38px;height:38px;border-radius:50%;border:1px solid var(--border);background:transparent;color:var(--dim);cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .15s;flex-shrink:0}
.icon-btn:hover{border-color:var(--accent);color:var(--accent)}
.icon-btn.mic-on{background:rgba(248,113,113,.10);border-color:var(--red);color:var(--red)}
#send-btn{width:38px;height:38px;border-radius:50%;border:none;background:var(--accent);color:var(--bg);cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:opacity .15s,transform .1s}
#send-btn:hover{opacity:.85;transform:scale(1.06)}
#send-btn:disabled{opacity:.35;cursor:not-allowed;transform:none}

/* ACTIVITY */
#act-scroll{flex:1;overflow-y:auto;padding:6px;min-height:0}
.ev{padding:7px 9px;margin-bottom:4px;border-radius:6px;border-left:2px solid var(--gray);background:rgba(255,255,255,.02);transition:background .15s}
.ev:hover{background:rgba(255,255,255,.04)}
.c-green {border-color:var(--green)}
.c-teal  {border-color:var(--accent)}
.c-blue  {border-color:var(--blue)}
.c-red   {border-color:var(--red)}
.c-yellow{border-color:var(--yellow)}
.c-gray  {border-color:var(--gray)}
.ev-lbl{font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ev-meta{display:flex;justify-content:space-between;margin-top:3px;font-size:9px;color:var(--dim)}

/* QUEUE STATS */
.stats-sec{padding:12px;border-top:1px solid var(--border);flex-shrink:0}
.stat-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.stat-card{background:var(--bg);border:1px solid var(--border);border-radius:7px;padding:8px 10px;text-align:center}
.stat-n{font-size:20px;font-weight:700}
.n-def  {color:var(--text)}
.n-teal {color:var(--accent)}
.n-green{color:var(--green)}
.n-red  {color:var(--red)}
.stat-l{font-size:9px;color:var(--dim);margin-top:2px;letter-spacing:.5px}

/* OVERLAYS */
.overlay{position:fixed;inset:0;background:rgba(14,14,17,.90);display:flex;align-items:center;justify-content:center;z-index:200;backdrop-filter:blur(6px)}
.overlay.hidden{display:none}
.modal{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:32px 36px;width:480px;max-width:92vw}
.ob-brand{color:var(--accent);font-size:20px;font-weight:700;letter-spacing:3px;margin-bottom:4px}
.ob-sub{color:var(--dim);font-size:11px;margin-bottom:28px}
.ob-step{display:none}.ob-step.active{display:block}
.ob-h{font-size:16px;margin-bottom:6px}
.ob-p{font-size:12px;color:var(--dim);line-height:1.6;margin-bottom:20px}
.field-input,.modal-input{width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:10px 14px;border-radius:8px;font-family:var(--font);font-size:13px;outline:none;margin-bottom:14px;transition:border-color .2s}
.field-input:focus,.modal-input:focus{border-color:var(--accent)}
.voice-list{max-height:195px;overflow-y:auto;display:flex;flex-direction:column;gap:6px;margin-bottom:16px}
.vi{padding:9px 12px;border:1px solid var(--border);border-radius:8px;cursor:pointer;transition:all .15s}
.vi:hover,.vi.sel{border-color:var(--accent);background:var(--accent-dim)}
.vi-name{font-size:12px}.vi-desc{font-size:10px;color:var(--dim);margin-top:2px}
.primary-btn{width:100%;padding:11px;background:var(--accent);border:none;color:var(--bg);border-radius:8px;font-family:var(--font);font-size:13px;font-weight:700;cursor:pointer;transition:opacity .15s}
.primary-btn:hover{opacity:.85}.primary-btn:disabled{opacity:.35;cursor:not-allowed}
.sm-modal{width:360px;padding:24px 28px}
.sm-ttl{font-size:14px;margin-bottom:16px}
.modal-actions{display:flex;gap:8px;margin-top:6px}
.cancel-btn{flex:1;padding:9px;border-radius:7px;background:var(--bg);border:1px solid var(--border);color:var(--dim);font-family:var(--font);font-size:12px;cursor:pointer}
.confirm-btn{flex:1;padding:9px;border-radius:7px;background:var(--accent);border:none;color:var(--bg);font-family:var(--font);font-size:12px;font-weight:700;cursor:pointer}
.scan-status{font-size:12px;color:var(--dim);margin-bottom:14px}
.scan-spinner{width:26px;height:26px;border:2px solid rgba(255,255,255,.08);border-top-color:var(--accent);border-radius:50%;animation:spin 1s linear infinite;margin-bottom:14px}
.scan-grid{display:grid;grid-template-columns:1fr;gap:8px;max-height:320px;overflow:auto}
.scan-card{border:1px solid var(--border);border-radius:10px;padding:12px;background:var(--bg);display:flex;align-items:center;justify-content:space-between;gap:10px}
.scan-main{min-width:0}
.scan-name{font-size:12px}
.scan-meta{font-size:10px;color:var(--dim);margin-top:4px}
.scan-empty{font-size:12px;color:var(--dim);padding:18px 0;text-align:center}
.viewer-modal{width:min(860px,92vw);padding:20px}
.viewer-frame{width:100%;height:min(70vh,520px);border:1px solid var(--border);border-radius:12px;overflow:hidden;background:var(--bg)}
.viewer-frame img{width:100%;height:100%;object-fit:contain;display:block}
.settings-grid{display:flex;flex-direction:column;gap:12px}
.field-label{font-size:10px;color:var(--dim);letter-spacing:1px;text-transform:uppercase}
.field-note{font-size:11px;color:var(--dim)}
@keyframes spin{to{transform:rotate(360deg)}}

/* SCROLLBARS */
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
::-webkit-scrollbar-thumb:hover{background:var(--dim)}
</style>
</head>
<body>

<!-- TOP BAR -->
<div id="topbar">
  <div id="logo">GISMO</div>
  <div id="status-pill" class="pill-offline">
    <div class="pill-dot dot-red" id="s-dot"></div>
    <span id="s-txt">OFFLINE</span>
  </div>
  <input id="search" type="text" placeholder="Search commands, runs, memory…" oninput="onSearch(this.value)" />
  <div id="top-actions">
    <div id="op-badge">operator: <span id="op-name">—</span></div>
    <button class="icon-btn top-icon" id="settings-btn" onclick="openSettings()" title="Settings">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor">
        <path d="M19.14 12.94a7.49 7.49 0 0 0 .05-.94 7.49 7.49 0 0 0-.05-.94l2.03-1.58a.5.5 0 0 0 .12-.64l-1.92-3.32a.5.5 0 0 0-.6-.22l-2.39.96a7.16 7.16 0 0 0-1.63-.94L14.5 2.5a.5.5 0 0 0-.49-.4h-4.02a.5.5 0 0 0-.49.4l-.36 2.58a7.16 7.16 0 0 0-1.63.94l-2.39-.96a.5.5 0 0 0-.6.22L2.6 8.6a.5.5 0 0 0 .12.64l2.03 1.58a7.49 7.49 0 0 0-.05.94 7.49 7.49 0 0 0 .05.94L2.72 14.3a.5.5 0 0 0-.12.64l1.92 3.32a.5.5 0 0 0 .6.22l2.39-.96c.5.39 1.04.71 1.63.94l.36 2.58a.5.5 0 0 0 .49.4h4.02a.5.5 0 0 0 .49-.4l.36-2.58c.59-.23 1.13-.55 1.63-.94l2.39.96a.5.5 0 0 0 .6-.22l1.92-3.32a.5.5 0 0 0-.12-.64zM12 15.5A3.5 3.5 0 1 1 12 8a3.5 3.5 0 0 1 0 7.5z"/>
      </svg>
    </button>
  </div>
</div>

<!-- GRID -->
<div id="grid">

  <!-- LEFT: DEVICES + HEALTH -->
  <div class="panel" id="left-panel">
    <div class="sec-hdr"><div class="sec-ttl">Connected Devices</div></div>
    <div id="dev-scroll"></div>
    <button id="add-dev-btn" onclick="openAddDev()">+ Add Device</button>

    <div class="health-sec">
      <div class="sec-ttl" style="margin-bottom:10px">System Health</div>
      <div class="hb">
        <div class="hb-row"><span class="hb-key">CPU</span><span class="hb-val" id="cpu-v">—</span></div>
        <div class="hb-track"><div class="hb-fill fill-cpu" id="cpu-b" style="width:0"></div></div>
      </div>
      <div class="hb">
        <div class="hb-row"><span class="hb-key">RAM</span><span class="hb-val" id="ram-v">—</span></div>
        <div class="hb-track"><div class="hb-fill fill-ram" id="ram-b" style="width:0"></div></div>
      </div>
      <div class="hb">
        <div class="hb-row"><span class="hb-key">NET</span><span class="hb-val" id="net-v">—</span></div>
        <div class="hb-track"><div class="hb-fill fill-net" id="net-b" style="width:0"></div></div>
      </div>
    </div>

    <div class="daemon-sec">
      <div>
        <div class="daemon-lbl">Daemon</div>
        <div class="daemon-val" id="daemon-val">—</div>
      </div>
      <button class="ctrl-btn" id="pause-btn" onclick="togglePause()">Pause</button>
    </div>
  </div>

  <!-- CENTER: CHAT -->
  <div class="panel" id="center">
    <div id="chat-feed"></div>
    <div id="chat-bar">
      <button class="icon-btn" id="mic-btn" onclick="toggleMic()" title="Voice input">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor">
          <path d="M12 1a4 4 0 0 1 4 4v7a4 4 0 0 1-8 0V5a4 4 0 0 1 4-4zm0 2a2 2 0 0 0-2 2v7a2 2 0 1 0 4 0V5a2 2 0 0 0-2-2zm-7 9a7 7 0 0 0 14 0h2a9 9 0 0 1-8 8.94V23h-2v-2.06A9 9 0 0 1 3 12h2z"/>
        </svg>
      </button>
      <textarea id="chat-input" placeholder="Message GISMO…" rows="1"
        onkeydown="onKey(event)" oninput="autoResize(this)"></textarea>
      <button id="send-btn" onclick="sendChat()">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
          <path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/>
        </svg>
      </button>
    </div>
  </div>

  <!-- RIGHT: ACTIVITY + STATS -->
  <div class="panel" id="right-panel">
    <div class="sec-hdr"><div class="sec-ttl">Activity Feed</div></div>
    <div id="act-scroll"></div>
    <div class="stats-sec">
      <div class="sec-ttl" style="margin-bottom:8px">Queue</div>
      <div class="stat-grid">
        <div class="stat-card"><div class="stat-n n-def"   id="q-queued">—</div><div class="stat-l">Queued</div></div>
        <div class="stat-card"><div class="stat-n n-teal"  id="q-running">—</div><div class="stat-l">Running</div></div>
        <div class="stat-card"><div class="stat-n n-green" id="q-done">—</div><div class="stat-l">Done</div></div>
        <div class="stat-card"><div class="stat-n n-red"   id="q-failed">—</div><div class="stat-l">Failed</div></div>
      </div>
    </div>
  </div>

</div>

<!-- ONBOARDING -->
<div class="overlay hidden" id="ob-overlay">
  <div class="modal">
    <div class="ob-brand">GISMO</div>
    <div class="ob-sub">General Intelligent System for Multiflow Operations</div>
    <div class="ob-step active" id="ob-s1">
      <div class="ob-h">Welcome, Operator.</div>
      <div class="ob-p">GISMO runs entirely on your hardware — no cloud, no silent actions, full audit trail. What should I call you?</div>
      <input class="field-input" id="ob-name" type="text" placeholder="Your name" />
      <button class="primary-btn" onclick="obNext()">Continue →</button>
    </div>
    <div class="ob-step" id="ob-s2">
      <div class="ob-h">Choose a voice.</div>
      <div class="ob-p">GISMO can speak to you using text-to-speech. Pick a voice to get started.</div>
      <div class="voice-list" id="ob-voices"></div>
      <button class="primary-btn" id="ob-done-btn" onclick="obFinish()" disabled>Get Started</button>
    </div>
  </div>
</div>

<!-- ADD DEVICE -->
<div class="overlay hidden" id="dev-overlay">
  <div class="modal">
    <div class="sm-ttl">Finding devices on your network</div>
    <div id="scan-loading">
      <div class="scan-spinner"></div>
      <div class="scan-status">Scanning your network…</div>
    </div>
    <div class="scan-grid" id="scan-results"></div>
    <div class="modal-actions">
      <button class="cancel-btn" onclick="closeAddDev()">Close</button>
      <button class="confirm-btn" onclick="scanDevices()">Scan Again</button>
    </div>
  </div>
</div>

<!-- VIEWER -->
<div class="overlay hidden" id="viewer-overlay">
  <div class="modal viewer-modal">
    <div class="sm-ttl" id="viewer-title">Live view</div>
    <div class="viewer-frame"><img id="viewer-image" alt="Device preview" /></div>
    <div class="modal-actions">
      <button class="cancel-btn" onclick="closeViewer()">Close</button>
    </div>
  </div>
</div>

<!-- SETTINGS -->
<div class="overlay hidden" id="settings-overlay">
  <div class="modal">
    <div class="sm-ttl">Settings</div>
    <div class="settings-grid">
      <div>
        <div class="field-label">Operator Name</div>
        <input class="field-input" id="settings-name" type="text" placeholder="Your name" />
      </div>
      <div>
        <div class="field-label">Voice</div>
        <select class="field-input" id="settings-voice"></select>
        <div class="modal-actions">
          <button class="cancel-btn" onclick="previewSettingsVoice()">Preview</button>
        </div>
      </div>
      <div>
        <div class="field-label">Theme</div>
        <div class="field-note">More themes are coming soon.</div>
      </div>
    </div>
    <div class="modal-actions">
      <button class="cancel-btn" onclick="closeSettings()">Cancel</button>
      <button class="confirm-btn" onclick="saveSettings()">Save</button>
    </div>
  </div>
</div>

<script>
// ── State ─────────────────────────────────────────────────────────────────────
var chatHistory   = [];
var daemonPaused  = false;
var micActive     = false;
var micRec        = null;
var obVoice       = null;
var briefingDone  = false;
var ttsEnabled    = true;   // operator can toggle via mic mute concept
var currentAudio  = null;
var lastScan      = [];

// ── Boot ──────────────────────────────────────────────────────────────────────
async function init() {
  var ob = await get('/api/onboarding');
  if (!ob) {
    addMsg('gismo', 'Could not reach GISMO server. Is it running?');
  } else if (ob.needs_onboarding) {
    showOb();
  } else {
    setOp(ob.operator_name);
    maybeBriefing();
  }
  await refreshStatus();
  await refreshHealth();
  await refreshActivity();
  await refreshDevices();
  setInterval(refreshStatus, 5000);
  setInterval(refreshHealth, 5000);
  setInterval(refreshActivity, 5000);
  setInterval(refreshDevices, 10000);
}

// ── 3. Daemon status + 4. Queue stats ────────────────────────────────────────
async function refreshStatus() {
  var data = await get('/api/status');
  if (!data) return;
  updatePill(data.daemon || {});
  updateStats(data.queue || {});
}

function updatePill(d) {
  var pill = $('status-pill'), dot = $('s-dot'), txt = $('s-txt');
  var online = !!d.running && !d.stale;
  if (online) {
    pill.className = 'pill-online';
    dot.className  = 'pill-dot dot-green';
    txt.textContent = 'ONLINE';
  } else {
    pill.className = 'pill-offline';
    dot.className  = 'pill-dot dot-red';
    txt.textContent = 'OFFLINE';
  }
  daemonPaused = !!d.paused;
  $('pause-btn').textContent  = daemonPaused ? 'Resume' : 'Pause';
  $('daemon-val').textContent = !d.running ? 'offline'
    : d.paused ? 'paused' : d.stale ? 'stale' : 'running';
}

function updateStats(q) {
  var b = q.by_status || {};
  $('q-queued').textContent  = b.QUEUED      != null ? b.QUEUED      : 0;
  $('q-running').textContent = b.IN_PROGRESS != null ? b.IN_PROGRESS : 0;
  $('q-done').textContent    = b.SUCCEEDED   != null ? b.SUCCEEDED   : 0;
  $('q-failed').textContent  = b.FAILED      != null ? b.FAILED      : 0;
}

// ── 1. Chat briefing on load ──────────────────────────────────────────────────
async function maybeBriefing() {
  if (briefingDone) return;
  briefingDone = true;
  var d = await get('/api/briefing');
  if (d && d.briefing) {
    addMsg('gismo', d.briefing);
    speakText(d.briefing);
  }
}

// ── 5. System health ──────────────────────────────────────────────────────────
async function refreshHealth() {
  var data = await get('/api/health');
  if (!data) return;
  var cpu = Math.round(data.cpu_percent || 0);
  var ram = Math.round(data.virtual_memory || 0);
  bar('cpu', cpu, cpu + '%');
  bar('ram', ram, ram + '%');
  bar('net', 100, 'OK');
}

function bar(k, pct, label) {
  $(k + '-v').textContent    = label;
  $(k + '-b').style.width    = pct + '%';
}

// ── Devices ───────────────────────────────────────────────────────────────────
async function refreshDevices() {
  var devs = await get('/api/devices/list') || [];
  var el = $('dev-scroll');
  var localDot = ($('s-txt').textContent === 'ONLINE') ? 'd-on' : 'd-off';
  var html = '<div class="dev-card"><div class="dev-head">'
    + '<div class="dev-thumb"><div class="dev-thumb-empty">GISMO</div></div>'
    + '<div class="d-info"><div class="d-title"><div class="d-dot ' + localDot + '"></div><div class="d-name">This computer</div></div>'
    + '<div class="d-type">Local daemon</div><div class="d-ip">127.0.0.1</div></div></div></div>';
  devs.forEach(function(device) {
    var dot = device.status === 'online' ? 'd-on' : 'd-off';
    var thumb = device.thumbnail_url
      ? '<div class="dev-thumb js-open-viewer" data-stream="' + esc(device.stream_url) + '" data-title="'
          + esc(device.name) + '"><img src="' + esc(device.thumbnail_url) + '?t=' + Date.now() + '" alt="'
          + esc(device.name) + '" /></div>'
      : '<div class="dev-thumb"><div class="dev-thumb-empty">' + esc(shortType(device.device_type)) + '</div></div>';
    html += '<div class="dev-card"><div class="dev-head">' + thumb
      + '<div class="d-info"><div class="d-title"><div class="d-dot ' + dot + '"></div><div class="d-name">' + esc(device.name) + '</div></div>'
      + '<div class="d-type">' + esc(device.brand) + ' · ' + esc(device.device_type) + '</div>'
      + '<div class="d-ip">' + esc(device.ip) + '</div>'
      + '<div class="dev-actions">'
      + (device.stream_url ? '<button class="mini-btn js-open-viewer" data-stream="' + esc(device.stream_url)
          + '" data-title="' + esc(device.name) + '">View</button>' : '')
      + '<button class="mini-btn js-remove-device" data-device-id="' + esc(device.id) + '">Remove</button>'
      + '</div></div></div></div>';
  });
  el.innerHTML = html;
  bindDeviceActions();
}

function openAddDev() {
  $('dev-overlay').classList.remove('hidden');
  scanDevices();
}

function closeAddDev() {
  $('dev-overlay').classList.add('hidden');
}

async function scanDevices() {
  $('scan-loading').style.display = 'block';
  $('scan-results').innerHTML = '';
  var results = await get('/api/devices/scan') || [];
  lastScan = results;
  $('scan-loading').style.display = 'none';
  if (!results.length) {
    $('scan-results').innerHTML = '<div class="scan-empty">No devices found yet. Try again in a moment.</div>';
    return;
  }
  $('scan-results').innerHTML = results.map(function(device, index) {
    var action = device.saved
      ? '<button class="mini-btn" disabled>Connected</button>'
      : '<button class="mini-btn js-connect-device" data-index="' + index + '">Connect</button>';
    return '<div class="scan-card"><div class="scan-main">'
      + '<div class="scan-name">' + esc(device.hostname || device.ip) + '</div>'
      + '<div class="scan-meta">' + esc(device.brand) + ' · ' + esc(device.device_type) + ' · ' + esc(device.ip) + '</div>'
      + '</div>' + action + '</div>';
  }).join('');
  bindScanActions();
}

async function connectScannedDevice(index) {
  var device = lastScan[index];
  if (!device) return;
  await post('/api/devices/add', device);
  await refreshDevices();
  await scanDevices();
}

async function removeDevice(id) {
  await post('/api/devices/remove', {id: id});
  refreshDevices();
}

function bindDeviceActions() {
  document.querySelectorAll('#dev-scroll .js-open-viewer').forEach(function(el) {
    el.addEventListener('click', function() {
      openViewer(el.dataset.stream, el.dataset.title);
    });
  });
  document.querySelectorAll('#dev-scroll .js-remove-device').forEach(function(el) {
    el.addEventListener('click', function() {
      removeDevice(el.dataset.deviceId);
    });
  });
}

function bindScanActions() {
  document.querySelectorAll('#scan-results .js-connect-device').forEach(function(el) {
    el.addEventListener('click', function() {
      connectScannedDevice(Number(el.dataset.index));
    });
  });
}

function openViewer(url, title) {
  $('viewer-title').textContent = title || 'Live view';
  $('viewer-image').src = url + '?t=' + Date.now();
  $('viewer-overlay').classList.remove('hidden');
}

function closeViewer() {
  $('viewer-overlay').classList.add('hidden');
  $('viewer-image').src = '';
}

// ── 5. Activity feed ──────────────────────────────────────────────────────────
async function refreshActivity() {
  var items = await get('/api/queue') || [];
  var el = $('act-scroll');
  if (!items.length) {
    el.innerHTML = '<div style="padding:14px;color:var(--dim);font-size:11px;text-align:center">Waiting for activity…</div>';
    return;
  }
  el.innerHTML = items.slice(0, 10).map(function(item) {
    var status = item.status || 'QUEUED';
    var color = status === 'SUCCEEDED' ? 'green'
      : status === 'FAILED' ? 'red'
      : status === 'IN_PROGRESS' ? 'yellow'
      : 'gray';
    var label = item.command_text || ('queue/' + String(item.id || '').slice(0, 8));
    var timestamp = item.updated_at || item.created_at || item.started_at || item.finished_at;
    return '<div class="ev c-' + color + '">'
      + '<div class="ev-lbl">' + esc(label) + '</div>'
      + '<div class="ev-meta"><span>' + esc(statusLabel(status)) + '</span><span>' + esc(stamp(timestamp)) + '</span></div>'
      + '</div>';
  }).join('');
}

// ── 1. Chat bubbles ───────────────────────────────────────────────────────────
function addMsg(role, text) {
  var feed = $('chat-feed');
  var now  = new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
  var isG  = (role === 'gismo');
  var div  = document.createElement('div');
  div.className = 'msg msg-' + role;
  div.innerHTML =
    '<div class="msg-av ' + (isG ? 'av-g' : 'av-u') + '">' + (isG ? 'AI' : 'ME') + '</div>'
    + '<div class="msg-body">'
    + '<div class="bubble ' + (isG ? 'bbl-g' : 'bbl-u') + '">' + esc(text) + '</div>'
    + '<div class="msg-ts">' + now + '</div>'
    + '</div>';
  feed.appendChild(div);
  feed.scrollTop = feed.scrollHeight;
  return div;
}

function addTyping() {
  var feed = $('chat-feed');
  var div  = document.createElement('div');
  div.className = 'msg msg-gismo';
  div.id = 'typing-msg';
  div.innerHTML = '<div class="msg-av av-g">AI</div>'
    + '<div class="msg-body"><div class="bubble bbl-g">...</div></div>';
  feed.appendChild(div);
  feed.scrollTop = feed.scrollHeight;
}

function removeTyping() {
  var t = document.getElementById('typing-msg');
  if (t) t.remove();
}

function onKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
}

function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 100) + 'px';
}

// ── 1. Chat ─────────────────────────────────────────────────────────────────
async function sendChat() {
  var input = $('chat-input');
  var msg = input.value.trim();
  if (!msg) return;

  var historyToSend = chatHistory.slice(-12);
  input.value = '';
  input.style.height = 'auto';
  addMsg('user', msg);
  chatHistory.push({role: 'user', content: msg});

  var sendBtn = $('send-btn');
  sendBtn.disabled = true;
  addTyping();

  try {
    var res = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: msg, history: historyToSend})
    });
    var data = await res.json();
    removeTyping();

    if (!res.ok) {
      addMsg('gismo', 'GISMO: ' + (data.error || 'Request failed.'));
    } else {
      addMsg('gismo', data.reply || '');
      chatHistory.push({role: 'assistant', content: data.reply || ''});
      speakText(data.reply || '');
      refreshActivity();
    }
  } catch (err) {
    removeTyping();
    addMsg('gismo', 'Could not reach the server. Check that GISMO is running.');
    console.error('sendChat error:', err);
  }

  sendBtn.disabled = false;
  input.focus();
}

// ── 8. TTS — speak any GISMO text via /api/tts/speak ─────────────────────────
async function speakText(text) {
  if (!ttsEnabled || !text) return;
  try {
    // stop any currently playing audio
    if (currentAudio) { currentAudio.pause(); currentAudio = null; }
    var res = await fetch('/api/tts/speak', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({text: text})
    });
    if (!res.ok) return;
    var blob = await res.blob();
    var url  = URL.createObjectURL(blob);
    var audio = new Audio(url);
    currentAudio = audio;
    audio.onended = function() { URL.revokeObjectURL(url); currentAudio = null; };
    audio.play().catch(function(e) { console.warn('TTS play blocked:', e.message); });
  } catch (e) {
    // TTS not available — silent fail
  }
}

// ── 8. Mic — Web Speech API, auto-sends on result ────────────────────────────
function toggleMic() {
  var btn = $('mic-btn');
  var SR  = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) {
    addMsg('gismo', 'Speech recognition is not available in this browser. Try Chrome.');
    return;
  }
  if (micActive) {
    micRec && micRec.stop();
    micActive = false;
    btn.classList.remove('mic-on');
    return;
  }
  // stop any TTS while user speaks
  if (currentAudio) { currentAudio.pause(); currentAudio = null; }

  micRec = new SR();
  micRec.continuous      = false;
  micRec.interimResults  = false;
  micRec.lang            = 'en-US';

  micRec.onresult = function(e) {
    var transcript = e.results[0][0].transcript;
    $('chat-input').value = transcript;
    micActive = false;
    btn.classList.remove('mic-on');
    sendChat();           // auto-send after voice input
  };
  micRec.onerror = function(e) {
    console.warn('Speech recognition error:', e.error);
    micActive = false;
    btn.classList.remove('mic-on');
  };
  micRec.onend = function() {
    micActive = false;
    btn.classList.remove('mic-on');
  };
  micRec.start();
  micActive = true;
  btn.classList.add('mic-on');
}

// ── Daemon control ────────────────────────────────────────────────────────────
async function togglePause() {
  await post('/api/daemon/' + (daemonPaused ? 'resume' : 'pause'), {});
  refreshStatus();
}

// ── 7. Onboarding — in-UI wizard ──────────────────────────────────────────────
function showOb() {
  $('ob-overlay').classList.remove('hidden');
  $('ob-name').focus();
  loadObVoices();
}

async function loadObVoices() {
  var d = await get('/api/tts/voices');
  if (!d) return;
  $('ob-voices').innerHTML = d.voices.map(function(v) {
    var sel = v.is_selected ? ' sel' : '';
    return '<div class="vi' + sel + '" data-voice-id="' + esc(v.id) + '">'
      + '<div class="vi-name">' + esc(v.name) + '</div>'
      + '<div class="vi-desc">' + esc(v.description) + ' \xb7 ' + esc(v.lang) + ' \xb7 ' + esc(v.quality) + '</div>'
      + '</div>';
  }).join('');
  document.querySelectorAll('#ob-voices .vi').forEach(function(el) {
    el.addEventListener('click', function() {
      selVoice(el.dataset.voiceId, el);
    });
  });
  // pre-select first voice
  var first = d.voices[0];
  if (first) {
    obVoice = first.id;
    var fe = $('ob-voices').firstElementChild;
    if (fe) fe.classList.add('sel');
  }
  $('ob-done-btn').disabled = !obVoice;
}

function selVoice(id, el) {
  obVoice = id;
  document.querySelectorAll('.vi').forEach(function(e) { e.classList.remove('sel'); });
  el.classList.add('sel');
  $('ob-done-btn').disabled = false;
}

function obNext() {
  var name = $('ob-name').value.trim();
  if (!name) { $('ob-name').focus(); return; }
  $('ob-s1').classList.remove('active');
  $('ob-s2').classList.add('active');
}

async function obFinish() {
  var name = $('ob-name').value.trim();
  if (!name || !obVoice) return;
  var btn = $('ob-done-btn');
  btn.disabled    = true;
  btn.textContent = 'Setting up\u2026';
  var res = await post('/api/onboarding/complete', {name: name, voice_id: obVoice});
  if (res && res.ok) {
    $('ob-overlay').classList.add('hidden');
    setOp(name);
    briefingDone = false;   // allow briefing now that name is set
    maybeBriefing();
    refreshDevices();
  } else {
    btn.disabled    = false;
    btn.textContent = 'Get Started';
    addMsg('gismo', 'Setup failed. Please try again.');
  }
}

// ── 3. Operator name ──────────────────────────────────────────────────────────
function setOp(name) {
  $('op-name').textContent = name || '\u2014';
}

async function openSettings() {
  var data = await get('/api/settings');
  if (!data) return;
  $('settings-name').value = data.operator_name || '';
  $('settings-voice').innerHTML = (data.voices || []).map(function(voice) {
    var selected = voice.id === data.voice ? ' selected' : '';
    return '<option value="' + esc(voice.id) + '"' + selected + '>' + esc(voice.name) + ' · ' + esc(voice.lang) + '</option>';
  }).join('');
  $('settings-overlay').classList.remove('hidden');
}

function closeSettings() {
  $('settings-overlay').classList.add('hidden');
}

async function previewSettingsVoice() {
  var voiceId = $('settings-voice').value;
  try {
    var res = await fetch('/api/tts/speak', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text: 'Hello. This is how I sound.', voice: voiceId})
    });
    if (!res.ok) return;
    var blob = await res.blob();
    var url = URL.createObjectURL(blob);
    var audio = new Audio(url);
    audio.onended = function() { URL.revokeObjectURL(url); };
    audio.play();
  } catch (e) {
  }
}

async function saveSettings() {
  var data = await post('/api/settings', {
    operator_name: $('settings-name').value.trim(),
    voice_id: $('settings-voice').value
  });
  if (!data) return;
  setOp(data.operator_name);
  closeSettings();
}

// ── Search (placeholder) ──────────────────────────────────────────────────────
function onSearch(q) {
  // Future: filter activity feed rows or focus chat
}

// ── Utilities ─────────────────────────────────────────────────────────────────
function $(id) { return document.getElementById(id); }

function esc(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function shortType(kind) {
  if (!kind) return 'DEVICE';
  if (kind.indexOf('camera') >= 0) return 'CAM';
  if (kind.indexOf('light') >= 0) return 'LIGHT';
  if (kind.indexOf('hub') >= 0) return 'HUB';
  return String(kind).toUpperCase();
}

function age(iso) {
  if (!iso) return '';
  var s = Math.max(0, Date.now() - new Date(iso)) / 1000;
  if (s < 60)    return Math.round(s) + 's';
  if (s < 3600)  return Math.round(s / 60) + 'm';
  if (s < 86400) return Math.round(s / 3600) + 'h';
  return Math.round(s / 86400) + 'd';
}

function stamp(iso) {
  if (!iso) return '--';
  var dt = new Date(iso);
  if (isNaN(dt.getTime())) return '--';
  return dt.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
}

function statusLabel(status) {
  if (status === 'IN_PROGRESS') return 'running';
  if (status === 'SUCCEEDED') return 'succeeded';
  if (status === 'FAILED') return 'failed';
  if (status === 'QUEUED') return 'queued';
  return String(status || '').toLowerCase();
}

async function get(path) {
  try {
    var r = await fetch(path);
    return await r.json();
  } catch (e) {
    return null;
  }
}

async function post(path, body) {
  try {
    var r = await fetch(path, {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify(body)
    });
    return await r.json();
  } catch (e) {
    return null;
  }
}

// ── Start ─────────────────────────────────────────────────────────────────────
init();
</script>
</body>
</html>
"""
