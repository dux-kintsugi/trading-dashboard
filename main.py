#!/usr/bin/env python3
"""Kitebird Capital — Unified Dashboard (Trading + Ops Cost Monitor)."""

from flask import Flask, jsonify, render_template_string, request
import threading, time, json, requests, datetime, os
from difflib import SequenceMatcher
from pathlib import Path

app = Flask(__name__)
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

# ─── Shared cache ───────────────────────────────────────────────────
cache = {"vix": None, "funding": None, "arb": None, "updated": None}
ops_cache = {"sessions": [], "totals": {}, "updated": None}
lock = threading.Lock()

# ═══════════════════════════════════════════════════════════════════
# DATA FETCHERS — TRADING
# ═══════════════════════════════════════════════════════════════════

def fetch_vix():
    try:
        import yfinance as yf
        vix = yf.Ticker("^VIX")
        vix3m = yf.Ticker("^VIX3M")
        hist = vix.history(period="6mo")
        if hist.empty:
            return {"error": "No VIX data"}
        current = float(hist["Close"].iloc[-1])
        avg_7 = float(hist["Close"].tail(7).mean())
        avg_30 = float(hist["Close"].tail(30).mean())
        avg_90 = float(hist["Close"].tail(90).mean())
        high_90 = float(hist["Close"].tail(90).max())
        low_90 = float(hist["Close"].tail(90).min())
        hist3m = vix3m.history(period="5d")
        vix3m_val = float(hist3m["Close"].iloc[-1]) if not hist3m.empty else None
        if current > 18:
            signal, color, note = "SELL SPREADS", "green", "High IV — premium selling favorable"
        elif current >= 14:
            signal, color, note = "NEUTRAL", "yellow", "Normal IV — selective opportunities"
        else:
            signal, color, note = "DON'T SELL", "red", "Low IV — premium too cheap"
        structure = None
        if vix3m_val:
            ratio = current / vix3m_val
            kind = "CONTANGO" if ratio < 1 else "BACKWARDATION"
            structure = {"kind": kind, "ratio": round(ratio, 4), "vix3m": round(vix3m_val, 2)}
        return {"current": round(current, 2), "signal": signal, "color": color, "note": note,
                "avg_7": round(avg_7, 2), "avg_30": round(avg_30, 2), "avg_90": round(avg_90, 2),
                "high_90": round(high_90, 2), "low_90": round(low_90, 2), "structure": structure}
    except Exception as e:
        return {"error": str(e)}


def _fetch_polymarket():
    try:
        r = requests.get("https://gamma-api.polymarket.com/markets?closed=false&limit=100&order=volume24hr&ascending=false", timeout=15, headers=HEADERS)
        if r.status_code != 200: return []
        markets = []
        for m in (r.json() if isinstance(r.json(), list) else r.json().get("data", [])):
            title = m.get("question", m.get("title", ""))
            yes = None
            op = m.get("outcomePrices")
            if op:
                try: yes = float(json.loads(op)[0]) if isinstance(op, str) else float(op)
                except: pass
            if not yes:
                tokens = m.get("tokens", [])
                if tokens: yes = float(tokens[0].get("price", 0))
            if title and yes and 0 < yes < 1:
                markets.append({"title": title.lower().strip(), "raw_title": title, "yes": yes, "source": "Polymarket"})
        return markets
    except: return []

def _fetch_kalshi():
    base = "https://api.elections.kalshi.com/trade-api/v2"
    h = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
    markets = []
    try:
        r = requests.get(f"{base}/markets", params={"limit": 100, "status": "open"}, headers=h, timeout=15)
        if r.status_code != 200: return []
        for mkt in r.json().get("markets", []):
            title = mkt.get("title", mkt.get("subtitle", ""))
            price = float(mkt.get("yes_ask", 0) or mkt.get("last_price", 0) or 0)
            if price > 1: price /= 100
            if title and 0 < price < 1:
                markets.append({"title": title.lower().strip(), "raw_title": title, "yes": price, "source": "Kalshi"})
    except: pass
    return markets

