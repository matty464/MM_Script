"""Lightweight web dashboard for monitoring the MM bot.

Uses only the Python stdlib (http.server). Serves:
  GET /                -> single-page HTML UI
  GET /api/snapshot    -> JSON of current strategy + state + executor
  POST /api/start_quoting -> arm bid/ask placement (when manual_quoting_start in config)

Run in a daemon thread alongside the strategy. There is no auth — bind to
127.0.0.1 only unless you really know what you are doing.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

log = logging.getLogger("dashboard")


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>hl-mm dashboard</title>
<style>
  :root {
    --bg: #0b0e14;
    --panel: #131820;
    --panel2: #1a2030;
    --border: #232b3a;
    --text: #e6e9ef;
    --muted: #8a93a6;
    --green: #4ade80;
    --red: #f87171;
    --blue: #60a5fa;
    --yellow: #fbbf24;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  header {
    padding: 14px 22px;
    background: var(--panel);
    border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 18px; flex-wrap: wrap;
  }
  header h1 { font-size: 16px; margin: 0; font-weight: 600; }
  .pill {
    padding: 3px 10px; border-radius: 999px;
    background: var(--panel2); border: 1px solid var(--border);
    font-size: 12px; color: var(--muted);
  }
  .pill.green { color: var(--green); border-color: #1e3a2a; background: #0e1b14; }
  .pill.red { color: var(--red); border-color: #3a1e1e; background: #1b0e0e; }
  .pill.yellow { color: var(--yellow); border-color: #3a311e; background: #1b1709; }
  main { padding: 18px 22px; max-width: 1500px; }
  .grid {
    display: grid;
    grid-template-columns: repeat(12, 1fr);
    gap: 14px;
  }
  .card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 14px 16px;
  }
  .card h2 {
    font-size: 11px; text-transform: uppercase; letter-spacing: .08em;
    color: var(--muted); margin: 0 0 10px;
  }
  .kpi { font-size: 22px; font-weight: 600; line-height: 1.2; }
  .kpi.sub { font-size: 12px; font-weight: 400; color: var(--muted); margin-top: 2px; }
  .pos { color: var(--green); }
  .neg { color: var(--red); }
  .neutral { color: var(--text); }
  .span2 { grid-column: span 2; }
  .span3 { grid-column: span 3; }
  .span4 { grid-column: span 4; }
  .span6 { grid-column: span 6; }
  .span12 { grid-column: span 12; }
  table {
    width: 100%; border-collapse: collapse;
    font-size: 12px;
  }
  th {
    text-align: left; color: var(--muted);
    font-weight: 500; padding: 6px 8px;
    border-bottom: 1px solid var(--border);
    text-transform: uppercase; letter-spacing: .05em; font-size: 10px;
  }
  td {
    padding: 6px 8px; border-bottom: 1px solid #161b25;
  }
  tr:last-child td { border-bottom: none; }
  td.r, th.r { text-align: right; }
  .row {
    display: flex; gap: 14px; align-items: baseline;
  }
  .row .label { color: var(--muted); font-size: 11px; min-width: 90px; }
  .row .val { font-variant-numeric: tabular-nums; }
  .stack { display: flex; flex-direction: column; gap: 6px; }
  svg { display: block; width: 100%; height: 180px; }
  .chart-wrap { background: var(--panel2); border-radius: 6px; padding: 8px; }
  .legend { display: flex; gap: 14px; font-size: 11px; color: var(--muted); margin-bottom: 6px; }
  .legend .swatch { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; vertical-align: -1px; }
  .empty { color: var(--muted); font-style: italic; padding: 14px 0; text-align: center; }
  .flash { animation: flash .6s ease; }
  @keyframes flash { 0% { background: #2a3656; } 100% { background: transparent; } }
  footer { padding: 14px 22px; color: var(--muted); font-size: 11px; }
  a { color: var(--blue); }
</style>
</head>
<body>

<header>
  <h1>hl-mm dashboard</h1>
  <span id="status" class="pill">connecting…</span>
  <span id="network" class="pill"></span>
  <span id="mode" class="pill"></span>
  <span id="symbol" class="pill"></span>
  <span id="uptime" class="pill"></span>
  <span style="flex:1"></span>
  <button id="cancel-btn" onclick="cancelAll()" style="
    background:#3a1e1e; border:1px solid #7a2a2a; color:#f87171;
    padding:5px 14px; border-radius:6px; cursor:pointer; font:inherit;
    font-size:12px; font-weight:600; letter-spacing:.04em;
  ">⊗ Cancel All</button>
  <button onclick="apiAction('/api/flatten?side=buy','Flatten Long')" style="
    background:#1b170a; border:1px solid #5a4a1a; color:#fbbf24;
    padding:5px 14px; border-radius:6px; cursor:pointer; font:inherit;
    font-size:12px; font-weight:600; letter-spacing:.04em;
  ">▼ Flatten Long (b)</button>
  <button onclick="apiAction('/api/flatten?side=sell','Flatten Short')" style="
    background:#1b170a; border:1px solid #5a4a1a; color:#fbbf24;
    padding:5px 14px; border-radius:6px; cursor:pointer; font:inherit;
    font-size:12px; font-weight:600; letter-spacing:.04em;
  ">▲ Flatten Short (s)</button>
  <span id="start-quote-wrap" style="display:none">
    <button id="start-quote-btn" type="button" onclick="startLiveQuoting()" style="
      background:#0e1b14; border:1px solid #1e3a2a; color:#4ade80;
      padding:5px 14px; border-radius:6px; cursor:pointer; font:inherit;
      font-size:12px; font-weight:600; letter-spacing:.04em;
    ">▶ Start live quoting</button>
  </span>
  <span id="last-update" class="pill"></span>
</header>

<main>
  <div class="grid">

    <div class="card span2">
      <h2>Mid</h2>
      <div id="mid" class="kpi neutral">—</div>
      <div id="spread" class="kpi sub">—</div>
    </div>
    <div class="card span2">
      <h2>Sigma (bp/s)</h2>
      <div id="sigma" class="kpi neutral">—</div>
      <div class="kpi sub">realized vol estimate</div>
    </div>
    <div class="card span2">
      <h2>Position</h2>
      <div id="pos-size" class="kpi neutral">—</div>
      <div id="pos-notional" class="kpi sub">—</div>
    </div>
    <div class="card span2">
      <h2>Realized PnL</h2>
      <div id="pnl-realized" class="kpi neutral">—</div>
      <div id="fills-count" class="kpi sub">0 fills</div>
    </div>
    <div class="card span2">
      <h2>Unrealized PnL</h2>
      <div id="pnl-unrealized" class="kpi neutral">—</div>
      <div id="avg-entry" class="kpi sub">—</div>
    </div>
    <div class="card span2">
      <h2>Total PnL</h2>
      <div id="pnl-total" class="kpi neutral">—</div>
      <div id="loss-budget" class="kpi sub">—</div>
    </div>

    <div class="card span12">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;flex-wrap:wrap;gap:8px">
        <h2 style="margin:0">Price Chart</h2>
        <div style="display:flex;gap:10px;align-items:center;font-size:12px">
          <span style="color:var(--muted)">candle size:</span>
          <select id="candle-size" style="background:var(--panel2);border:1px solid var(--border);color:var(--text);padding:3px 8px;border-radius:4px;font:inherit;font-size:12px;cursor:pointer">
            <option value="5">5 s</option>
            <option value="10" selected>10 s</option>
            <option value="30">30 s</option>
            <option value="60">1 min</option>
          </select>
          <span class="legend"><span class="swatch" style="background:#4ade80;display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:3px;vertical-align:-1px"></span>up</span>
          <span class="legend"><span class="swatch" style="background:#f87171;display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:3px;vertical-align:-1px"></span>down</span>
          <span class="legend"><span class="swatch" style="background:#fbbf24;display:inline-block;width:10px;height:3px;vertical-align:4px;margin-right:3px"></span>bid/ask</span>
          <span class="legend"><span style="color:#a78bfa;margin-right:3px">—</span>PnL</span>
        </div>
      </div>
      <canvas id="price-chart" style="width:100%;display:block;border-radius:6px;background:var(--panel2)"></canvas>
    </div>

    <div class="card span6">
      <h2>Current Quote</h2>
      <div class="stack">
        <div class="row"><span class="label">fair px</span><span id="q-fair" class="val">—</span></div>
        <div class="row"><span class="label">ML signal</span><span id="q-signal" class="val">—</span></div>
        <div class="row"><span class="label">adj fair px</span><span id="q-adj-fair" class="val">—</span></div>
        <div class="row"><span class="label">half-spread</span><span id="q-hs" class="val">—</span></div>
        <div class="row"><span class="label">skew</span><span id="q-skew" class="val">—</span></div>
        <div class="row"><span class="label">target bid</span><span id="q-bid" class="val">—</span></div>
        <div class="row"><span class="label">target ask</span><span id="q-ask" class="val">—</span></div>
        <div class="row"><span class="label">book bid</span><span id="b-bid" class="val">—</span></div>
        <div class="row"><span class="label">book ask</span><span id="b-ask" class="val">—</span></div>
      </div>
    </div>

    <div class="card span12">
      <h2>Order Book <span style="color:var(--muted);font-weight:400;font-size:11px">— your orders shown with glow · depth bar = cumulative size</span></h2>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:0 24px">
        <!-- ASKS (top half) -->
        <div>
          <div style="font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:4px;display:grid;grid-template-columns:1fr 80px 80px;gap:0 8px">
            <span>price</span><span style="text-align:right">size</span><span style="text-align:right">cumul</span>
          </div>
          <div id="ob-asks" style="display:flex;flex-direction:column-reverse"></div>
        </div>
        <!-- BIDS (bottom half) -->
        <div>
          <div style="font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:4px;display:grid;grid-template-columns:1fr 80px 80px;gap:0 8px">
            <span>price</span><span style="text-align:right">size</span><span style="text-align:right">cumul</span>
          </div>
          <div id="ob-bids"></div>
        </div>
      </div>
      <!-- Spread bar -->
      <div id="ob-spread" style="text-align:center;padding:6px 0;font-size:12px;color:var(--muted);border-top:1px solid var(--border);border-bottom:1px solid var(--border);margin:4px 0"></div>
      <!-- My open orders summary -->
      <div style="margin-top:10px">
        <div style="font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin-bottom:6px">My Open Orders</div>
        <table style="width:100%;border-collapse:collapse;font-size:12px">
          <thead><tr>
            <th style="text-align:left;color:var(--muted);font-weight:500;padding:5px 8px;border-bottom:1px solid var(--border);font-size:10px;text-transform:uppercase">side</th>
            <th style="text-align:right;color:var(--muted);font-weight:500;padding:5px 8px;border-bottom:1px solid var(--border);font-size:10px;text-transform:uppercase">price</th>
            <th style="text-align:right;color:var(--muted);font-weight:500;padding:5px 8px;border-bottom:1px solid var(--border);font-size:10px;text-transform:uppercase">size</th>
            <th style="text-align:right;color:var(--muted);font-weight:500;padding:5px 8px;border-bottom:1px solid var(--border);font-size:10px;text-transform:uppercase">notional</th>
            <th style="text-align:left;color:var(--muted);font-weight:500;padding:5px 8px;border-bottom:1px solid var(--border);font-size:10px;text-transform:uppercase">id</th>
          </tr></thead>
          <tbody id="orders-tbody"></tbody>
        </table>
        <div id="orders-empty" class="empty">no open orders</div>
      </div>
    </div>

    <div class="card span6">
      <h2>Recent Fills</h2>
      <table>
        <thead><tr>
          <th>time</th><th>side</th><th class="r">price</th>
          <th class="r">size</th><th class="r">realized</th><th class="r">pos after</th>
        </tr></thead>
        <tbody id="fills-tbody"></tbody>
      </table>
      <div id="fills-empty" class="empty">no fills yet</div>
    </div>

    <div class="card span6">
      <h2>ML Signal — RLS Fair-Value Predictor</h2>
      <div class="stack" id="ml-stack">
        <div class="row"><span class="label">status</span><span id="ml-status" class="val">warming up…</span></div>
        <div class="row"><span class="label">signal</span><span id="ml-signal" class="val">—</span></div>
        <div class="row"><span class="label">adj fair px</span><span id="ml-adj-fair" class="val">—</span></div>
        <div class="row"><span class="label">updates</span><span id="ml-updates" class="val">—</span></div>
        <div class="row"><span class="label">MAE</span><span id="ml-mae" class="val">—</span></div>
        <div class="row"><span class="label">correlation</span><span id="ml-corr" class="val">—</span></div>
      </div>
      <div style="margin-top:10px">
        <div style="font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin-bottom:6px">Learned weights</div>
        <div id="ml-weights" style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px;font-size:12px"></div>
      </div>
    </div>

    <div class="card span12">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;flex-wrap:wrap;gap:8px">
        <h2 style="margin:0">ML Adaptation — How the model is learning</h2>
        <div id="ml-adapt-summary" style="font-size:12px;color:var(--muted)">—</div>
      </div>
      <div style="display:grid;grid-template-columns: 2fr 2fr 1.2fr;gap:12px">
        <div>
          <div style="font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin-bottom:4px">Feature weights over time</div>
          <canvas id="ml-weights-chart" style="width:100%;display:block;border-radius:6px;background:var(--panel2)"></canvas>
          <div id="ml-weights-legend" style="display:flex;flex-wrap:wrap;gap:6px 12px;margin-top:6px;font-size:11px"></div>
        </div>
        <div>
          <div style="font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin-bottom:4px">Accuracy over time (lower MAE / higher |corr| = better)</div>
          <canvas id="ml-accuracy-chart" style="width:100%;display:block;border-radius:6px;background:var(--panel2)"></canvas>
          <div style="display:flex;gap:14px;margin-top:6px;font-size:11px">
            <span><span style="display:inline-block;width:10px;height:3px;background:#fbbf24;vertical-align:3px;margin-right:3px"></span>MAE (left, bps)</span>
            <span><span style="display:inline-block;width:10px;height:3px;background:#a78bfa;vertical-align:3px;margin-right:3px"></span>correlation (right)</span>
          </div>
        </div>
        <div>
          <div style="font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin-bottom:4px">Predicted vs actual (last 100)</div>
          <canvas id="ml-scatter-chart" style="width:100%;display:block;border-radius:6px;background:var(--panel2)"></canvas>
          <div style="font-size:11px;color:var(--muted);margin-top:6px">Dots near the diagonal = good predictions.</div>
        </div>
      </div>
    </div>

    <div class="card span12">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;flex-wrap:wrap;gap:10px">
        <h2 style="margin:0">Adaptive Inventory Skew — bandit-learned multiplier</h2>
        <div id="skew-summary" style="font-size:12px;color:var(--muted)">—</div>
      </div>
      <div style="display:grid;grid-template-columns: 1.4fr 2.6fr;gap:14px">
        <div class="stack" id="skew-stats">
          <div class="row"><span class="label">status</span><span id="skew-status" class="val">—</span></div>
          <div class="row"><span class="label">base skew</span><span id="skew-base" class="val">—</span></div>
          <div class="row"><span class="label">active multiplier</span><span id="skew-mult" class="val">—</span></div>
          <div class="row"><span class="label">effective skew</span><span id="skew-eff" class="val">—</span></div>
          <div class="row"><span class="label">best so far</span><span id="skew-best" class="val">—</span></div>
          <div class="row"><span class="label">closing fills evaluated</span><span id="skew-obs" class="val">—</span></div>
          <div class="row"><span class="label">epsilon (explore)</span><span id="skew-eps" class="val">—</span></div>
        </div>
        <div>
          <div style="font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin-bottom:6px">Per-arm performance — green = active, blue = best so far</div>
          <table id="skew-arms-table">
            <thead><tr>
              <th class="r">multiplier</th>
              <th class="r">effective bps</th>
              <th class="r">pulls</th>
              <th class="r">mean edge (bps)</th>
              <th class="r">last edge</th>
              <th class="r">cum realized</th>
              <th class="r">cum notional</th>
            </tr></thead>
            <tbody id="skew-arms-tbody"></tbody>
          </table>
          <div id="skew-empty" class="empty" style="display:none">adaptive skew disabled — using static cfg.inventory_skew_bps</div>
          <div style="font-size:11px;color:var(--muted);margin-top:8px;line-height:1.5">
            Each arm = a multiplier of <code>inventory_skew_bps</code>. The bot picks one arm at a time;
            after every closing fill its per-fill edge (bps) is fed into that arm's reward EWMA.
            After <code>min_pulls_per_switch</code> observations the bandit either explores a random
            neighbor (with prob ε) or locks onto the highest-mean arm. Higher mean = better trade-off
            of fill volume vs adverse selection. Persisted to <code>state/skew_adapter.json</code>.
          </div>
        </div>
      </div>
    </div>

    <div class="card span6">
      <h2>Configuration</h2>
      <div id="config-grid" style="display:grid; grid-template-columns: repeat(2, 1fr); gap: 8px 18px;"></div>
    </div>

    <div class="card span12">
      <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:10px">
        <h2 style="margin:0">Live Tuning — change parameters without restarting</h2>
        <div style="display:flex;gap:8px;align-items:center">
          <span id="tune-status" style="font-size:11px;color:var(--muted)">edit a field, then click Apply</span>
          <button id="tune-revert" onclick="revertTuning()" style="
            background:var(--panel2); border:1px solid var(--border); color:var(--muted);
            padding:5px 12px; border-radius:6px; cursor:pointer; font:inherit; font-size:12px;
          ">Revert</button>
          <button id="tune-apply" onclick="applyTuning()" style="
            background:#0e1b14; border:1px solid #1e3a2a; color:#4ade80;
            padding:5px 14px; border-radius:6px; cursor:pointer; font:inherit;
            font-size:12px; font-weight:600; letter-spacing:.04em;
          ">Apply Changes</button>
        </div>
      </div>
      <div id="tune-grid" style="display:grid; grid-template-columns: repeat(4, 1fr); gap: 14px 18px"></div>
      <div style="font-size:11px;color:var(--muted);margin-top:10px;line-height:1.5">
        Edited fields highlight in <span style="color:var(--yellow)">yellow</span>.
        Changes are <strong>not persisted</strong> to <code>config.yaml</code> — they only affect this run.
        Network / mode / symbol / dashboard cannot be changed live.
      </div>
    </div>

  </div>
</main>

<footer>
  Polls /api/snapshot every 1s · trade at your own risk
</footer>

<script>
const fmt = {
  px: v => v == null ? '—' : Number(v).toLocaleString(undefined, {maximumFractionDigits: 6}),
  sz: v => v == null ? '—' : Number(v).toLocaleString(undefined, {maximumFractionDigits: 6}),
  usd: v => v == null ? '—' : '$' + Number(v).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2}),
  bp: v => v == null ? '—' : Number(v).toFixed(2) + ' bp',
  pct: v => v == null ? '—' : (Number(v) * 100).toFixed(3) + '%',
  age: s => {
    if (s == null) return '—';
    s = Math.floor(s);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return `${h}h ${String(m).padStart(2,'0')}m ${String(sec).padStart(2,'0')}s`;
  },
  time: ts => ts ? new Date(ts * 1000).toLocaleTimeString() : '—',
};

function pnlClass(v) {
  if (v == null) return 'neutral';
  if (v > 1e-9) return 'pos';
  if (v < -1e-9) return 'neg';
  return 'neutral';
}

function setText(id, text, klass) {
  const el = document.getElementById(id);
  if (!el) return;
  if (el.textContent !== text) {
    el.textContent = text;
    el.classList.add('flash');
    setTimeout(() => el.classList.remove('flash'), 600);
  }
  if (klass) {
    el.classList.remove('pos', 'neg', 'neutral');
    el.classList.add(klass);
  }
}

function statusPill(snap) {
  const el = document.getElementById('status');
  el.classList.remove('green', 'red', 'yellow');
  const status = snap.status || '';
  el.textContent = status;
  if (status.startsWith('RUNNING')) el.classList.add('green');
  else if (status.startsWith('HALTED')) el.classList.add('red');
  else if (status.startsWith('PAUSED')) el.classList.add('yellow');
}

// ─── Price Chart (Canvas) ────────────────────────────────────────────────────
let _lastSnap = null;   // held so the candle-size selector can redraw instantly

document.addEventListener('DOMContentLoaded', () => {
  const sel = document.getElementById('candle-size');
  if (sel) sel.addEventListener('change', () => { if (_lastSnap) renderChart(_lastSnap.ticks, _lastSnap.open_orders, _lastSnap.book); });
});

function buildCandles(ticks, intervalS) {
  if (!ticks || ticks.length < 2) return [];
  const candles = [];
  let cur = null;
  for (const t of ticks) {
    const bucket = Math.floor(t.ts / intervalS) * intervalS;
    if (!cur || cur.t !== bucket) {
      if (cur) candles.push(cur);
      cur = { t: bucket, o: t.mid, h: t.mid, l: t.mid, c: t.mid, pnl: t.realized + t.unrealized };
    } else {
      cur.h = Math.max(cur.h, t.mid);
      cur.l = Math.min(cur.l, t.mid);
      cur.c = t.mid;
      cur.pnl = t.realized + t.unrealized;
    }
  }
  if (cur) candles.push(cur);
  return candles;
}

function renderChart(ticks, openOrders, book) {
  const canvas = document.getElementById('price-chart');
  if (!canvas) return;

  // DPI-aware sizing
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.parentElement.getBoundingClientRect();
  const CW = Math.floor(rect.width) || 900;
  const CH = 320;
  canvas.width  = CW * dpr;
  canvas.height = CH * dpr;
  canvas.style.height = CH + 'px';
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);

  // Layout constants
  const PAD_L = 10, PAD_R = 72, PAD_T = 18, PAD_B = 28;
  const PNL_H = 60;   // height of PnL sub-panel
  const SPLIT = CH - PAD_B - PNL_H - 12; // y where price panel ends
  const priceH = SPLIT - PAD_T;
  const W = CW - PAD_L - PAD_R;

  ctx.clearRect(0, 0, CW, CH);

  // Background
  ctx.fillStyle = '#131820';
  ctx.fillRect(0, 0, CW, CH);

  const intervalS = parseInt(document.getElementById('candle-size')?.value || '10', 10);
  const candles = buildCandles(ticks, intervalS);

  if (candles.length < 2) {
    ctx.fillStyle = '#8a93a6';
    ctx.font = '13px ui-monospace,monospace';
    ctx.textAlign = 'center';
    ctx.fillText('waiting for data…', CW / 2, CH / 2);
    return;
  }

  // Price range (with padding)
  let pLo = Math.min(...candles.map(c => c.l));
  let pHi = Math.max(...candles.map(c => c.h));
  // Include open-order prices so they're always visible
  (openOrders || []).forEach(o => { pLo = Math.min(pLo, o.px); pHi = Math.max(pHi, o.px); });
  const pPad = (pHi - pLo) * 0.12 || pHi * 0.002 || 1;
  pLo -= pPad; pHi += pPad;
  const pRange = pHi - pLo;

  // PnL range
  const pnlVals = candles.map(c => c.pnl);
  let nLo = Math.min(...pnlVals, 0), nHi = Math.max(...pnlVals, 0);
  const nPad = (nHi - nLo) * 0.15 || 0.5;
  nLo -= nPad; nHi += nPad;
  const nRange = nHi - nLo || 1;

  // Scale helpers
  const xOf   = i => PAD_L + (i + 0.5) * (W / candles.length);
  const yPrice = p  => PAD_T + priceH * (1 - (p - pLo) / pRange);
  const yPnl   = v  => SPLIT + 12 + PNL_H * (1 - (v - nLo) / nRange);

  // ── Grid lines (price) ────────────────────────────────────────────────
  const nGridLines = 5;
  ctx.strokeStyle = '#1e2535';
  ctx.lineWidth = 1;
  for (let i = 0; i <= nGridLines; i++) {
    const p = pLo + (pRange / nGridLines) * i;
    const y = Math.round(yPrice(p)) + 0.5;
    ctx.beginPath(); ctx.moveTo(PAD_L, y); ctx.lineTo(PAD_L + W, y); ctx.stroke();
    // Price label
    ctx.fillStyle = '#5a6480';
    ctx.font = '10px ui-monospace,monospace';
    ctx.textAlign = 'left';
    ctx.fillText(p.toLocaleString(undefined, {maximumFractionDigits: 2}), PAD_L + W + 6, y + 3);
  }

  // ── PnL zero line ─────────────────────────────────────────────────────
  const yZero = Math.round(yPnl(0)) + 0.5;
  ctx.strokeStyle = '#2a3142';
  ctx.setLineDash([3, 5]);
  ctx.beginPath(); ctx.moveTo(PAD_L, yZero); ctx.lineTo(PAD_L + W, yZero); ctx.stroke();
  ctx.setLineDash([]);

  // ── Open-order horizontal lines ───────────────────────────────────────
  (openOrders || []).forEach(o => {
    const y = Math.round(yPrice(o.px)) + 0.5;
    const color = o.side === 'BUY' ? '#4ade80' : '#f87171';
    ctx.strokeStyle = color + 'aa';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(PAD_L, y); ctx.lineTo(PAD_L + W, y); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = color;
    ctx.font = 'bold 10px ui-monospace,monospace';
    ctx.textAlign = 'left';
    ctx.fillText(`${o.side === 'BUY' ? 'BID' : 'ASK'} ${o.px.toLocaleString(undefined,{maximumFractionDigits:2})}`, PAD_L + W + 6, y + 3);
  });

  // ── Current bid/ask lines (live book) ─────────────────────────────────
  if (book) {
    [[book.bid_px, '#4ade8055', 'BBO bid'], [book.ask_px, '#f8717155', 'BBO ask']].forEach(([px, col, _lbl]) => {
      if (!px) return;
      const y = Math.round(yPrice(px)) + 0.5;
      ctx.strokeStyle = col;
      ctx.lineWidth = 1;
      ctx.setLineDash([2, 6]);
      ctx.beginPath(); ctx.moveTo(PAD_L, y); ctx.lineTo(PAD_L + W, y); ctx.stroke();
      ctx.setLineDash([]);
    });
  }

  // ── Candles ───────────────────────────────────────────────────────────
  const cw = Math.max(Math.floor(W / candles.length) - 1, 1);
  candles.forEach((c, i) => {
    const x  = Math.round(xOf(i));
    const up = c.c >= c.o;
    const col = up ? '#4ade80' : '#f87171';
    const yO = yPrice(c.o), yC = yPrice(c.c), yH = yPrice(c.h), yL = yPrice(c.l);

    // Wick
    ctx.strokeStyle = col;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, yH); ctx.lineTo(x, yL);
    ctx.stroke();

    // Body
    const bodyTop = Math.min(yO, yC);
    const bodyH   = Math.max(Math.abs(yC - yO), 1);
    ctx.fillStyle = col;
    ctx.fillRect(Math.round(x - cw / 2), Math.round(bodyTop), cw, Math.round(bodyH));
  });

  // ── PnL area chart ─────────────────────────────────────────────────────
  const pnlGrad = ctx.createLinearGradient(0, SPLIT + 12, 0, SPLIT + 12 + PNL_H);
  pnlGrad.addColorStop(0, '#a78bfa55');
  pnlGrad.addColorStop(1, '#a78bfa00');
  ctx.fillStyle = pnlGrad;
  ctx.beginPath();
  ctx.moveTo(Math.round(xOf(0)), yZero);
  candles.forEach((c, i) => ctx.lineTo(Math.round(xOf(i)), yPnl(c.pnl)));
  ctx.lineTo(Math.round(xOf(candles.length - 1)), yZero);
  ctx.closePath();
  ctx.fill();
  ctx.strokeStyle = '#a78bfa';
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  candles.forEach((c, i) => i === 0 ? ctx.moveTo(Math.round(xOf(i)), yPnl(c.pnl)) : ctx.lineTo(Math.round(xOf(i)), yPnl(c.pnl)));
  ctx.stroke();

  // ── Time axis labels ──────────────────────────────────────────────────
  ctx.fillStyle = '#5a6480';
  ctx.font = '10px ui-monospace,monospace';
  ctx.textAlign = 'center';
  const step = Math.max(1, Math.floor(candles.length / 6));
  for (let i = 0; i < candles.length; i += step) {
    const x = Math.round(xOf(i));
    const d = new Date(candles[i].t * 1000);
    const label = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    ctx.fillText(label, x, CH - 8);
  }

  // ── Panel labels ──────────────────────────────────────────────────────
  ctx.fillStyle = '#5a6480';
  ctx.font = '10px ui-monospace,monospace';
  ctx.textAlign = 'left';
  ctx.fillText('PnL (USD)', PAD_L + 4, SPLIT + 24);

  // ── Crosshair: current price dot ──────────────────────────────────────
  if (book && book.mid) {
    const y = yPrice(book.mid);
    ctx.fillStyle = '#60a5fa';
    ctx.beginPath();
    ctx.arc(PAD_L + W, y, 4, 0, Math.PI * 2);
    ctx.fill();
  }
}

function renderConfig(cfg) {
  if (!cfg) return;
  const grid = document.getElementById('config-grid');
  grid.innerHTML = '';
  Object.entries(cfg).forEach(([k, v]) => {
    const wrap = document.createElement('div');
    wrap.style.display = 'flex'; wrap.style.justifyContent = 'space-between'; wrap.style.gap = '12px';
    const label = document.createElement('span'); label.style.color = 'var(--muted)'; label.textContent = k;
    const val = document.createElement('span'); val.textContent = String(v);
    wrap.appendChild(label); wrap.appendChild(val);
    grid.appendChild(wrap);
  });
}

// ─── Live Tuning panel ──────────────────────────────────────────────────────
// We track which inputs the user has manually edited so background polls
// don't overwrite them mid-edit, and so Apply only sends changed fields.
const _tuneEdited = new Set();
const _tuneLast = {};        // last server-known value, per field
let   _tuneBuilt = false;    // build form once, then only refresh values

function renderTuning(cfg, spec) {
  if (!cfg || !spec) return;
  const grid = document.getElementById('tune-grid');

  // First render: build the inputs (grouped, sorted by group ordering).
  if (!_tuneBuilt) {
    grid.innerHTML = '';
    const groupOrder = ['Sizing', 'Pricing', 'Loop', 'Risk', 'ML', 'Adaptive', 'Leverage'];
    const byGroup = {};
    spec.forEach(s => { (byGroup[s.group] = byGroup[s.group] || []).push(s); });

    groupOrder.forEach(group => {
      if (!byGroup[group]) return;
      const col = document.createElement('div');
      col.style.display = 'flex'; col.style.flexDirection = 'column'; col.style.gap = '6px';

      const h = document.createElement('div');
      h.textContent = group;
      h.style.fontSize = '10px'; h.style.textTransform = 'uppercase';
      h.style.letterSpacing = '.08em'; h.style.color = 'var(--muted)';
      h.style.borderBottom = '1px solid var(--border)';
      h.style.paddingBottom = '4px'; h.style.marginBottom = '2px';
      col.appendChild(h);

      byGroup[group].forEach(s => {
        const row = document.createElement('label');
        row.style.display = 'flex'; row.style.flexDirection = 'column'; row.style.gap = '2px';

        const lbl = document.createElement('span');
        lbl.textContent = s.label;
        lbl.style.color = 'var(--muted)'; lbl.style.fontSize = '11px';

        const inp = document.createElement('input');
        inp.id = 'tune-' + s.key;
        inp.dataset.key = s.key;
        inp.dataset.kind = s.type;
        if (s.type === 'bool') {
          inp.type = 'checkbox';
          inp.style.width = '18px';
          inp.style.height = '18px';
          inp.style.margin = '4px 0';
          inp.style.accentColor = '#4ade80';
        } else if (s.type === 'str') {
          inp.type = 'text';
        } else {
          inp.type = 'number';
          inp.step = (s.type === 'int') ? '1' : 'any';
        }
        if (s.type !== 'bool') {
          inp.style.background = 'var(--panel2)';
          inp.style.border = '1px solid var(--border)';
          inp.style.color = 'var(--text)';
          inp.style.padding = '5px 8px';
          inp.style.borderRadius = '4px';
          inp.style.font = 'inherit';
          inp.style.fontSize = '12px';
          inp.style.width = '100%';
        }
        const evt = (s.type === 'bool') ? 'change' : 'input';
        inp.addEventListener(evt, () => {
          _tuneEdited.add(s.key);
          if (s.type !== 'bool') {
            inp.style.borderColor = '#7a651e';
            inp.style.background = '#1b170a';
          }
          document.getElementById('tune-status').textContent =
            _tuneEdited.size + ' field(s) edited — click Apply';
          document.getElementById('tune-status').style.color = 'var(--yellow)';
        });

        row.appendChild(lbl); row.appendChild(inp);
        col.appendChild(row);
      });

      grid.appendChild(col);
    });
    _tuneBuilt = true;
  }

  // Refresh values for any input the user is NOT currently editing.
  spec.forEach(s => {
    const inp = document.getElementById('tune-' + s.key);
    if (!inp) return;
    _tuneLast[s.key] = cfg[s.key];
    if (!_tuneEdited.has(s.key)) {
      if (s.type === 'bool') {
        inp.checked = !!cfg[s.key];
      } else {
        inp.value = cfg[s.key] != null ? String(cfg[s.key]) : '';
      }
    }
  });
}

function _coerceTune(raw, kind) {
  if (kind === 'bool') return !!raw;   // raw is already a boolean from .checked
  if (kind === 'str') return raw;
  if (kind === 'int') return parseInt(raw, 10);
  return parseFloat(raw);
}

async function applyTuning() {
  if (_tuneEdited.size === 0) {
    document.getElementById('tune-status').textContent = 'no changes to apply';
    return;
  }
  const updates = {};
  _tuneEdited.forEach(key => {
    const inp = document.getElementById('tune-' + key);
    if (!inp) return;
    const raw = (inp.dataset.kind === 'bool') ? inp.checked : inp.value.trim();
    updates[key] = _coerceTune(raw, inp.dataset.kind);
  });

  // Sanity preview confirmation
  const lines = Object.entries(updates).map(([k, v]) =>
    `  ${k}: ${_tuneLast[k]} -> ${v}`).join('\\n');
  if (!confirm(`Apply these live changes?\n\n${lines}\n\nThis affects the running bot immediately.`)) {
    return;
  }

  const btn = document.getElementById('tune-apply');
  btn.disabled = true; btn.textContent = 'applying…';
  try {
    const r = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(updates),
    });
    const result = await r.json();
    const status = document.getElementById('tune-status');
    if (result.ok) {
      const n = Object.keys(result.updated || {}).length;
      status.textContent = `applied ${n} change(s) ✓`;
      status.style.color = 'var(--green)';
      _tuneEdited.clear();
      // Reset all input borders
      document.querySelectorAll('#tune-grid input').forEach(i => {
        i.style.borderColor = 'var(--border)';
        i.style.background = 'var(--panel2)';
      });
    } else {
      const errs = Object.entries(result.errors || {}).map(([k, v]) => `${k}: ${v}`).join('; ');
      status.textContent = `errors: ${errs}`;
      status.style.color = 'var(--red)';
    }
  } catch(e) {
    document.getElementById('tune-status').textContent = 'request failed: ' + e.message;
    document.getElementById('tune-status').style.color = 'var(--red)';
  } finally {
    btn.disabled = false; btn.textContent = 'Apply Changes';
  }
}

function revertTuning() {
  _tuneEdited.clear();
  Object.entries(_tuneLast).forEach(([key, v]) => {
    const inp = document.getElementById('tune-' + key);
    if (!inp) return;
    if (inp.dataset.kind === 'bool') {
      inp.checked = !!v;
    } else {
      inp.value = v != null ? String(v) : '';
      inp.style.borderColor = 'var(--border)';
      inp.style.background = 'var(--panel2)';
    }
  });
  const status = document.getElementById('tune-status');
  status.textContent = 'reverted to current values';
  status.style.color = 'var(--muted)';
}

function renderOrders(orders) {
  const tbody = document.getElementById('orders-tbody');
  const empty = document.getElementById('orders-empty');
  tbody.innerHTML = '';
  if (!orders || orders.length === 0) { empty.style.display = 'block'; return; }
  empty.style.display = 'none';
  orders.sort((a,b) => (a.side === b.side ? b.px - a.px : (a.side === 'BUY' ? -1 : 1)));
  for (const o of orders) {
    const tr = document.createElement('tr');
    const sideClass = o.side === 'BUY' ? 'pos' : 'neg';
    const cloid = o.cloid ? String(o.cloid).slice(0, 12) + '…' : '—';
    const oid = o.oid ? ` (oid ${o.oid})` : '';
    tr.innerHTML = `
      <td class="${sideClass}">${o.side}</td>
      <td class="r">${fmt.px(o.px)}</td>
      <td class="r">${fmt.sz(o.sz)}</td>
      <td class="r">${fmt.usd(o.px * o.sz)}</td>
      <td><code style="color:var(--muted)">${cloid}${oid}</code></td>
    `;
    tbody.appendChild(tr);
  }
}

function renderFills(fills) {
  const tbody = document.getElementById('fills-tbody');
  const empty = document.getElementById('fills-empty');
  tbody.innerHTML = '';
  if (!fills || fills.length === 0) { empty.style.display = 'block'; return; }
  empty.style.display = 'none';
  for (const f of fills.slice(-25).reverse()) {
    const tr = document.createElement('tr');
    const sideClass = f.side === 'BUY' ? 'pos' : 'neg';
    tr.innerHTML = `
      <td>${fmt.time(f.ts)}</td>
      <td class="${sideClass}">${f.side}</td>
      <td class="r">${fmt.px(f.px)}</td>
      <td class="r">${fmt.sz(f.sz)}</td>
      <td class="r ${pnlClass(f.realized_pnl)}">${fmt.usd(f.realized_pnl)}</td>
      <td class="r">${fmt.sz(f.position_after)}</td>
    `;
    tbody.appendChild(tr);
  }
}

async function tick() {
  try {
    const r = await fetch('/api/snapshot', { cache: 'no-store' });
    if (!r.ok) throw new Error('http ' + r.status);
    const snap = await r.json();

    statusPill(snap);
    const sqw = document.getElementById('start-quote-wrap');
    const sqb = document.getElementById('start-quote-btn');
    if (sqw && sqb) {
      const manual = snap.config && snap.config.manual_quoting_start;
      if (manual) {
        sqw.style.display = 'inline';
        if (!snap.quoting_armed) {
          sqb.disabled = false;
          sqb.textContent = '▶ Start live quoting';
          sqb.style.opacity = '1';
          sqb.style.cursor = 'pointer';
        } else {
          sqb.disabled = true;
          sqb.textContent = 'Live quoting on ✓';
          sqb.style.cursor = 'default';
          sqb.style.opacity = '0.9';
        }
      } else {
        sqw.style.display = 'none';
      }
    }
    document.getElementById('network').textContent = snap.config?.network || '';
    document.getElementById('mode').textContent = snap.config?.mode || '';
    document.getElementById('symbol').textContent = snap.config?.symbol || '';
    document.getElementById('uptime').textContent = 'up ' + fmt.age(snap.uptime_s);
    document.getElementById('last-update').textContent = 'updated ' + new Date().toLocaleTimeString();

    const book = snap.book || {};
    setText('mid', fmt.px(book.mid));
    setText('spread', book.spread_bps != null ? fmt.bp(book.spread_bps) + ' spread' : '—');
    if (snap.sigma_bps_per_sec != null) {
      let sigTxt = Number(snap.sigma_bps_per_sec).toFixed(2);
      const rawS = snap.sigma_raw_bps_per_sec;
      if (rawS != null && Math.abs(rawS - snap.sigma_bps_per_sec) > 0.08) {
        sigTxt += ' (raw ' + Number(rawS).toFixed(2) + ')';
      }
      setText('sigma', sigTxt);
    } else setText('sigma', '—');

    const pos = snap.position || {};
    const posClass = pos.size > 1e-9 ? 'pos' : pos.size < -1e-9 ? 'neg' : 'neutral';
    setText('pos-size', fmt.sz(pos.size), posClass);
    setText('pos-notional', `${fmt.usd(pos.notional)} @ avg ${fmt.px(pos.avg_entry_px)}`);
    setText('avg-entry', `avg entry ${fmt.px(pos.avg_entry_px)}`);

    const pnl = snap.pnl || {};
    setText('pnl-realized', fmt.usd(pnl.realized), pnlClass(pnl.realized));
    setText('pnl-unrealized', fmt.usd(pnl.unrealized), pnlClass(pnl.unrealized));
    setText('pnl-total', fmt.usd(pnl.total), pnlClass(pnl.total));
    setText('fills-count', `${snap.fills_count || 0} fills`);

    const lossBudget = snap.config?.max_session_loss_usd
      ? `kill at ${fmt.usd(-Math.abs(snap.config.max_session_loss_usd))}`
      : '';
    setText('loss-budget', lossBudget);

    const q = snap.quote;
    if (q) {
      setText('q-fair', fmt.px(q.fair_px));
      const sigBps = q.signal_bps;
      const sigQEl = document.getElementById('q-signal');
      if (sigQEl) {
        sigQEl.textContent = sigBps != null ? fmt.bp(sigBps) : '— (warming)';
        sigQEl.classList.remove('pos', 'neg', 'neutral');
        sigQEl.classList.add(pnlClass(sigBps));
      }
      setText('q-adj-fair', fmt.px(q.adjusted_fair_px));
      const bhs = q.bid_half_spread_bps, ahs = q.ask_half_spread_bps;
      if (bhs != null && ahs != null && Math.abs(bhs - ahs) > 0.02) {
        setText('q-hs', fmt.bp(bhs) + ' bid · ' + fmt.bp(ahs) + ' ask');
      } else {
        setText('q-hs', fmt.bp(q.half_spread_bps));
      }
      setText('q-skew', fmt.bp(q.skew_bps));
      setText('q-bid', `${fmt.px(q.bid_px)} × ${fmt.sz(q.bid_sz)}`);
      setText('q-ask', `${fmt.px(q.ask_px)} × ${fmt.sz(q.ask_sz)}`);
    }
    setText('b-bid', book.bid_px != null ? `${fmt.px(book.bid_px)} × ${fmt.sz(book.bid_sz)}` : '—');
    setText('b-ask', book.ask_px != null ? `${fmt.px(book.ask_px)} × ${fmt.sz(book.ask_sz)}` : '—');

    _lastSnap = snap;
    renderOrderBook(snap.depth, snap.open_orders, snap.book);
    renderOrders(snap.open_orders);
    renderFills(snap.fills);
    renderChart(snap.ticks, snap.open_orders, snap.book);
    renderConfig(snap.config);
    renderTuning(snap.config, snap.tunable_spec);
    renderML(snap.ml, snap.quote);
    renderMLAdaptation(snap.ml_history, snap.ml);
    renderSkewAdapter(snap.skew_adapter);

  } catch (e) {
    const el = document.getElementById('status');
    // Show the actual error so the user can diagnose — "connecting…" was
    // masking render exceptions from the prior bot version.
    el.textContent = e.name === 'TypeError' && e.message.includes('fetch')
      ? 'disconnected — bot not running on :8765'
      : 'error: ' + e.message;
    el.classList.remove('green', 'yellow'); el.classList.add('red');
    console.error('[dashboard tick]', e);
  }
}

function renderOrderBook(depth, myOrders, book) {
  const asksEl = document.getElementById('ob-asks');
  const bidsEl = document.getElementById('ob-bids');
  const spreadEl = document.getElementById('ob-spread');
  if (!asksEl || !bidsEl) return;

  if (!depth || (!depth.asks.length && !depth.bids.length)) {
    asksEl.innerHTML = bidsEl.innerHTML = '<div class="empty">no depth data</div>';
    return;
  }

  // Build sets of my order prices for highlight lookup
  const myBidPrices = new Set();
  const myAskPrices = new Set();
  (myOrders || []).forEach(o => {
    if (o.side === 'BUY') myBidPrices.add(o.px);
    else myAskPrices.add(o.px);
  });

  // Compute max cumulative size for bar scaling
  let bidCumul = 0, askCumul = 0;
  const bidsWithCumul = (depth.bids || []).map(l => { bidCumul += l.sz; return { ...l, cumul: bidCumul }; });
  const asksWithCumul = (depth.asks || []).map(l => { askCumul += l.sz; return { ...l, cumul: askCumul }; });
  const maxCumul = Math.max(bidCumul, askCumul, 1e-9);

  function makeRow(level, side, myPrices) {
    const isMine = [...myPrices].some(p => Math.abs(p - level.px) / level.px < 2e-5);
    const isGreen = side === 'bid';
    const barPct = (level.cumul / maxCumul * 100).toFixed(1);
    const barColor = isGreen ? 'rgba(74,222,128,0.10)' : 'rgba(248,113,113,0.10)';
    const textColor = isGreen ? 'var(--green)' : 'var(--red)';
    const glowStyle = isMine
      ? `box-shadow:inset 0 0 0 1px ${isGreen ? '#4ade80' : '#f87171'}, 0 0 6px ${isGreen ? '#4ade8055' : '#f8717155'};border-radius:3px;`
      : '';
    const myDot = isMine ? `<span style="color:${textColor};font-size:10px;margin-right:4px" title="my order">●</span>` : '';

    return `<div style="
      display:grid;grid-template-columns:1fr 80px 80px;gap:0 8px;
      padding:2px 6px;font-size:12px;position:relative;overflow:hidden;
      background:linear-gradient(${isGreen?'to left':'to right'},${barColor} ${barPct}%,transparent ${barPct}%);
      ${glowStyle}
      font-variant-numeric:tabular-nums;
    ">
      <span style="color:${textColor}">${myDot}${fmt.px(level.px)}</span>
      <span style="text-align:right;color:var(--text)">${fmt.sz(level.sz)}</span>
      <span style="text-align:right;color:var(--muted)">${fmt.sz(level.cumul)}</span>
    </div>`;
  }

  // Asks: show reversed (lowest ask at bottom, closest to mid)
  asksEl.innerHTML = asksWithCumul.map(l => makeRow(l, 'ask', myAskPrices)).join('');

  // Bids: show highest bid first (closest to mid at top)
  bidsEl.innerHTML = bidsWithCumul.map(l => makeRow(l, 'bid', myBidPrices)).join('');

  // Spread bar
  if (book && book.bid_px && book.ask_px) {
    const spread = book.ask_px - book.bid_px;
    const spreadBps = book.spread_bps != null ? book.spread_bps.toFixed(2) : (spread / book.mid * 1e4).toFixed(2);
    spreadEl.innerHTML = `<span style="color:var(--yellow)">spread ${fmt.px(spread)} (${spreadBps} bp)</span>
      &nbsp;·&nbsp; mid <strong style="color:var(--text)">${fmt.px(book.mid)}</strong>`;
  }
}

function renderML(ml, quote) {
  if (!ml) return;
  const warm = ml.warm;
  setText('ml-status', warm ? '✓ warm' : `warming (${ml.n_updates || 0} / 30 updates)`);
  const sigBps = ml.last_signal_bps;
  const sigEl = document.getElementById('ml-signal');
  if (sigEl) {
    sigEl.textContent = sigBps != null ? fmt.bp(sigBps) : '—';
    sigEl.classList.remove('pos', 'neg', 'neutral');
    sigEl.classList.add(pnlClass(sigBps));
  }
  if (quote) setText('ml-adj-fair', fmt.px(quote.adjusted_fair_px));
  setText('ml-updates', String(ml.n_updates || 0));
  setText('ml-mae', ml.mae_bps != null ? fmt.bp(ml.mae_bps) : '—');
  const corrEl = document.getElementById('ml-corr');
  if (corrEl && ml.correlation != null) {
    corrEl.textContent = ml.correlation.toFixed(4);
    corrEl.classList.remove('pos', 'neg', 'neutral');
    corrEl.classList.add(ml.correlation > 0.05 ? 'pos' : ml.correlation < -0.05 ? 'neg' : 'neutral');
  }
  const w = ml.weights || {};
  const wGrid = document.getElementById('ml-weights');
  if (wGrid) {
    wGrid.innerHTML = Object.entries(w).map(([k, v]) => {
      const cls = v > 0.01 ? 'pos' : v < -0.01 ? 'neg' : 'neutral';
      return `<span style="color:var(--muted)">${k}</span><span class="${cls}">${Number(v).toFixed(4)}</span>`;
    }).join('');
  }
}

// ─── Adaptive Skew (multi-armed bandit) ─────────────────────────────────────
function renderSkewAdapter(adapter) {
  const tbody = document.getElementById('skew-arms-tbody');
  const empty = document.getElementById('skew-empty');
  const summary = document.getElementById('skew-summary');
  if (!adapter) {
    if (tbody) tbody.innerHTML = '';
    if (empty) empty.style.display = 'block';
    if (summary) summary.textContent = '—';
    setText('skew-status', '—');
    return;
  }

  if (!adapter.enabled) {
    setText('skew-status', 'disabled (using static cfg)');
    setText('skew-base', fmt.bp(adapter.base_skew_bps));
    setText('skew-mult', '1.00x');
    setText('skew-eff', fmt.bp(adapter.effective_skew_bps));
    setText('skew-best', '—');
    setText('skew-obs', '—');
    setText('skew-eps', '—');
    if (tbody) tbody.innerHTML = '';
    if (empty) empty.style.display = 'block';
    if (summary) summary.textContent = 'adaptive skew disabled';
    return;
  }

  if (empty) empty.style.display = 'none';
  setText('skew-status', adapter.total_observations > 0 ? 'learning' : 'waiting for first closing fill');
  setText('skew-base', fmt.bp(adapter.base_skew_bps));
  setText('skew-mult', `${Number(adapter.current_multiplier).toFixed(2)}x`);
  setText('skew-eff', fmt.bp(adapter.effective_skew_bps));
  setText('skew-best', `${Number(adapter.best_multiplier).toFixed(2)}x`);
  setText('skew-obs', String(adapter.total_observations || 0));
  setText('skew-eps', Number(adapter.epsilon).toFixed(2));

  if (summary) {
    summary.textContent =
      `${adapter.total_observations || 0} closing fills evaluated · ` +
      `active ${Number(adapter.current_multiplier).toFixed(2)}x · ` +
      `best ${Number(adapter.best_multiplier).toFixed(2)}x · ` +
      `effective ${Number(adapter.effective_skew_bps).toFixed(2)}bp`;
  }

  if (!tbody) return;
  const arms = adapter.arms || [];
  tbody.innerHTML = arms.map(a => {
    const mult = Number(a.multiplier).toFixed(2);
    const effBps = Number(adapter.base_skew_bps * a.multiplier).toFixed(2);
    const meanCls = a.pulls === 0 ? 'neutral' : pnlClass(a.mean_edge_bps);
    const lastCls = a.pulls === 0 ? 'neutral' : pnlClass(a.last_edge_bps);
    const realCls = pnlClass(a.cum_realized_usd);
    let bg = '';
    let badge = '';
    if (a.is_active && a.is_best) {
      bg = 'background:rgba(74,222,128,0.10)';
      badge = ` <span style="color:#4ade80;font-size:10px">● active · best</span>`;
    } else if (a.is_active) {
      bg = 'background:rgba(74,222,128,0.08)';
      badge = ` <span style="color:#4ade80;font-size:10px">● active</span>`;
    } else if (a.is_best) {
      bg = 'background:rgba(96,165,250,0.06)';
      badge = ` <span style="color:#60a5fa;font-size:10px">★ best</span>`;
    }
    return `<tr style="${bg}">
      <td class="r">${mult}x${badge}</td>
      <td class="r">${effBps} bp</td>
      <td class="r">${a.pulls}</td>
      <td class="r ${meanCls}">${a.pulls === 0 ? '—' : Number(a.mean_edge_bps).toFixed(2) + ' bp'}</td>
      <td class="r ${lastCls}">${a.pulls === 0 ? '—' : Number(a.last_edge_bps).toFixed(2) + ' bp'}</td>
      <td class="r ${realCls}">${fmt.usd(a.cum_realized_usd)}</td>
      <td class="r" style="color:var(--muted)">${fmt.usd(a.cum_notional_usd)}</td>
    </tr>`;
  }).join('');
}

// ─── ML Adaptation: weights/accuracy/scatter charts ─────────────────────────
const ML_FEATURE_COLORS = {
  bbo_imbalance:  '#60a5fa',
  l2_imbalance:   '#22d3ee',
  momentum_5s:    '#4ade80',
  momentum_15s:   '#84cc16',
  momentum_60s:   '#facc15',
  trade_flow_5s:  '#fb923c',
  trade_flow_30s: '#f87171',
  spread_bps:     '#e879f9',
  funding:        '#a78bfa',
};

function _setupCanvas(canvas, height) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.parentElement.getBoundingClientRect();
  const W = Math.max(Math.floor(rect.width), 200);
  canvas.width  = W * dpr;
  canvas.height = height * dpr;
  canvas.style.height = height + 'px';
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  return { ctx, W, H: height };
}

function renderMLAdaptation(history, ml) {
  // Summary line at the top of the card
  const sum = document.getElementById('ml-adapt-summary');
  if (sum) {
    if (!history || history.length === 0) {
      sum.textContent = 'collecting…';
    } else {
      const first = history[0], last = history[history.length - 1];
      const dt = last.ts - first.ts;
      const mins = dt / 60;
      const dUpdates = last.n_updates - first.n_updates;
      const updRate = mins > 0 ? (dUpdates / mins) : 0;
      const corrAbs = Math.abs(last.correlation || 0);
      const skill = corrAbs > 0.15 ? 'good' : corrAbs > 0.05 ? 'developing' : corrAbs > 0.02 ? 'weak' : 'none';
      sum.textContent =
        `tracking ${mins.toFixed(1)} min · ${updRate.toFixed(1)} updates/min · ` +
        `predictive skill: ${skill} (|corr|=${corrAbs.toFixed(3)})`;
    }
  }

  renderMLWeightsChart(history);
  renderMLAccuracyChart(history);
  renderMLScatterChart(ml);
}

function renderMLWeightsChart(history) {
  const canvas = document.getElementById('ml-weights-chart');
  if (!canvas) return;
  const { ctx, W, H } = _setupCanvas(canvas, 220);
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = '#131820'; ctx.fillRect(0, 0, W, H);

  const PAD_L = 8, PAD_R = 8, PAD_T = 8, PAD_B = 22;

  if (!history || history.length < 2) {
    ctx.fillStyle = '#5a6480'; ctx.font = '12px ui-monospace,monospace'; ctx.textAlign = 'center';
    ctx.fillText('warming up… (need ≥2 ticks)', W / 2, H / 2);
    return;
  }

  // Determine global range across all features
  const features = Object.keys(ML_FEATURE_COLORS);
  let lo = Infinity, hi = -Infinity;
  for (const h of history) for (const f of features) {
    const v = h.weights?.[f]; if (v == null) continue;
    if (v < lo) lo = v; if (v > hi) hi = v;
  }
  if (!isFinite(lo) || !isFinite(hi) || lo === hi) { lo = -0.5; hi = 0.5; }
  const pad = (hi - lo) * 0.1 || 0.1;
  lo -= pad; hi += pad;

  const x0 = history[0].ts, x1 = history[history.length - 1].ts;
  const xRange = Math.max(x1 - x0, 1);
  const xOf = ts => PAD_L + (W - PAD_L - PAD_R) * (ts - x0) / xRange;
  const yOf = v  => PAD_T + (H - PAD_T - PAD_B) * (1 - (v - lo) / (hi - lo));

  // Zero line
  if (lo < 0 && hi > 0) {
    const y = Math.round(yOf(0)) + 0.5;
    ctx.strokeStyle = '#2a3142'; ctx.setLineDash([3, 5]);
    ctx.beginPath(); ctx.moveTo(PAD_L, y); ctx.lineTo(W - PAD_R, y); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = '#5a6480'; ctx.font = '10px ui-monospace,monospace'; ctx.textAlign = 'left';
    ctx.fillText('0', PAD_L + 2, y - 3);
  }

  // Lines per feature
  ctx.lineWidth = 1.4;
  for (const f of features) {
    const color = ML_FEATURE_COLORS[f];
    ctx.strokeStyle = color;
    ctx.beginPath();
    let started = false;
    for (const h of history) {
      const v = h.weights?.[f]; if (v == null) continue;
      const x = xOf(h.ts), y = yOf(v);
      if (!started) { ctx.moveTo(x, y); started = true; } else { ctx.lineTo(x, y); }
    }
    ctx.stroke();
  }

  // Time axis labels
  ctx.fillStyle = '#5a6480'; ctx.font = '10px ui-monospace,monospace'; ctx.textAlign = 'center';
  for (let i = 0; i <= 4; i++) {
    const ts = x0 + xRange * (i / 4);
    const x = xOf(ts);
    const d = new Date(ts * 1000);
    ctx.fillText(d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }), x, H - 6);
  }

  // Range label
  ctx.textAlign = 'right'; ctx.fillStyle = '#5a6480';
  ctx.fillText(hi.toFixed(3), W - PAD_R - 2, PAD_T + 10);
  ctx.fillText(lo.toFixed(3), W - PAD_R - 2, H - PAD_B - 2);

  // Build legend below
  const leg = document.getElementById('ml-weights-legend');
  if (leg) {
    leg.innerHTML = features.map(f => {
      const last = history[history.length - 1].weights?.[f];
      const v = last != null ? last.toFixed(3) : '—';
      return `<span><span style="display:inline-block;width:10px;height:3px;background:${ML_FEATURE_COLORS[f]};vertical-align:3px;margin-right:3px"></span>` +
             `<span style="color:var(--muted)">${f}</span> <span class="${last>0.01?'pos':last<-0.01?'neg':'neutral'}">${v}</span></span>`;
    }).join('');
  }
}

function renderMLAccuracyChart(history) {
  const canvas = document.getElementById('ml-accuracy-chart');
  if (!canvas) return;
  const { ctx, W, H } = _setupCanvas(canvas, 220);
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = '#131820'; ctx.fillRect(0, 0, W, H);

  const PAD_L = 30, PAD_R = 30, PAD_T = 8, PAD_B = 22;

  // Filter to records that have any predictive data
  const pts = (history || []).filter(h => (h.n_updates || 0) > 0);
  if (pts.length < 2) {
    ctx.fillStyle = '#5a6480'; ctx.font = '12px ui-monospace,monospace'; ctx.textAlign = 'center';
    ctx.fillText('waiting for first model updates…', W / 2, H / 2);
    return;
  }

  const x0 = pts[0].ts, x1 = pts[pts.length - 1].ts;
  const xRange = Math.max(x1 - x0, 1);
  const xOf = ts => PAD_L + (W - PAD_L - PAD_R) * (ts - x0) / xRange;

  // MAE on left axis
  let maeHi = Math.max(...pts.map(p => p.mae_bps), 0);
  if (maeHi < 0.5) maeHi = 0.5;
  maeHi *= 1.1;
  const yMAE = v => PAD_T + (H - PAD_T - PAD_B) * (1 - v / maeHi);

  // Correlation on right axis [-1, 1]
  const yCorr = v => PAD_T + (H - PAD_T - PAD_B) * (1 - (v + 1) / 2);

  // Grid lines for MAE
  ctx.strokeStyle = '#1e2535'; ctx.lineWidth = 1;
  ctx.fillStyle = '#5a6480'; ctx.font = '10px ui-monospace,monospace'; ctx.textAlign = 'right';
  for (let i = 0; i <= 4; i++) {
    const v = (maeHi / 4) * i;
    const y = Math.round(yMAE(v)) + 0.5;
    ctx.beginPath(); ctx.moveTo(PAD_L, y); ctx.lineTo(W - PAD_R, y); ctx.stroke();
    ctx.fillText(v.toFixed(2), PAD_L - 4, y + 3);
  }

  // Right axis labels (correlation)
  ctx.textAlign = 'left'; ctx.fillStyle = '#a78bfa99';
  for (const v of [-1, -0.5, 0, 0.5, 1]) {
    ctx.fillText(v.toFixed(1), W - PAD_R + 4, yCorr(v) + 3);
  }
  // Zero correlation line
  const yZero = Math.round(yCorr(0)) + 0.5;
  ctx.strokeStyle = '#2a3142'; ctx.setLineDash([3, 5]);
  ctx.beginPath(); ctx.moveTo(PAD_L, yZero); ctx.lineTo(W - PAD_R, yZero); ctx.stroke();
  ctx.setLineDash([]);

  // MAE line
  ctx.strokeStyle = '#fbbf24'; ctx.lineWidth = 1.6; ctx.beginPath();
  pts.forEach((p, i) => {
    const x = xOf(p.ts), y = yMAE(p.mae_bps);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();

  // Correlation line
  ctx.strokeStyle = '#a78bfa'; ctx.lineWidth = 1.6; ctx.beginPath();
  pts.forEach((p, i) => {
    const x = xOf(p.ts), y = yCorr(p.correlation || 0);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();

  // Time axis labels
  ctx.fillStyle = '#5a6480'; ctx.font = '10px ui-monospace,monospace'; ctx.textAlign = 'center';
  for (let i = 0; i <= 4; i++) {
    const ts = x0 + xRange * (i / 4);
    const d = new Date(ts * 1000);
    ctx.fillText(d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }), xOf(ts), H - 6);
  }

  // Latest value badges
  const last = pts[pts.length - 1];
  ctx.textAlign = 'left'; ctx.font = 'bold 11px ui-monospace,monospace';
  ctx.fillStyle = '#fbbf24'; ctx.fillText(`MAE ${last.mae_bps.toFixed(2)} bps`, PAD_L + 4, PAD_T + 12);
  ctx.fillStyle = '#a78bfa'; ctx.fillText(`corr ${(last.correlation || 0).toFixed(3)}`, PAD_L + 4, PAD_T + 26);
}

function renderMLScatterChart(ml) {
  const canvas = document.getElementById('ml-scatter-chart');
  if (!canvas) return;
  const { ctx, W, H } = _setupCanvas(canvas, 220);
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = '#131820'; ctx.fillRect(0, 0, W, H);

  const PAD = 26;
  const pred = (ml && ml.recent_pred) || [];
  const act  = (ml && ml.recent_actual) || [];
  const n = Math.min(pred.length, act.length);

  if (n < 3) {
    ctx.fillStyle = '#5a6480'; ctx.font = '12px ui-monospace,monospace'; ctx.textAlign = 'center';
    ctx.fillText('warming up…', W / 2, H / 2);
    return;
  }

  // Symmetric range so y=x diagonal sits at 45°
  let m = 0;
  for (let i = 0; i < n; i++) m = Math.max(m, Math.abs(pred[i]), Math.abs(act[i]));
  m = Math.max(m * 1.1, 0.5);

  const xOf = v => PAD + (W - 2 * PAD) * (v + m) / (2 * m);
  const yOf = v => PAD + (H - 2 * PAD) * (1 - (v + m) / (2 * m));

  // Axes through zero
  ctx.strokeStyle = '#2a3142'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(xOf(-m), yOf(0)); ctx.lineTo(xOf(m), yOf(0)); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(xOf(0), yOf(-m)); ctx.lineTo(xOf(0), yOf(m)); ctx.stroke();

  // y = x diagonal (perfect prediction)
  ctx.strokeStyle = '#4ade8088'; ctx.setLineDash([4, 4]);
  ctx.beginPath(); ctx.moveTo(xOf(-m), yOf(-m)); ctx.lineTo(xOf(m), yOf(m)); ctx.stroke();
  ctx.setLineDash([]);

  // Scatter points (newest = brightest)
  for (let i = 0; i < n; i++) {
    const age = (n - 1 - i) / Math.max(n - 1, 1);   // 0 = newest, 1 = oldest
    const alpha = 0.25 + 0.75 * (1 - age);
    const sign = pred[i] * act[i];
    ctx.fillStyle = sign >= 0 ? `rgba(74, 222, 128, ${alpha})` : `rgba(248, 113, 113, ${alpha})`;
    const x = xOf(pred[i]), y = yOf(act[i]);
    ctx.beginPath(); ctx.arc(x, y, 2.5, 0, Math.PI * 2); ctx.fill();
  }

  // Axis labels
  ctx.fillStyle = '#5a6480'; ctx.font = '10px ui-monospace,monospace';
  ctx.textAlign = 'center'; ctx.fillText('predicted (bps) →', W / 2, H - 6);
  ctx.save(); ctx.translate(10, H / 2); ctx.rotate(-Math.PI / 2);
  ctx.fillText('actual (bps) →', 0, 0); ctx.restore();

  // Range corner labels
  ctx.textAlign = 'right';
  ctx.fillText(`±${m.toFixed(2)} bps`, W - 4, PAD - 8);
  ctx.textAlign = 'left';
  // Hit-rate (sign agreement)
  let agree = 0;
  for (let i = 0; i < n; i++) if (Math.sign(pred[i]) === Math.sign(act[i]) && pred[i] !== 0) agree++;
  const hr = (agree / n * 100).toFixed(1);
  ctx.fillStyle = agree / n > 0.55 ? '#4ade80' : agree / n > 0.45 ? '#fbbf24' : '#f87171';
  ctx.font = 'bold 11px ui-monospace,monospace';
  ctx.fillText(`directional hit rate: ${hr}% (n=${n})`, PAD + 2, PAD - 8);
}

async function apiAction(url, label) {
  if (!confirm(`Send: ${label}?`)) return;
  try {
    const r = await fetch(url, { cache: 'no-store' });
    const d = await r.json();
    alert(d.ok ? `${label}: done` : `${label} failed: ${d.error || '?'}`);
  } catch(e) {
    alert(`${label} error: ${e.message}`);
  }
}

async function startLiveQuoting() {
  const btn = document.getElementById('start-quote-btn');
  if (!btn || btn.disabled) return;
  if (!confirm("Start posting bid and ask orders at the bot's target prices?")) return;
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'arming…';
  try {
    const r = await fetch('/api/start_quoting', { method: 'POST', cache: 'no-store' });
    const d = await r.json();
    if (d.ok) {
      btn.textContent = 'Live quoting on ✓';
      return;
    }
    btn.textContent = '✗ ' + (d.error || '?');
    btn.disabled = false;
  } catch (e) {
    btn.textContent = '✗ ' + e.message;
    btn.disabled = false;
  }
  setTimeout(() => { btn.textContent = prev; }, 4500);
}

async function cancelAll() {
  const btn = document.getElementById('cancel-btn');
  btn.textContent = 'cancelling…';
  btn.disabled = true;
  try {
    const r = await fetch('/api/cancel', { method: 'GET', cache: 'no-store' });
    const d = await r.json();
    if (d.ok) {
      btn.textContent = '✓ cancelled';
      btn.style.background = '#0e1b14';
      btn.style.borderColor = '#1e3a2a';
      btn.style.color = '#4ade80';
    } else {
      btn.textContent = '✗ failed: ' + (d.error || '?');
    }
  } catch(e) {
    btn.textContent = '✗ error: ' + e.message;
  }
  setTimeout(() => {
    btn.textContent = '⊗ Cancel All Orders';
    btn.style.background = '#3a1e1e';
    btn.style.borderColor = '#7a2a2a';
    btn.style.color = '#f87171';
    btn.disabled = false;
  }, 3000);
}

tick();
setInterval(tick, 1000);
</script>
</body>
</html>
"""


