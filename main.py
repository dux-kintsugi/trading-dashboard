#!/usr/bin/env python3
"""QuantLab Trading Dashboard — VIX + Funding Rates + Arb Scanner."""

from flask import Flask, jsonify, render_template_string
import threading, time, json, requests, datetime
from difflib import SequenceMatcher

app = Flask(__name__)
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

# ─── Shared cache ───────────────────────────────────────────────────
cache = {"vix": None, "funding": None, "arb": None, "updated": None}
lock = threading.Lock()

# ═══════════════════════════════════════════════════════════════════
# DATA FETCHERS (adapted from standalone scripts)
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
        top_long = [fmt(r) for r in deduped[:10]]  # high = short opp
        top_short = [fmt(r) for r in deduped[-10:][::-1]]  # negative = long opp
        avg = sum(r["rate"] for r in deduped) / len(deduped) if deduped else 0
        return {"top_positive": top_long, "top_negative": top_short,
                "total": len(deduped), "sources": sources,
                "avg_rate": round(avg*100, 4), "avg_ann": round(avg*3*365*100, 1)}
    except Exception as e:
        return {"error": str(e)}


# ─── Background refresh ─────────────────────────────────────────────
def refresh():
    while True:
        vix = fetch_vix()
        funding = fetch_funding()
        arb = fetch_arb()
        with lock:
            cache["vix"] = vix
            cache["funding"] = funding
            cache["arb"] = arb
            cache["updated"] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        time.sleep(300)

threading.Thread(target=refresh, daemon=True).start()

# ═══════════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════════

@app.route("/api/data")
def api_data():
    with lock:
        return jsonify(cache)

@app.route("/")
def index():
    return render_template_string(HTML)