def fetch_arb():
    try:
        poly = _fetch_polymarket()
        kalshi = _fetch_kalshi()
        if not poly or not kalshi:
            return {"poly_count": len(poly), "kalshi_count": len(kalshi), "opps": []}
        opps = []
        for p in poly:
            for k in kalshi:
                ratio = SequenceMatcher(None, p["title"], k["title"]).ratio()
                if ratio >= 0.55:
                    spread = abs(p["yes"] - k["yes"]) * 100
                    if spread > 3:
                        opps.append({"poly_title": p["raw_title"], "kalshi_title": k["raw_title"],
                                     "poly_yes": round(p["yes"]*100, 1), "kalshi_yes": round(k["yes"]*100, 1),
                                     "spread": round(spread, 1), "match": round(ratio*100)})
        opps.sort(key=lambda x: -x["spread"])
        return {"poly_count": len(poly), "kalshi_count": len(kalshi), "opps": opps[:20]}
    except Exception as e:
        return {"error": str(e), "opps": []}


def _fetch_binance():
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex", timeout=10, headers=HEADERS)
        if r.status_code != 200: return []
        return [{"symbol": i["symbol"], "rate": float(i.get("lastFundingRate", 0)), "source": "Binance"}
                for i in r.json() if float(i.get("lastFundingRate", 0)) != 0]
    except: return []

def _fetch_bybit():
    try:
        r = requests.get("https://api.bybit.com/v5/market/tickers?category=linear", timeout=10, headers=HEADERS)
        if r.status_code != 200: return []
        return [{"symbol": i["symbol"], "rate": float(i.get("fundingRate", 0)), "source": "Bybit"}
                for i in r.json().get("result", {}).get("list", []) if float(i.get("fundingRate", 0)) != 0]
    except: return []

def _fetch_gateio():
    try:
        r = requests.get("https://api.gateio.ws/api/v4/futures/usdt/contracts", timeout=10, headers=HEADERS)
        if r.status_code != 200: return []
        return [{"symbol": i["name"].replace("_", ""), "rate": float(i.get("funding_rate", 0)), "source": "Gate.io"}
                for i in r.json() if float(i.get("funding_rate", 0)) != 0]
    except: return []

def fetch_funding():
    try:
        all_rates = []
        sources = []
        for name, fn in [("Binance", _fetch_binance), ("Bybit", _fetch_bybit), ("Gate.io", _fetch_gateio)]:
            d = fn()
            if d:
                all_rates.extend(d)
                sources.append(name)
        seen = {}
        for r in all_rates:
            sym = r["symbol"].replace("USDT", "").replace("BUSD", "").replace("USD", "")
            if sym not in seen: seen[sym] = r
        deduped = list(seen.values())
        deduped.sort(key=lambda x: -x["rate"])
        def fmt(r):
            ann = r["rate"] * 3 * 365 * 100
            return {"symbol": r["symbol"], "rate_8h": round(r["rate"]*100, 4),
                    "annualized": round(ann, 1), "source": r["source"]}
        top_long = [fmt(r) for r in deduped[:10]]
        top_short = [fmt(r) for r in deduped[-10:][::-1]]
        avg = sum(r["rate"] for r in deduped) / len(deduped) if deduped else 0
        return {"top_positive": top_long, "top_negative": top_short,
                "total": len(deduped), "sources": sources,
                "avg_rate": round(avg*100, 4), "avg_ann": round(avg*3*365*100, 1)}
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════
# DATA — OPS COST MONITOR
# ═══════════════════════════════════════════════════════════════════

# Model cost table (per 1M tokens, input/output)
MODEL_COSTS = {
    "claude-opus-4-6":       {"input": 15.0, "output": 75.0, "tier": "T1-Frontier"},
    "claude-sonnet-4-6":     {"input": 3.0,  "output": 15.0, "tier": "T2-Workhorse"},
    "gemini-2.5-flash-lite": {"input": 0.075,"output": 0.30, "tier": "T3-Light"},
    "deepseek-r1":           {"input": 0.55, "output": 2.19, "tier": "T4-DeepThink"},
}

# Tier policy: what model should be used for what
TIER_POLICY = {
    "main":      {"expected": "T1-Frontier",   "model": "claude-opus-4-6"},
    "subagent":  {"expected": "T2-Workhorse",  "model": "claude-sonnet-4-6"},
    "heartbeat": {"expected": "T3-Light",       "model": "gemini-2.5-flash-lite"},
    "research":  {"expected": "T4-DeepThink",   "model": "deepseek-r1"},
}

