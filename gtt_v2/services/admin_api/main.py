"""Admin API — FastAPI REST server for signal management and dashboard."""
import asyncio
from contextlib import asynccontextmanager
import structlog
import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

from shared.config import settings, configure_logging
from shared.db.postgres import create_pool, run_migrations
from shared.redis.client import create_redis
from shared.instruments.cache import InstrumentCache
from shared.instruments.loader import download_instruments
from shared.heartbeat import heartbeat_loop

from .routers.signals import router as signals_router
from .routers.accounts import router as accounts_router

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging("admin-api")
    log.info("admin_api_starting")

    pool = await create_pool(settings.postgres_dsn)
    await run_migrations(pool)
    redis = await create_redis(settings.redis_host, settings.redis_port)

    # Load instruments into cache
    async with httpx.AsyncClient() as http:
        instruments = await download_instruments(http)
    cache = InstrumentCache(redis)
    await cache.load(instruments)

    app.state.pool = pool
    app.state.redis = redis
    log.info("admin_api_ready")

    asyncio.create_task(heartbeat_loop())
    yield

    await pool.close()


app = FastAPI(title="GTT Trading Admin API", lifespan=lifespan)
app.include_router(signals_router)
app.include_router(accounts_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>GTT Trading Dashboard</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, sans-serif; background: #f0f2f5; color: #111; font-size: 14px; }
  header { background: #0f172a; color: #fff; padding: 14px 24px; display: flex; align-items: center; gap: 12px; }
  header h1 { font-size: 1rem; font-weight: 600; letter-spacing: .02em; }
  .badge { font-size: .7rem; background: #334155; color: #e2e8f0; border-radius: 4px; padding: 2px 8px; }
  .refresh-note { margin-left: auto; font-size: .7rem; color: #94a3b8; }
  main { max-width: 1400px; margin: 24px auto; padding: 0 16px; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-bottom: 24px; }
  .card { background: #fff; border-radius: 8px; padding: 16px; border: 1px solid #e2e8f0; }
  .card .label { font-size: .7rem; color: #64748b; text-transform: uppercase; letter-spacing: .06em; margin-bottom: 6px; }
  .card .value { font-size: 1.5rem; font-weight: 700; }
  .card .value.green { color: #16a34a; }
  .card .value.red   { color: #dc2626; }
  .section-title { font-size: .9rem; font-weight: 600; margin-bottom: 10px; color: #0f172a; }
  .section-bar { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:10px; }
  .table-wrap { overflow-x: auto; border-radius: 8px; border: 1px solid #e2e8f0; }
  table { width: 100%; border-collapse: collapse; background: #fff; }
  thead { background: #f8fafc; }
  th { padding: 10px 12px; text-align: left; font-weight: 600; color: #475569; font-size: .68rem; text-transform: uppercase; letter-spacing: .06em; border-bottom: 1px solid #e2e8f0; white-space: nowrap; }
  td { padding: 10px 12px; border-bottom: 1px solid #f1f5f9; vertical-align: top; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: #f8fafc; }
  .instrument { font-weight: 600; font-size: .85rem; }
  .expiry { font-size: .7rem; color: #94a3b8; margin-top: 2px; }
  .price-cell { white-space: nowrap; }
  .raw  { color: #64748b; font-size: .78rem; }
  .adj  { color: #0f172a; font-weight: 600; font-size: .85rem; }
  .arrow { color: #94a3b8; margin: 0 3px; font-size: .7rem; }
  .targets-raw  { color: #64748b; font-size: .75rem; }
  .targets-adj  { color: #0f172a; font-size: .8rem; font-weight: 600; }
  .status { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: .68rem; font-weight: 700; white-space: nowrap; }
  .status.PENDING     { background: #fef9c3; color: #854d0e; }
  .status.ACTIVE      { background: #dcfce7; color: #166534; }
  .status.STOPLOSS_HIT{ background: #fee2e2; color: #991b1b; }
  .status.TARGET1_HIT { background: #d1fae5; color: #065f46; }
  .status.TARGET2_HIT { background: #a7f3d0; color: #065f46; }
  .status.TARGET3_HIT { background: #6ee7b7; color: #064e3b; }
  .status.BLOCKED     { background: #fef2f2; color: #b91c1c; }
  .status.FAILED      { background: #fee2e2; color: #7f1d1d; }
  .status.CANCELLED   { background: #f1f5f9; color: #64748b; }
  .status.EXPIRED     { background: #f1f5f9; color: #94a3b8; }
  .block-reason { font-size: .72rem; color: #b91c1c; margin-top: 4px; max-width: 200px; line-height: 1.4; }
  .gtt-ids { font-size: .7rem; color: #0ea5e9; margin-top: 3px; word-break: break-all; }
  .action-buy  { color: #16a34a; font-weight: 700; }
  .action-sell { color: #dc2626; font-weight: 700; }
  .trade-type { font-size: .72rem; background: #f1f5f9; color: #475569; padding: 1px 6px; border-radius: 4px; }
  .btn-cancel { background: none; border: 1px solid #dc2626; color: #dc2626; padding: 3px 10px; border-radius: 4px; cursor: pointer; font-size: .72rem; }
  .btn-cancel:hover { background: #fee2e2; }
  .btn-exit { background: none; border: 1px solid #7c3aed; color: #7c3aed; padding: 3px 10px; border-radius: 4px; cursor: pointer; font-size: .72rem; margin-left: 4px; }
  .btn-exit:hover { background: #ede9fe; }
  .btn-export { background:#0f172a; border:1px solid #0f172a; color:#fff; padding:7px 12px; border-radius:6px; cursor:pointer; font-size:.75rem; font-weight:600; white-space:nowrap; }
  .btn-export:hover { background:#1e293b; border-color:#1e293b; }
  #msg { position: fixed; bottom: 20px; right: 20px; background: #0f172a; color: #fff; padding: 10px 18px; border-radius: 6px; display: none; font-size: .82rem; z-index: 999; }
  .time-cell { font-size: .75rem; color: #64748b; white-space: nowrap; }
  .pos-section { margin-top: 28px; }
  .pos-pnl-pos { color: #16a34a; font-weight: 700; }
  .pos-pnl-neg { color: #dc2626; font-weight: 700; }
  .pos-qty { font-size: .75rem; color: #64748b; }
  #error-banner { display:none; background:#fee2e2; color:#991b1b; border:1px solid #fca5a5; border-radius:6px; padding:10px 16px; margin-bottom:14px; font-size:.82rem; }
  .legend { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:24px; }
  .legend-item { display:flex; align-items:center; gap:5px; font-size:.72rem; color:#475569; }
  .legend-dot { width:10px; height:10px; border-radius:50%; flex-shrink:0; }
  .pipeline-section { margin-top: 28px; }
  .pipe-log { background:#fff; border:1px solid #e2e8f0; border-radius:8px; overflow:hidden; }
  .pipe-row { display:grid; grid-template-columns:52px 60px 1fr; gap:0; border-bottom:1px solid #f1f5f9; padding:7px 12px; align-items:start; font-size:.78rem; line-height:1.45; }
  .pipe-row:last-child { border-bottom:none; }
  .pipe-row:hover { background:#f8fafc; }
  .pipe-ts { font-size:.7rem; color:#94a3b8; white-space:nowrap; padding-top:1px; }
  .pipe-badge { display:inline-block; border-radius:4px; padding:1px 6px; font-size:.65rem; font-weight:700; white-space:nowrap; margin-top:1px; }
  .pipe-badge.info    { background:#eff6ff; color:#1d4ed8; }
  .pipe-badge.success { background:#dcfce7; color:#166534; }
  .pipe-badge.warning { background:#fef9c3; color:#854d0e; }
  .pipe-badge.error   { background:#fee2e2; color:#991b1b; }
  .pipe-msg { padding-left:8px; color:#1e293b; }
  .pipe-signal { font-weight:600; color:#0f172a; margin-right:5px; font-size:.75rem; }
</style>
</head>
<body>
<header>
  <h1>GTT Trading Dashboard</h1>
  <span class="badge" id="profile-badge">loading…</span>
  <span class="refresh-note">auto-refresh every 15s</span>
</header>
<main>
  <div id="error-banner"></div>

  <div class="legend">
    <div class="legend-item"><div class="legend-dot" style="background:#b45309"></div><strong>PENDING</strong> — GTT placed on Upstox, waiting for price to reach entry trigger</div>
    <div class="legend-item"><div class="legend-dot" style="background:#16a34a"></div><strong>ACTIVE</strong> — Entry leg triggered, position is open, SL + Target GTT now live</div>
    <div class="legend-item"><div class="legend-dot" style="background:#065f46"></div><strong>TARGET_HIT</strong> — Target leg executed, trade closed at profit</div>
    <div class="legend-item"><div class="legend-dot" style="background:#991b1b"></div><strong>STOPLOSS_HIT</strong> — Stoploss leg executed, trade closed at loss</div>
    <div class="legend-item"><div class="legend-dot" style="background:#b91c1c"></div><strong>BLOCKED</strong> — Signal rejected before GTT was placed (see reason below signal)</div>
    <div class="legend-item"><div class="legend-dot" style="background:#7f1d1d"></div><strong>FAILED</strong> — GTT placement attempted but Upstox returned an error</div>
    <div class="legend-item"><div class="legend-dot" style="background:#64748b"></div><strong>CANCELLED</strong> — Manually cancelled via this dashboard</div>
    <div class="legend-item"><div class="legend-dot" style="background:#94a3b8"></div><strong>EXPIRED</strong> — GTT never triggered; auto-cancelled after timeout or price passed entry zone</div>
  </div>

  <div class="stats">
    <div class="card"><div class="label">Active GTTs</div><div class="value" id="s-active">—</div></div>
    <div class="card"><div class="label">Pending</div><div class="value" id="s-pending">—</div></div>
    <div class="card"><div class="label">Targets Hit</div><div class="value green" id="s-targets">—</div></div>
    <div class="card"><div class="label">SL Hit</div><div class="value red" id="s-sl">—</div></div>
    <div class="card">
      <div class="label">Today P&L</div>
      <div class="value" id="s-pnl">—</div>
      <div id="s-pnl-sub" style="font-size:.65rem;color:#64748b;margin-top:3px;line-height:1.5"></div>
    </div>
  </div>

  <div class="section-bar">
    <p class="section-title" style="margin-bottom:0">Signals (last 50)</p>
    <button class="btn-export" onclick="exportTrades()">Export Trades CSV</button>
  </div>
  <div class="table-wrap">
  <table>
    <thead><tr>
      <th>ID</th>
      <th>Time (IST)</th>
      <th>Instrument</th>
      <th>Action</th>
      <th>Entry<br><span style="font-weight:400;color:#94a3b8">raw → adj</span></th>
      <th>Stop Loss<br><span style="font-weight:400;color:#94a3b8">raw → adj</span></th>
      <th>Targets<br><span style="font-weight:400;color:#94a3b8">raw → adj</span></th>
      <th>Trade Type</th>
      <th>Status / Reason</th>
      <th>GTT IDs</th>
      <th></th>
    </tr></thead>
    <tbody id="signals-body">
      <tr><td colspan="11" style="text-align:center;color:#aaa;padding:28px">Loading…</td></tr>
    </tbody>
  </table>
  </div>

  <div class="pos-section">
    <p class="section-title" style="margin-top:0">Live Positions (Upstox)</p>
    <div class="table-wrap">
    <table>
      <thead><tr>
        <th>Symbol</th>
        <th>Qty</th>
        <th>Avg Price</th>
        <th>LTP</th>
        <th>Unrealised P&L</th>
        <th>Realised P&L</th>
        <th>Action</th>
      </tr></thead>
      <tbody id="positions-body">
        <tr><td colspan="7" style="text-align:center;color:#aaa;padding:16px">Loading…</td></tr>
      </tbody>
    </table>
    </div>
  </div>

  <div class="pipeline-section">
    <p class="section-title" style="margin-top:0">Signal Pipeline — Live Audit Log</p>
    <p style="font-size:.72rem;color:#64748b;margin-bottom:10px">Every step from Telegram message to GTT placement. Newest events at top. Auto-refreshes every 15s.</p>
    <div class="pipe-log" id="pipeline-log">
      <div class="pipe-row"><span class="pipe-ts">—</span><span></span><span class="pipe-msg" style="color:#aaa">Loading…</span></div>
    </div>
  </div>
</main>
<div id="msg"></div>

<script>
function fmt(v) { return (v == null || v === '') ? '—' : parseFloat(v).toFixed(1); }
let latestSummary = null;
let latestSignals = [];
let latestLivePnl = null;

function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', timeZone: 'Asia/Kolkata' })
    + '<br><span style="font-size:.68rem;color:#94a3b8">'
    + d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', timeZone: 'Asia/Kolkata' })
    + '</span>';
}

function fmtTargets(raw, adj) {
  const r = (raw || []).map(v => parseFloat(v).toFixed(0)).join(' / ');
  const a = (adj || []).map(v => parseFloat(v).toFixed(1)).join(' / ');
  if (!r && !a) return '—';
  return `<div class="targets-raw">${r || '—'}</div>`
       + `<div class="targets-adj">${a || '—'}</div>`;
}

function fmtBlockReason(reason) {
  if (!reason) return '';

  // prefix-based reasons
  if (reason.startsWith('rr_ratio:')) {
    const rr = reason.split(':')[1];
    return `Risk:Reward = ${rr} (minimum 2.0 required). Target is too close to entry.`;
  }
  if (reason.startsWith('timing:')) {
    const r = reason.split(':')[1];
    const tm = {
      before_market_open: 'Market not open yet — signal received before 9:15 AM IST',
      safe_before_9_30:   'SAFE profile cannot trade before 9:30 AM IST',
      after_entry_close:  'Entry window closed — no new GTTs after 3:30 PM IST',
      weekend:            'Market closed on weekends',
    };
    return tm[r] || ('Timing gate blocked: ' + r);
  }
  if (reason.startsWith('missed_entry:')) {
    const detail = reason.split(':').slice(1).join(':');
    return 'Missed entry — price already moved past entry zone: ' + detail;
  }
  if (reason.startsWith('upstox_reject:')) {
    return 'Upstox rejected GTT: ' + reason.slice('upstox_reject:'.length);
  }
  if (reason.startsWith('entry_not_filled_target_hit:')) {
    const detail = reason.slice('entry_not_filled_target_hit:'.length);
    return 'Auto-cancelled: price hit target before entry leg was triggered. ' + detail;
  }

  // exact-match reasons
  const map = {
    'no_targets':            'Signal has no targets defined — GTT requires at least one target',
    'no_gtt_ids_returned':   'Upstox returned no GTT IDs after placement — order may not have been created',
    'daily_pnl_limit':       'Daily P&L limit reached (±10% of capital). No more trades today.',
    'instrument_not_found':  'Strike or expiry not found in Upstox instrument master',
    'rate_limited':          'GTT rate limit hit — too many orders in short time window',
    'manual_cancel':         'GTT cancelled manually via dashboard',
    'manual_exit':           'Position exited manually via dashboard (GTT cancelled + market order placed)',
    'stale_expiry':          'GTT auto-expired — entry was never triggered within the allowed time window',
    'instrument_not_found':  'Instrument not found — check if strike/expiry is correct',
  };
  return map[reason] || reason;
}

async function load() {
  document.getElementById('error-banner').style.display = 'none';
  try {
    const [sum, sigs, livePnl] = await Promise.all([
      fetch('/accounts/summary').then(r => r.json()),
      fetch('/signals?limit=50').then(r => r.json()),
      fetch('/accounts/live-pnl').then(r => r.json()).catch(() => null),
    ]);
    latestSummary = sum;
    latestSignals = sigs || [];
    latestLivePnl = livePnl;

    // ── Stats cards ──────────────────────────────────────────────────────────
    document.getElementById('s-active').textContent  = sum.active_signals ?? '—';
    document.getElementById('s-pending').textContent = sum.pending_signals ?? '—';
    document.getElementById('s-targets').textContent = sum.targets_hit ?? '—';
    document.getElementById('s-sl').textContent      = sum.stoploss_hit ?? '—';

    // Profile badge — sourced from /accounts/summary (trader_profile field)
    const badge = document.getElementById('profile-badge');
    if (sum.trader_profile) badge.textContent = sum.trader_profile;

    // P&L card — uses live-pnl (unrealized + realized) with fallback to realized-only
    const pnlEl  = document.getElementById('s-pnl');
    const pnlSub = document.getElementById('s-pnl-sub');
    let pnl;
    if (livePnl && livePnl.unrealized_error === null) {
      pnl = parseFloat(livePnl.total_pnl ?? 0);
      const unr = parseFloat(livePnl.unrealized_pnl ?? 0);
      const rel = parseFloat(livePnl.realized_pnl  ?? 0);
      pnlSub.innerHTML =
        `<span style="color:#0ea5e9">unr ₹${unr.toFixed(0)}</span>` +
        ` + <span>rel ₹${rel.toFixed(0)}</span>`;
    } else {
      pnl = parseFloat(sum.total_pnl ?? 0);
      pnlSub.textContent = livePnl ? 'positions unavailable' : 'realized only';
    }
    pnlEl.textContent = '₹' + pnl.toFixed(0);
    pnlEl.className   = 'value ' + (pnl >= 0 ? 'green' : 'red');

    const tbody = document.getElementById('signals-body');
    if (!sigs.length) {
      tbody.innerHTML = '<tr><td colspan="11" style="text-align:center;color:#aaa;padding:28px">No signals yet</td></tr>';
    } else {
      tbody.innerHTML = sigs.map(s => {
        const blockText = fmtBlockReason(s.block_reason);
        const gttIds = (s.gtt_order_ids || []).join('<br>');
        let actions = '';
        if (['PENDING','ACTIVE'].includes(s.status)) {
          actions += `<button class="btn-cancel" onclick="cancelSignal(${s.id})">Cancel GTT</button>`;
          if (s.status === 'ACTIVE') {
            actions += `<button class="btn-exit" onclick="exitSignal(${s.id})">Exit Trade</button>`;
          }
        }
        return `<tr>
          <td style="color:#94a3b8;font-size:.75rem">${s.id}</td>
          <td class="time-cell">${fmtTime(s.signal_at)}</td>
          <td>
            <div class="instrument">${s.underlying} ${s.strike ?? ''}${s.option_type ?? ''}</div>
            <div class="expiry">${s.expiry ?? ''}</div>
          </td>
          <td><span class="action-${(s.action||'').toLowerCase()}">${s.action}</span></td>
          <td class="price-cell">
            <span class="raw">${fmt(s.entry_low_raw)}</span>
            <span class="arrow">→</span>
            <span class="adj">${fmt(s.entry_low_adj)}</span>
          </td>
          <td class="price-cell">
            <span class="raw">${fmt(s.stoploss_raw)}</span>
            <span class="arrow">→</span>
            <span class="adj">${fmt(s.stoploss_adj)}</span>
          </td>
          <td>${fmtTargets(s.targets_raw, s.targets_adj)}</td>
          <td><span class="trade-type">${s.trade_type ?? '—'}</span></td>
          <td>
            <span class="status ${s.status}">${s.status}</span>
            ${blockText ? `<div class="block-reason">${blockText}</div>` : ''}
          </td>
          <td class="gtt-ids">${gttIds || '—'}</td>
          <td style="white-space:nowrap">${actions}</td>
        </tr>`;
      }).join('');
    }
  } catch(e) {
    console.error(e);
    const banner = document.getElementById('error-banner');
    banner.textContent = 'Dashboard failed to load data: ' + (e.message || String(e)) + '. Check if the admin API server is running.';
    banner.style.display = 'block';
  }
  loadPositions();
  loadPipelineLog();
}

async function loadPositions() {
  try {
    const positions = await fetch('/accounts/positions').then(r => r.json());
    const tbody = document.getElementById('positions-body');
    const active = (positions || []).filter(p => p.quantity !== 0);
    if (!active.length) {
      tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:#aaa;padding:16px">No open positions</td></tr>';
      return;
    }
    tbody.innerHTML = active.map(p => {
      const unr = parseFloat(p.unrealised ?? p.pnl ?? 0);
      const rel = parseFloat(p.realised ?? 0);
      const ltp = parseFloat(p.last_price ?? 0);
      const avg = parseFloat(p.average_price ?? 0);
      const instrKey = (p.instrument_key || p.instrument_token || '').replace(/'/g, "\\'");
      const qty = Math.abs(parseInt(p.quantity || p.buy_quantity || 0));
      const txnType = (parseInt(p.buy_quantity ?? 0) > 0) ? 'BUY' : 'SELL';
      let actionCell = `<button class="btn-exit" onclick="exitPosition('${instrKey}',${qty},'${txnType}')">Exit Position</button>`;
      if (p.signal_id) {
        actionCell += ` <button class="btn-exit" style="margin-left:4px;font-size:.68rem" onclick="exitSignal(${p.signal_id})">Exit via Signal</button>`;
      }
      return `<tr>
        <td style="font-weight:600">${p.trading_symbol || p.instrument_token || '—'}</td>
        <td class="pos-qty">${p.quantity}</td>
        <td>${avg.toFixed(2)}</td>
        <td>${ltp.toFixed(2)}</td>
        <td class="${unr >= 0 ? 'pos-pnl-pos' : 'pos-pnl-neg'}">₹${unr.toFixed(2)}</td>
        <td class="${rel >= 0 ? 'pos-pnl-pos' : 'pos-pnl-neg'}">₹${rel.toFixed(2)}</td>
        <td style="white-space:nowrap">${actionCell}</td>
      </tr>`;
    }).join('');
  } catch(e) {
    document.getElementById('positions-body').innerHTML =
      '<tr><td colspan="7" style="text-align:center;color:#aaa;padding:16px">Could not load positions</td></tr>';
  }
}

async function cancelSignal(id) {
  if (!confirm('Cancel GTT for signal ' + id + '?')) return;
  const r = await fetch('/signals/' + id + '/cancel', { method: 'POST' });
  const d = await r.json();
  showMsg(r.ok ? 'Cancelled GTT for signal ' + id : 'Error: ' + (d.detail || d.status));
  if (r.ok) load();
}

async function exitPosition(instrumentKey, qty, txnType) {
  if (!confirm('Exit position for ' + instrumentKey + '? A market order will be placed immediately.')) return;
  const r = await fetch('/accounts/positions/exit', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({instrument_key: instrumentKey, quantity: qty, transaction_type: txnType})
  });
  const d = await r.json();
  showMsg(r.ok ? 'Exit order placed for ' + instrumentKey : 'Error: ' + (d.detail || d.status));
  if (r.ok) load();
}

async function exitSignal(id) {
  if (!confirm('EXIT TRADE for signal ' + id + '? This will cancel the GTT and place a market SELL order.')) return;
  const r = await fetch('/signals/' + id + '/exit', { method: 'POST' });
  const d = await r.json();
  if (r.ok) {
    const errs = (d.errors || []).map(e => JSON.stringify(e)).join('; ');
    showMsg('Exited signal ' + id + (errs ? ' (errors: ' + errs + ')' : ''));
    load();
  } else {
    showMsg('Exit failed: ' + (d.detail || d.status));
  }
}

function showMsg(text) {
  const el = document.getElementById('msg');
  el.textContent = text;
  el.style.display = 'block';
  setTimeout(() => el.style.display = 'none', 3500);
}

function csvCell(v) {
  const text = v == null ? '' : String(v);
  return '"' + text.replace(/"/g, '""') + '"';
}

function exportTrades() {
  if (!latestSignals.length) {
    showMsg('No trades to export yet');
    return;
  }

  const now = new Date();
  const snapshotIst = now.toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: true,
  });

  const totalPnl = latestLivePnl ? latestLivePnl.total_pnl : (latestSummary ? latestSummary.total_pnl : '');
  const realizedPnl = latestLivePnl ? latestLivePnl.realized_pnl : (latestSummary ? latestSummary.realized_pnl : '');
  const unrealizedPnl = latestLivePnl ? latestLivePnl.unrealized_pnl : '';

  const lines = [
    [csvCell('Exported At (IST)'), csvCell(snapshotIst)].join(','),
    [csvCell('Dashboard Total P&L'), csvCell(totalPnl)].join(','),
    [csvCell('Dashboard Realized P&L'), csvCell(realizedPnl)].join(','),
    [csvCell('Dashboard Unrealized P&L'), csvCell(unrealizedPnl)].join(','),
    [csvCell('Trader Profile'), csvCell(latestSummary?.trader_profile ?? '')].join(','),
    '',
    [
      'ID',
      'Signal Time (IST)',
      'Underlying',
      'Strike',
      'Option Type',
      'Expiry',
      'Action',
      'Trade Type',
      'Status',
      'Status Reason',
      'Entry Raw',
      'Entry Adjusted',
      'Stoploss Raw',
      'Stoploss Adjusted',
      'Targets Raw',
      'Targets Adjusted',
      'Entry Price',
      'Exit Price',
      'PnL',
      'GTT IDs'
    ].map(csvCell).join(',')
  ];

  for (const s of latestSignals) {
    const signalTimeIst = s.signal_at ? new Date(s.signal_at).toLocaleString('en-IN', {
      timeZone: 'Asia/Kolkata',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: true,
    }) : '';
    lines.push([
      s.id,
      signalTimeIst,
      s.underlying,
      s.strike ?? '',
      s.option_type ?? '',
      s.expiry ?? '',
      s.action ?? '',
      s.trade_type ?? '',
      s.status ?? '',
      fmtBlockReason(s.block_reason),
      s.entry_low_raw ?? '',
      s.entry_low_adj ?? '',
      s.stoploss_raw ?? '',
      s.stoploss_adj ?? '',
      (s.targets_raw || []).join(' / '),
      (s.targets_adj || []).join(' / '),
      s.entry_price ?? '',
      s.exit_price ?? '',
      s.pnl ?? '',
      (s.gtt_order_ids || []).join(' | ')
    ].map(csvCell).join(','));
  }

  const blob = new Blob([lines.join('\\n')], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  const stamp = now.toISOString().slice(0, 19).replace(/[:T]/g, '-');
  a.href = url;
  a.download = `gtt-dashboard-export-${stamp}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  showMsg('Trades exported');
}

async function loadPipelineLog() {
  try {
    const events = await fetch('/accounts/pipeline-log?count=60').then(r => r.json());
    const el = document.getElementById('pipeline-log');
    if (!events || !events.length) {
      el.innerHTML = '<div class="pipe-row"><span class="pipe-ts">—</span><span></span><span class="pipe-msg" style="color:#aaa">No pipeline events yet. Waiting for a Telegram signal…</span></div>';
      return;
    }
    el.innerHTML = events.map(e => {
      const sig = e.signal ? `<span class="pipe-signal">${e.signal}</span>` : '';
      return `<div class="pipe-row">
        <span class="pipe-ts">${e.ts || ''}</span>
        <span><span class="pipe-badge ${e.status || 'info'}">${(e.status||'info').toUpperCase()}</span></span>
        <span class="pipe-msg">${sig}${e.message || ''}</span>
      </div>`;
    }).join('');
  } catch(e) {
    document.getElementById('pipeline-log').innerHTML =
      '<div class="pipe-row"><span class="pipe-ts">—</span><span></span><span class="pipe-msg" style="color:#b91c1c">Could not load pipeline log: ' + (e.message||String(e)) + '</span></div>';
  }
}

load();
setInterval(load, 15000);
</script>
</body>
</html>"""