# ═══════════════════════════════════════════════════════════════════
# HTML TEMPLATE
# ═══════════════════════════════════════════════════════════════════

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>QuantLab Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0a0e17;--card:#111827;--border:#1e293b;--text:#e2e8f0;--dim:#64748b;--green:#22c55e;--yellow:#eab308;--red:#ef4444;--blue:#3b82f6;--cyan:#06b6d4}
body{background:var(--bg);color:var(--text);font-family:'SF Mono',Monaco,'Cascadia Code',monospace;font-size:13px;min-height:100vh}
.header{text-align:center;padding:24px 0 8px;border-bottom:1px solid var(--border)}
.header h1{font-size:20px;letter-spacing:4px;color:var(--cyan);font-weight:400}
.header .sub{color:var(--dim);font-size:11px;margin-top:4px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:16px;max-width:1400px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:16px;overflow:hidden}
.card.full{grid-column:1/-1}
.card h2{font-size:13px;color:var(--dim);text-transform:uppercase;letter-spacing:2px;margin-bottom:12px;display:flex;align-items:center;gap:8px}
.card h2 span{font-size:16px}
.signal{font-size:28px;font-weight:700;margin:8px 0}
.signal.green{color:var(--green)}.signal.yellow{color:var(--yellow)}.signal.red{color:var(--red)}
.note{color:var(--dim);font-size:11px;margin-bottom:12px}
.stat-row{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid #1a1f2e}
.stat-row .label{color:var(--dim)}.stat-row .val{color:var(--text)}
table{width:100%;border-collapse:collapse}
th{text-align:left;color:var(--dim);font-size:11px;padding:6px 8px;border-bottom:1px solid var(--border);font-weight:400;text-transform:uppercase;letter-spacing:1px}
td{padding:6px 8px;border-bottom:1px solid #1a1f2e;font-size:12px}
tr:hover{background:#ffffff06}
.pos{color:var(--green)}.neg{color:var(--red)}
.badge{display:inline-block;padding:2px 6px;border-radius:4px;font-size:10px;background:#ffffff10;color:var(--dim)}
.alert{color:var(--yellow);font-weight:700}
.empty{color:var(--dim);text-align:center;padding:24px;font-style:italic}
.loading{color:var(--dim);text-align:center;padding:40px}
.footer{text-align:center;padding:12px;color:var(--dim);font-size:10px;border-top:1px solid var(--border)}
.tag{display:inline-block;padding:1px 5px;border-radius:3px;font-size:10px;margin-left:4px}
.tag.contango{background:#22c55e20;color:var(--green)}.tag.backwardation{background:#ef444420;color:var(--red)}
@media(max-width:768px){.grid{grid-template-columns:1fr}.card.full{grid-column:1}}
</style>
</head>
<body>
<div class="header">
  <h1>▲ QUANTLAB</h1>
  <div class="sub">TRADING DASHBOARD &mdash; <span id="clock"></span> &mdash; Next refresh: <span id="countdown">300</span>s</div>
</div>
<div class="grid">
  <div class="card" id="vix-card"><div class="loading">Loading VIX data…</div></div>
  <div class="card" id="structure-card"><div class="loading">Loading term structure…</div></div>
  <div class="card full" id="funding-card"><div class="loading">Loading funding rates…</div></div>
  <div class="card full" id="arb-card"><div class="loading">Loading arb scanner…</div></div>
</div>
<div class="footer">Data refreshes every 5 minutes • Sources: Yahoo Finance, Binance, Bybit, Gate.io, Polymarket, Kalshi</div>

<script>
let countdown = 300;
const $=id=>document.getElementById(id);
function clock(){$('clock').textContent=new Date().toLocaleString('en-US',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false})+' ET'}
setInterval(clock,1000);clock();
setInterval(()=>{countdown--;if(countdown<=0)countdown=300;$('countdown').textContent=countdown},1000);

function render(d){
  // VIX
  const v=d.vix;
  if(v&&!v.error){
    $('vix-card').innerHTML=`<h2><span>📊</span> VIX MONITOR</h2>
      <div class="signal ${v.color}">${v.current} — ${v.signal}</div>
      <div class="note">${v.note}</div>
      <div class="stat-row"><span class="label">7-Day Avg</span><span class="val">${v.avg_7}</span></div>
      <div class="stat-row"><span class="label">30-Day Avg</span><span class="val">${v.avg_30}</span></div>
      <div class="stat-row"><span class="label">90-Day Avg</span><span class="val">${v.avg_90}</span></div>
      <div class="stat-row"><span class="label">90-Day Range</span><span class="val">${v.low_90} — ${v.high_90}</span></div>`;
    const s=v.structure;
    if(s){
      $('structure-card').innerHTML=`<h2><span>📐</span> TERM STRUCTURE</h2>
        <div class="signal" style="font-size:22px">${s.kind} <span class="tag ${s.kind.toLowerCase()}">${s.ratio}</span></div>
        <div class="note">${s.kind==='CONTANGO'?'Normal — no panic':'⚠️ Fear elevated'}</div>
        <div class="stat-row"><span class="label">VIX</span><span class="val">${v.current}</span></div>
        <div class="stat-row"><span class="label">VIX3M</span><span class="val">${s.vix3m}</span></div>
        <div class="stat-row"><span class="label">Ratio</span><span class="val">${s.ratio}</span></div>`;
    } else { $('structure-card').innerHTML='<h2><span>📐</span> TERM STRUCTURE</h2><div class="empty">VIX3M unavailable</div>'; }
  } else {
    $('vix-card').innerHTML=`<h2><span>📊</span> VIX MONITOR</h2><div class="empty">${v?v.error:'Loading…'}</div>`;
    $('structure-card').innerHTML='<h2><span>📐</span> TERM STRUCTURE</h2><div class="empty">—</div>';
  }
  // Funding
  const f=d.funding;
  if(f&&!f.error&&f.top_positive){
    let rows=r=>r.map(x=>`<tr><td>${x.symbol}</td><td class="${x.rate_8h>=0?'pos':'neg'}">${x.rate_8h}%</td><td class="${x.annualized>=0?'pos':'neg'}">${x.annualized}%</td><td><span class="badge">${x.source}</span></td></tr>`).join('');
    $('funding-card').innerHTML=`<h2><span>💰</span> FUNDING RATES <span class="badge">${f.total} pairs • ${f.sources.join(', ')}</span></h2>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
        <div><h2 style="font-size:11px;color:var(--green)">📈 HIGHEST (Short Opps)</h2>
          <table><tr><th>Symbol</th><th>Rate/8h</th><th>Ann.</th><th>Src</th></tr>${rows(f.top_positive)}</table></div>
        <div><h2 style="font-size:11px;color:var(--red)">📉 MOST NEGATIVE (Long Opps)</h2>
          <table><tr><th>Symbol</th><th>Rate/8h</th><th>Ann.</th><th>Src</th></tr>${rows(f.top_negative)}</table></div>
      </div>
      <div style="margin-top:8px;color:var(--dim);font-size:11px">Market avg: ${f.avg_rate}%/8h (${f.avg_ann}% ann)</div>`;
  } else {
    $('funding-card').innerHTML=`<h2><span>💰</span> FUNDING RATES</h2><div class="empty">${f?f.error||'No data':'Loading…'}</div>`;
  }
  // Arb
  const a=d.arb;
  if(a&&!a.error){
    if(a.opps&&a.opps.length){
      let r=a.opps.map(o=>`<tr><td>${o.poly_title.substring(0,50)}${o.poly_title.length>50?'…':''}</td><td>${o.poly_yes}¢</td><td>${o.kalshi_yes}¢</td><td class="alert">${o.spread}%</td><td>${o.match}%</td></tr>`).join('');
      $('arb-card').innerHTML=`<h2><span>🔍</span> ARB SCANNER <span class="badge">${a.poly_count} Poly • ${a.kalshi_count} Kalshi</span></h2>
        <table><tr><th>Market</th><th>Poly</th><th>Kalshi</th><th>Spread</th><th>Match</th></tr>${r}</table>`;
    } else {
      $('arb-card').innerHTML=`<h2><span>🔍</span> ARB SCANNER <span class="badge">${a.poly_count} Poly • ${a.kalshi_count} Kalshi</span></h2>
        <div class="empty">No opportunities with spread &gt; 3% found</div>`;
    }
  } else {
    $('arb-card').innerHTML=`<h2><span>🔍</span> ARB SCANNER</h2><div class="empty">${a?a.error||'No data':'Loading…'}</div>`;
  }
}

async function load(){
  try{const r=await fetch('/api/data');const d=await r.json();render(d);countdown=300}catch(e){console.error(e)}
}
load();
setInterval(load,300000);
</script>
</body>
</html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