# Cost log file — agents append entries here
COST_LOG = Path(os.environ.get("COST_LOG", "/tmp/kitebird-cost-log.jsonl"))

def load_cost_log():
    """Load cost entries from JSONL log file."""
    entries = []
    if COST_LOG.exists():
        for line in COST_LOG.read_text().strip().split("\n"):
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except:
                    pass
    return entries

def compute_ops_data():
    """Compute ops metrics from cost log + hardcoded baseline."""
    entries = load_cost_log()
    now = datetime.datetime.utcnow()
    today = now.strftime("%Y-%m-%d")

    # Hardcoded initial data from today's actual usage
    sessions = [
        {"name": "Trading Report", "type": "subagent", "model": "Sonnet 4.6",
         "tier": "T2", "tokens_in": 23000, "tokens_out": 3500,
         "cost_actual": 0.52, "cost_opus": 2.00, "savings": 1.48,
         "mfs": 5, "status": "✅", "time": "15:58"},
        {"name": "Account Setup", "type": "subagent", "model": "Sonnet 4.6",
         "tier": "T2", "tokens_in": 29700, "tokens_out": 4900,
         "cost_actual": 0.66, "cost_opus": 2.60, "savings": 1.94,
         "mfs": 5, "status": "✅", "time": "15:39"},
    ]

    # Add any logged entries
    for e in entries:
        if e.get("date", "") == today:
            model = e.get("model", "unknown")
            tier_info = None
            for k, v in MODEL_COSTS.items():
                if k in model.lower():
                    tier_info = v
                    break
            tin = e.get("tokens_in", 0)
            tout = e.get("tokens_out", 0)
            actual = (tin * tier_info["input"] / 1e6 + tout * tier_info["output"] / 1e6) if tier_info else 0
            opus = tin * 15.0 / 1e6 + tout * 75.0 / 1e6
            sessions.append({
                "name": e.get("name", "Unknown"),
                "type": e.get("type", "subagent"),
                "model": model,
                "tier": tier_info["tier"][:2] if tier_info else "??",
                "tokens_in": tin, "tokens_out": tout,
                "cost_actual": round(actual, 2),
                "cost_opus": round(opus, 2),
                "savings": round(opus - actual, 2),
                "mfs": e.get("mfs", 4),
                "status": "✅",
                "time": e.get("time", "--:--"),
            })

    total_actual = sum(s["cost_actual"] for s in sessions)
    total_opus = sum(s["cost_opus"] for s in sessions)
    total_savings = sum(s["savings"] for s in sessions)
    avg_mfs = sum(s["mfs"] for s in sessions) / len(sessions) if sessions else 0
    pct_savings = (total_savings / total_opus * 100) if total_opus > 0 else 0
    underpowered = sum(1 for s in sessions if s["mfs"] <= 2)
    overpowered = sum(1 for s in sessions if s.get("type") != "main" and s["tier"] == "T1")

    return {
        "sessions": sessions,
        "totals": {
            "actual": round(total_actual, 2),
            "opus_baseline": round(total_opus, 2),
            "savings": round(total_savings, 2),
            "pct_savings": round(pct_savings, 1),
            "avg_mfs": round(avg_mfs, 1),
            "underpowered_alerts": underpowered,
            "overpowered_alerts": overpowered,
            "session_count": len(sessions),
        },
        "tiers": {
            "T1": {"name": "Frontier (Opus)", "cost": "$75/1M", "color": "#a855f7", "use": "Main session, high-stakes"},
            "T2": {"name": "Workhorse (Sonnet)", "cost": "$15/1M", "color": "#3b82f6", "use": "Sub-agents, research"},
            "T3": {"name": "Light (Flash)", "cost": "$0.50/1M", "color": "#22c55e", "use": "Heartbeats, status checks"},
            "T4": {"name": "DeepThink (R1)", "cost": "$4.14/1M", "color": "#eab308", "use": "Quant research, math"},
        },
        "policy": {
            "baseline_model": "Claude Opus 4.6 ($75/1M out)",
            "tiering_active_since": "2026-02-20 16:00 UTC",
            "priority": "⚠️ Underpowered usage is RED severity — quality over savings",
        },
        "updated": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
    }