def _make_handler(
    snapshot_provider: Callable[[], dict],
    cancel_fn: Optional[Callable[[], None]] = None,
    flatten_buy_fn: Optional[Callable[[], None]] = None,
    flatten_sell_fn: Optional[Callable[[], None]] = None,
    update_config_fn: Optional[Callable[[dict], dict]] = None,
    start_quoting_fn: Optional[Callable[[], None]] = None,
):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            log.debug("%s - %s", self.address_string(), format % args)

        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path == "/api/config":
                if update_config_fn is None:
                    self._send_json(400, {"ok": False, "error": "config update not available"})
                    return
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    updates = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(updates, dict):
                        raise ValueError("body must be a JSON object")
                except Exception as exc:
                    self._send_json(400, {"ok": False, "error": f"bad JSON: {exc}"})
                    return
                try:
                    result = update_config_fn(updates)
                    self._send_json(200, result)
                except Exception as exc:
                    self._send_json(500, {"ok": False, "error": str(exc)})
                return

            if self.path == "/api/start_quoting":
                if start_quoting_fn is None:
                    self._send_json(400, {"ok": False, "error": "start quoting not available"})
                    return
                try:
                    start_quoting_fn()
                    self._send_json(200, {"ok": True})
                except Exception as exc:
                    self._send_json(500, {"ok": False, "error": str(exc)})
                return

            self.send_response(404)
            self.end_headers()

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                body = INDEX_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if self.path.startswith("/api/flatten"):
                side = "buy" if "side=buy" in self.path else "sell" if "side=sell" in self.path else None
                fn = flatten_buy_fn if side == "buy" else flatten_sell_fn if side == "sell" else None
                if fn is not None:
                    try:
                        fn()
                        body = json.dumps({"ok": True, "side": side}).encode()
                    except Exception as exc:
                        body = json.dumps({"ok": False, "error": str(exc)}).encode()
                else:
                    body = json.dumps({"ok": False, "error": "flatten not available"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if self.path == "/api/cancel":
                if cancel_fn is not None:
                    try:
                        cancel_fn()
                        body = json.dumps({"ok": True}).encode()
                    except Exception as exc:
                        body = json.dumps({"ok": False, "error": str(exc)}).encode()
                else:
                    body = json.dumps({"ok": False, "error": "cancel not available"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if self.path == "/api/snapshot":
                try:
                    snap = snapshot_provider()
                    body = json.dumps(snap).encode("utf-8")
                except Exception as exc:
                    body = json.dumps({"error": str(exc)}).encode("utf-8")
                    self.send_response(500)
                else:
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            self.send_response(404)
            self.end_headers()

    return Handler


class Dashboard:
    def __init__(
        self,
        host: str,
        port: int,
        snapshot_provider: Callable[[], dict],
        cancel_fn: Optional[Callable[[], None]] = None,
        flatten_buy_fn: Optional[Callable[[], None]] = None,
        flatten_sell_fn: Optional[Callable[[], None]] = None,
        update_config_fn: Optional[Callable[[dict], dict]] = None,
        start_quoting_fn: Optional[Callable[[], None]] = None,
    ):
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer = ThreadingHTTPServer(
            (host, port),
            _make_handler(
                snapshot_provider,
                cancel_fn,
                flatten_buy_fn,
                flatten_sell_fn,
                update_config_fn,
                start_quoting_fn,
            ),
        )
        self._thread: threading.Thread = threading.Thread(
            target=self._server.serve_forever, name="dashboard", daemon=True
        )

    def start(self) -> None:
        self._thread.start()
        log.info("dashboard listening on http://%s:%d", self.host, self.port)

    def stop(self) -> None:
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception as exc:
            log.warning("dashboard shutdown error: %s", exc)