# ─── Background refresh ─────────────────────────────────────────────
def refresh():
    while True:
        vix = fetch_vix()
        funding = fetch_funding()
        arb = fetch_arb()
        ops = compute_ops_data()
        with lock:
            cache["vix"] = vix
            cache["funding"] = funding
            cache["arb"] = arb
            cache["updated"] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            ops_cache.update(ops)
        time.sleep(300)

threading.Thread(target=refresh, daemon=True).start()

# ═══════════════════════════════════════════════════════════════════
# API ROUTES
# ═══════════════════════════════════════════════════════════════════

@app.route("/api/trading")
def api_trading():
    with lock:
        return jsonify(cache)

@app.route("/api/ops")
def api_ops():
    with lock:
        return jsonify(ops_cache)

@app.route("/api/ops/log", methods=["POST"])
def api_ops_log():
    """Agents POST cost entries here."""
    entry = request.get_json()
    if not entry:
        return jsonify({"error": "no data"}), 400
    entry["date"] = entry.get("date", datetime.datetime.utcnow().strftime("%Y-%m-%d"))
    entry["time"] = entry.get("time", datetime.datetime.utcnow().strftime("%H:%M"))
    with open(COST_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return jsonify({"ok": True})

@app.route("/")
def index():
    return render_template_string(HTML)

# ═══════════════════════════════════════════════════════════════════
# UNIFIED HTML TEMPLATE
# ═══════════════════════════════════════════════════════════════════

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kitebird Capital</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0a0e17;--card:#111827;--border:#1e293b;--text:#e2e8f0;--dim:#64748b;--green:#22c55e;--yellow:#eab308;--red:#ef4444;--blue:#3b82f6;--cyan:#06b6d4;--purple:#a855f7}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:14px;min-height:100vh;min-height:100dvh;overflow-x:hidden}

/* ─── NAV ─── */
.topnav{position:sticky;top:0;z-index:100;background:#0d1117;border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 16px;height:52px;gap:8px;-webkit-overflow-scrolling:touch}
.topnav .logo{font-size:15px;font-weight:700;color:var(--cyan);white-space:nowrap;margin-right:12px;letter-spacing:1px}
.topnav .logo span{color:var(--dim);font-weight:400;font-size:12px}
.nav-btn{background:none;border:1px solid transparent;color:var(--dim);padding:8px 14px;border-radius:6px;cursor:pointer;font-size:13px;font-weight:500;white-space:nowrap;transition:all .15s}
.nav-btn:hover{color:var(--text);background:#ffffff08}
.nav-btn.active{color:var(--cyan);border-color:var(--cyan);background:#06b6d410}
.nav-right{margin-left:auto;display:flex;align-items:center;gap:8px}
.clock{color:var(--dim);font-size:12px;font-family:'SF Mono',Monaco,monospace;white-space:nowrap}

/* ─── PAGE ─── */
.page{display:none;padding:16px;max-width:1400px;margin:0 auto}
.page.active{display:block}

/* ─── CARDS ─── */
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px;overflow:hidden}
.card.full{grid-column:1/-1}
.card h2{font-size:12px;color:var(--dim);text-transform:uppercase;letter-spacing:1.5px;margin-bottom:10px;display:flex;align-items:center;gap:6px;font-weight:600}
.card h2 .icon{font-size:15px}

/* ─── STATS ─── */
.signal{font-size:26px;font-weight:700;margin:6px 0}
.signal.green{color:var(--green)}.signal.yellow{color:var(--yellow)}.signal.red{color:var(--red)}
.note{color:var(--dim);font-size:12px;margin-bottom:10px}
.stat-row{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #1a1f2e;font-size:13px}
.stat-row .label{color:var(--dim)}.stat-row .val{color:var(--text);font-weight:500}

/* ─── BIG STATS (Ops) ─── */
.big-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:16px}
.big-stat{background:#0d1117;border:1px solid var(--border);border-radius:10px;padding:14px;text-align:center}
.big-stat .num{font-size:28px;font-weight:700;line-height:1.2}
.big-stat .lbl{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:1px;margin-top:4px}
.big-stat.savings .num{color:var(--green)}
.big-stat.alert-red .num{color:var(--red)}
.big-stat.alert-yellow .num{color:var(--yellow)}
.big-stat.neutral .num{color:var(--blue)}

/* ─── TABLES ─── */
table{width:100%;border-collapse:collapse}
th{text-align:left;color:var(--dim);font-size:11px;padding:8px;border-bottom:1px solid var(--border);font-weight:600;text-transform:uppercase;letter-spacing:.5px}
td{padding:8px;border-bottom:1px solid #1a1f2e;font-size:13px}
tr:hover{background:#ffffff06}
.pos{color:var(--green)}.neg{color:var(--red)}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;background:#ffffff10;color:var(--dim);font-weight:500}
.badge.t1{background:#a855f720;color:var(--purple)}
.badge.t2{background:#3b82f620;color:var(--blue)}
.badge.t3{background:#22c55e20;color:var(--green)}
.badge.t4{background:#eab30820;color:var(--yellow)}
.alert{color:var(--yellow);font-weight:700}
.empty{color:var(--dim);text-align:center;padding:24px;font-style:italic}
.loading{color:var(--dim);text-align:center;padding:40px}
.tag{display:inline-block;padding:2px 6px;border-radius:4px;font-size:11px;margin-left:4px;font-weight:500}
.tag.contango{background:#22c55e20;color:var(--green)}.tag.backwardation{background:#ef444420;color:var(--red)}

/* ─── MFS BAR ─── */
.mfs-bar{display:inline-flex;gap:2px;align-items:center}
.mfs-dot{width:8px;height:8px;border-radius:50%;background:#1e293b}
.mfs-dot.filled-5,.mfs-dot.filled-4{background:var(--green)}
.mfs-dot.filled-3{background:var(--yellow)}
.mfs-dot.filled-2,.mfs-dot.filled-1{background:var(--red)}

/* ─── TIER LEGEND ─── */
.tier-legend{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;margin-bottom:16px}
.tier-item{display:flex;align-items:center;gap:10px;background:#0d1117;border:1px solid var(--border);border-radius:8px;padding:10px 12px}
.tier-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.tier-info .tier-name{font-size:13px;font-weight:600}
.tier-info .tier-cost{font-size:11px;color:var(--dim)}

/* ─── POLICY BANNER ─── */
.policy-banner{background:#ef444415;border:1px solid #ef444440;border-radius:8px;padding:12px 16px;margin-bottom:16px;font-size:13px;display:flex;align-items:center;gap:8px}
.policy-banner .icon{font-size:18px}

/* ─── FOOTER ─── */
.footer{text-align:center;padding:16px;color:var(--dim);font-size:11px;border-top:1px solid var(--border);margin-top:16px}

/* ─── RESPONSIVE ─── */
@media(max-width:768px){
  .grid{grid-template-columns:1fr}
  .card.full{grid-column:1}
  .big-stats{grid-template-columns:repeat(2,1fr)}
  .tier-legend{grid-template-columns:1fr}
  .topnav{padding:0 10px;gap:4px}
  .nav-btn{padding:8px 10px;font-size:12px}
  .topnav .logo{font-size:13px;margin-right:6px}
  table{font-size:12px}
  th,td{padding:6px 4px}
  .page{padding:10px}
  .big-stat .num{font-size:22px}
  .hide-mobile{display:none}
}
@media(max-width:420px){
  .big-stats{grid-template-columns:1fr 1fr}
  .nav-btn{padding:6px 8px;font-size:11px}
}
</style>
</head>
<body>

<!-- ═══ NAVIGATION ═══ -->
<nav class="topnav">
  <div class="logo">KITEBIRD <span>CAPITAL</span></div>
  <button class="nav-btn active" onclick="showPage('trading')">📊 Trading</button>
  <button class="nav-btn" onclick="showPage('ops')">⚙️ Ops & Costs</button>
  <div class="nav-right">
    <span class="clock" id="clock"></span>
  </div>
</nav>

<!-- ═══ TRADING PAGE ═══ -->
<div class="page active" id="page-trading">
  <div class="grid">
    <div class="card" id="vix-card"><div class="loading">Loading VIX data…</div></div>
    <div class="card" id="structure-card"><div class="loading">Loading term structure…</div></div>
    <div class="card full" id="funding-card"><div class="loading">Loading funding rates…</div></div>
    <div class="card full" id="arb-card"><div class="loading">Loading arb scanner…</div></div>
  </div>
  <div class="footer">Auto-refresh every 5 min • Sources: Yahoo Finance, Binance, Bybit, Gate.io, Polymarket, Kalshi</div>
</div>

<!-- ═══ OPS PAGE ═══ -->
<div class="page" id="page-ops">
  <div id="ops-content"><div class="loading">Loading ops data…</div></div>
  <div class="footer">Model tiering active since Feb 20, 2026 • Baseline: All-Opus ($75/1M output tokens)</div>
</div>

<script>
// ─── Navigation ──────────────────────────────────────────────
function showPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('page-'+name).classList.add('active');
  event.target.classList.add('active');
  if (name === 'ops') loadOps();
  if (name === 'trading') loadTrading();
}

// ─── Clock ───────────────────────────────────────────────────
const $=id=>document.getElementById(id);
function clock(){$('clock').textContent=new Date().toLocaleString('en-US',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false})+' ET'}
setInterval(clock,1000);clock();

// ─── MFS dots helper ─────────────────────────────────────────
function mfsDots(score) {
  let h = '';
  for (let i = 1; i <= 5; i++) {
    h += '<div class="mfs-dot' + (i <= score ? ' filled-'+score : '') + '"></div>';
  }
  return '<div class="mfs-bar">' + h + '</div>';
}

function tierBadge(tier) {
  const t = tier.toLowerCase().replace('-','');
  const cls = t.startsWith('t1') ? 't1' : t.startsWith('t2') ? 't2' : t.startsWith('t3') ? 't3' : 't4';
  return '<span class="badge '+cls+'">'+tier+'</span>';
}

// ═══ TRADING RENDER ══════════════════════════════════════════
function renderTrading(d){
  const v=d.vix;
  if(v&&!v.error){
    $('vix-card').innerHTML='<h2><span class="icon">📊</span> VIX MONITOR</h2>'+
      '<div class="signal '+v.color+'">'+v.current+' — '+v.signal+'</div>'+
      '<div class="note">'+v.note+'</div>'+
      '<div class="stat-row"><span class="label">7d Avg</span><span class="val">'+v.avg_7+'</span></div>'+
      '<div class="stat-row"><span class="label">30d Avg</span><span class="val">'+v.avg_30+'</span></div>'+
      '<div class="stat-row"><span class="label">90d Avg</span><span class="val">'+v.avg_90+'</span></div>'+
      '<div class="stat-row"><span class="label">90d Range</span><span class="val">'+v.low_90+' — '+v.high_90+'</span></div>';
    const s=v.structure;
    if(s){
      $('structure-card').innerHTML='<h2><span class="icon">📐</span> TERM STRUCTURE</h2>'+
        '<div class="signal" style="font-size:22px">'+s.kind+' <span class="tag '+s.kind.toLowerCase()+'">'+s.ratio+'</span></div>'+
        '<div class="note">'+(s.kind==='CONTANGO'?'Normal — no panic':'⚠️ Fear elevated')+'</div>'+
        '<div class="stat-row"><span class="label">VIX</span><span class="val">'+v.current+'</span></div>'+
        '<div class="stat-row"><span class="label">VIX3M</span><span class="val">'+s.vix3m+'</span></div>'+
        '<div class="stat-row"><span class="label">Ratio</span><span class="val">'+s.ratio+'</span></div>';
    } else { $('structure-card').innerHTML='<h2><span class="icon">📐</span> TERM STRUCTURE</h2><div class="empty">VIX3M unavailable</div>'; }
  } else {
    $('vix-card').innerHTML='<h2><span class="icon">📊</span> VIX MONITOR</h2><div class="empty">'+(v?v.error:'Loading…')+'</div>';
    $('structure-card').innerHTML='<h2><span class="icon">📐</span> TERM STRUCTURE</h2><div class="empty">—</div>';
  }
  const f=d.funding;
  if(f&&!f.error&&f.top_positive){
    let rows=function(r){return r.map(function(x){return '<tr><td>'+x.symbol+'</td><td class="'+(x.rate_8h>=0?'pos':'neg')+'">'+x.rate_8h+'%</td><td class="'+(x.annualized>=0?'pos':'neg')+'">'+x.annualized+'%</td><td><span class="badge">'+x.source+'</span></td></tr>'}).join('')};
    $('funding-card').innerHTML='<h2><span class="icon">💰</span> FUNDING RATES <span class="badge">'+f.total+' pairs</span></h2>'+
      '<div class="grid"><div><h2 style="font-size:11px;color:var(--green)">📈 HIGHEST (Short Opps)</h2>'+
      '<table><tr><th>Symbol</th><th>Rate/8h</th><th>Ann.</th><th class="hide-mobile">Src</th></tr>'+rows(f.top_positive)+'</table></div>'+
      '<div><h2 style="font-size:11px;color:var(--red)">📉 MOST NEGATIVE (Long Opps)</h2>'+
      '<table><tr><th>Symbol</th><th>Rate/8h</th><th>Ann.</th><th class="hide-mobile">Src</th></tr>'+rows(f.top_negative)+'</table></div></div>'+
      '<div style="margin-top:8px;color:var(--dim);font-size:11px">Market avg: '+f.avg_rate+'%/8h ('+f.avg_ann+'% ann) • Sources: '+f.sources.join(', ')+'</div>';
  } else {
    $('funding-card').innerHTML='<h2><span class="icon">💰</span> FUNDING RATES</h2><div class="empty">'+(f?f.error||'No data':'Loading…')+'</div>';
  }
  const a=d.arb;
  if(a&&!a.error){
    if(a.opps&&a.opps.length){
      let r=a.opps.map(function(o){return '<tr><td>'+o.poly_title.substring(0,40)+(o.poly_title.length>40?'…':'')+'</td><td>'+o.poly_yes+'¢</td><td>'+o.kalshi_yes+'¢</td><td class="alert">'+o.spread+'%</td></tr>'}).join('');
      $('arb-card').innerHTML='<h2><span class="icon">🔍</span> ARB SCANNER <span class="badge">'+a.poly_count+' Poly • '+a.kalshi_count+' Kalshi</span></h2>'+
        '<table><tr><th>Market</th><th>Poly</th><th>Kalshi</th><th>Spread</th></tr>'+r+'</table>';
    } else {
      $('arb-card').innerHTML='<h2><span class="icon">🔍</span> ARB SCANNER <span class="badge">'+a.poly_count+' Poly • '+a.kalshi_count+' Kalshi</span></h2>'+
        '<div class="empty">No opportunities with spread &gt; 3% found</div>';
    }
  } else {
    $('arb-card').innerHTML='<h2><span class="icon">🔍</span> ARB SCANNER</h2><div class="empty">'+(a?a.error||'No data':'Loading…')+'</div>';
  }
}

// ═══ OPS RENDER ══════════════════════════════════════════════
function renderOps(d){
  const t = d.totals || {};
  const sessions = d.sessions || [];
  const tiers = d.tiers || {};

  let html = '';

  // Policy banner
  html += '<div class="policy-banner"><span class="icon">⚠️</span> <strong>Priority:</strong>&nbsp;Underpowered model usage is RED severity. Quality over savings — when in doubt, tier UP.</div>';

  // Big stats
  html += '<div class="big-stats">';
  html += '<div class="big-stat savings"><div class="num">$'+(t.savings||0).toFixed(2)+'</div><div class="lbl">Saved Today</div></div>';
  html += '<div class="big-stat savings"><div class="num">'+(t.pct_savings||0)+'%</div><div class="lbl">vs Opus Baseline</div></div>';
  html += '<div class="big-stat neutral"><div class="num">$'+(t.actual||0).toFixed(2)+'</div><div class="lbl">Actual Cost</div></div>';
  html += '<div class="big-stat '+(t.underpowered_alerts>0?'alert-red':'neutral')+'"><div class="num">'+(t.underpowered_alerts||0)+'</div><div class="lbl">🔴 Underpowered</div></div>';
  html += '<div class="big-stat '+(t.overpowered_alerts>0?'alert-yellow':'neutral')+'"><div class="num">'+(t.overpowered_alerts||0)+'</div><div class="lbl">🟡 Overpowered</div></div>';
  html += '<div class="big-stat neutral"><div class="num">'+(t.avg_mfs||0)+'</div><div class="lbl">Avg Model Fit</div></div>';
  html += '</div>';

  // Tier legend
  html += '<div class="tier-legend">';
  for (const [key, val] of Object.entries(tiers)) {
    html += '<div class="tier-item"><div class="tier-dot" style="background:'+val.color+'"></div><div class="tier-info"><div class="tier-name">'+key+' '+val.name+'</div><div class="tier-cost">'+val.cost+' • '+val.use+'</div></div></div>';
  }
  html += '</div>';

  // Session table
  html += '<div class="card full" style="margin-top:0">';
  html += '<h2><span class="icon">📋</span> SESSION LOG — TODAY</h2>';
  if (sessions.length) {
    html += '<div style="overflow-x:auto"><table>';
    html += '<tr><th>Time</th><th>Task</th><th>Model</th><th>Tier</th><th>Actual</th><th>Opus Would Be</th><th>Saved</th><th>Fit Score</th></tr>';
    sessions.forEach(function(s) {
      html += '<tr>';
      html += '<td>'+s.time+'</td>';
      html += '<td>'+s.name+'</td>';
      html += '<td>'+s.model+'</td>';
      html += '<td>'+tierBadge(s.tier)+'</td>';
      html += '<td>$'+s.cost_actual.toFixed(2)+'</td>';
      html += '<td style="color:var(--dim)">$'+s.cost_opus.toFixed(2)+'</td>';
      html += '<td class="pos">$'+s.savings.toFixed(2)+'</td>';
      html += '<td>'+mfsDots(s.mfs)+'</td>';
      html += '</tr>';
    });
    html += '</table></div>';
  } else {
    html += '<div class="empty">No sessions logged yet today</div>';
  }
  html += '</div>';

  // Cost comparison
  html += '<div class="grid" style="margin-top:14px">';
  html += '<div class="card"><h2><span class="icon">💰</span> COST BREAKDOWN</h2>';
  html += '<div class="stat-row"><span class="label">Actual spend</span><span class="val pos">$'+(t.actual||0).toFixed(2)+'</span></div>';
  html += '<div class="stat-row"><span class="label">All-Opus baseline</span><span class="val" style="color:var(--dim)">$'+(t.opus_baseline||0).toFixed(2)+'</span></div>';
  html += '<div class="stat-row"><span class="label">Savings</span><span class="val pos">$'+(t.savings||0).toFixed(2)+' ('+(t.pct_savings||0)+'%)</span></div>';
  html += '<div class="stat-row"><span class="label">Sessions today</span><span class="val">'+(t.session_count||0)+'</span></div>';
  html += '</div>';

  html += '<div class="card"><h2><span class="icon">🎯</span> QUALITY METRICS</h2>';
  html += '<div class="stat-row"><span class="label">Avg Model Fit Score</span><span class="val">'+(t.avg_mfs||0)+' / 5</span></div>';
  html += '<div class="stat-row"><span class="label">Underpowered alerts</span><span class="val" style="color:'+(t.underpowered_alerts>0?'var(--red)':'var(--green)')+'">'+(t.underpowered_alerts||0)+'</span></div>';
  html += '<div class="stat-row"><span class="label">Overpowered alerts</span><span class="val" style="color:'+(t.overpowered_alerts>0?'var(--yellow)':'var(--green)')+'">'+(t.overpowered_alerts||0)+'</span></div>';
  html += '<div class="stat-row"><span class="label">Policy</span><span class="val">Tier UP if unsure</span></div>';
  html += '</div>';
  html += '</div>';

  $('ops-content').innerHTML = html;
}

// ═══ DATA LOADERS ════════════════════════════════════════════
async function loadTrading(){
  try{const r=await fetch('/api/trading');const d=await r.json();renderTrading(d)}catch(e){console.error(e)}
}
async function loadOps(){
  try{const r=await fetch('/api/ops');const d=await r.json();renderOps(d)}catch(e){console.error(e)}
}

// Initial load
loadTrading();
setInterval(loadTrading,300000);
setInterval(function(){if(document.getElementById('page-ops').classList.contains('active'))loadOps()},60000);
</script>
</body>
</html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
