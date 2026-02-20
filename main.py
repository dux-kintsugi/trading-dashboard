#!/usr/bin/env python3
"""Kitebird Capital — Unified Dashboard (Trading + Portfolio + News & Signals + Ops + Org)."""

from flask import Flask, jsonify, render_template_string, request
import threading, time, json, requests, datetime, os, re, glob
from difflib import SequenceMatcher
from pathlib import Path

app = Flask(__name__)
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

# ─── Shared cache ───────────────────────────────────────────────────
cache = {"vix": None, "funding": None, "arb": None, "updated": None}
ops_cache = {"sessions": [], "totals": {}, "updated": None}
portfolio_cache = {"updated": None}
news_cache = {"headlines": [], "updated": None}
signals_cache = {"markets": [], "updated": None}
org_cache = {"teams": [], "updated": None}
lock = threading.Lock()

PORTFOLIO_FILE = Path("/tmp/kitebird-portfolio.json")
TEAM_VIEWS_FILE = Path("/tmp/kitebird-team-views.json")
TEAMS_DIR = Path(os.path.expanduser("~/ClawSystem/Obsidian/Agents/Teams"))
SIGNALS_BOOK_FILE = Path("/tmp/kitebird-signals-book.json")
ETF_PORTFOLIO_FILE = Path("/tmp/kitebird-etf-portfolio.json")
SCREENSHOTS_DIR = Path(os.path.expanduser("~/ClawSystem/Control/Dux/tools/dashboard/screenshots"))

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

MODEL_COSTS = {
    "claude-opus-4-6":       {"input": 15.0, "output": 75.0, "tier": "T1-Frontier"},
    "claude-sonnet-4-6":     {"input": 3.0,  "output": 15.0, "tier": "T2-Workhorse"},
    "gemini-2.5-flash-lite": {"input": 0.075,"output": 0.30, "tier": "T3-Light"},
    "deepseek-r1":           {"input": 0.55, "output": 2.19, "tier": "T4-DeepThink"},
}

TIER_POLICY = {
    "main":      {"expected": "T1-Frontier",   "model": "claude-opus-4-6"},
    "subagent":  {"expected": "T2-Workhorse",  "model": "claude-sonnet-4-6"},
    "heartbeat": {"expected": "T3-Light",       "model": "gemini-2.5-flash-lite"},
    "research":  {"expected": "T4-DeepThink",   "model": "deepseek-r1"},
}

COST_LOG = Path(os.environ.get("COST_LOG", "/tmp/kitebird-cost-log.jsonl"))

def load_cost_log():
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
    entries = load_cost_log()
    now = datetime.datetime.utcnow()
    today = now.strftime("%Y-%m-%d")
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
                "name": e.get("name", "Unknown"), "type": e.get("type", "subagent"),
                "model": model, "tier": tier_info["tier"][:2] if tier_info else "??",
                "tokens_in": tin, "tokens_out": tout,
                "cost_actual": round(actual, 2), "cost_opus": round(opus, 2),
                "savings": round(opus - actual, 2), "mfs": e.get("mfs", 4),
                "status": "✅", "time": e.get("time", "--:--"),
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
            "actual": round(total_actual, 2), "opus_baseline": round(total_opus, 2),
            "savings": round(total_savings, 2), "pct_savings": round(pct_savings, 1),
            "avg_mfs": round(avg_mfs, 1), "underpowered_alerts": underpowered,
            "overpowered_alerts": overpowered, "session_count": len(sessions),
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


# ═══════════════════════════════════════════════════════════════════
# DATA — PORTFOLIO (Paper Trading)
# ═══════════════════════════════════════════════════════════════════

def _default_portfolio():
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    return {
        "starting_capital": 10000,
        "strategies": [
            {"name": "SPX Put Spreads", "allocation_pct": 40, "allocated": 4000},
            {"name": "Crypto Funding", "allocation_pct": 45, "allocated": 4500},
            {"name": "Forex Carry", "allocation_pct": 15, "allocated": 1500},
        ],
        "positions": [
            {"strategy": "SPX Put Spreads", "entry_date": now, "entry_price": 4000.00, "current_value": 4000.00, "pnl": 0, "status": "open"},
            {"strategy": "Crypto Funding", "entry_date": now, "entry_price": 4500.00, "current_value": 4500.00, "pnl": 0, "status": "open"},
            {"strategy": "Forex Carry", "entry_date": now, "entry_price": 1500.00, "current_value": 1500.00, "pnl": 0, "status": "open"},
        ],
        "trades": [],
        "created": now,
    }

def load_portfolio():
    if not PORTFOLIO_FILE.exists():
        data = _default_portfolio()
        PORTFOLIO_FILE.write_text(json.dumps(data, indent=2))
        return data
    try:
        return json.loads(PORTFOLIO_FILE.read_text())
    except:
        data = _default_portfolio()
        PORTFOLIO_FILE.write_text(json.dumps(data, indent=2))
        return data

def save_portfolio(data):
    PORTFOLIO_FILE.write_text(json.dumps(data, indent=2))

def compute_portfolio():
    data = load_portfolio()
    total_value = sum(p["current_value"] for p in data["positions"])
    total_pnl = total_value - data["starting_capital"]
    return {
        "starting_capital": data["starting_capital"],
        "total_value": round(total_value, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl / data["starting_capital"] * 100, 2) if data["starting_capital"] else 0,
        "strategies": data["strategies"],
        "positions": data["positions"],
        "trades": data.get("trades", [])[-50:],  # last 50
        "updated": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
    }


# ═══════════════════════════════════════════════════════════════════
# DATA — ETF MIRROR PORTFOLIO
# ═══════════════════════════════════════════════════════════════════

def _default_etf_portfolio():
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    return {
        "starting_capital": 10000,
        "portfolio_name": "ETF Mirror Portfolio",
        "description": "Mirrors main portfolio using ETFs and ETF options.",
        "strategies": [
            {"name": "SPY Options", "allocation_pct": 40, "allocated": 4000, "mirrors": "SPX Put Spreads"},
            {"name": "Crypto ETF", "allocation_pct": 45, "allocated": 4500, "mirrors": "Crypto Funding"},
            {"name": "Currency ETF", "allocation_pct": 15, "allocated": 1500, "mirrors": "Forex Carry"},
        ],
        "positions": [],
        "trades": [],
        "created": now,
    }

def load_etf_portfolio():
    if not ETF_PORTFOLIO_FILE.exists():
        data = _default_etf_portfolio()
        ETF_PORTFOLIO_FILE.write_text(json.dumps(data, indent=2))
        return data
    try:
        return json.loads(ETF_PORTFOLIO_FILE.read_text())
    except:
        data = _default_etf_portfolio()
        ETF_PORTFOLIO_FILE.write_text(json.dumps(data, indent=2))
        return data

def save_etf_portfolio(data):
    ETF_PORTFOLIO_FILE.write_text(json.dumps(data, indent=2))

def compute_etf_portfolio():
    data = load_etf_portfolio()
    total_value = sum(p.get("current_value", 0) for p in data.get("positions", []))
    if not total_value and data.get("positions"):
        total_value = data["starting_capital"]
    total_pnl = total_value - data["starting_capital"]
    return {
        "portfolio_name": data.get("portfolio_name", "ETF Mirror Portfolio"),
        "description": data.get("description", ""),
        "starting_capital": data["starting_capital"],
        "total_value": round(total_value, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl / data["starting_capital"] * 100, 2) if data["starting_capital"] else 0,
        "strategies": data.get("strategies", []),
        "positions": data.get("positions", []),
        "trades": data.get("trades", [])[-50:],
        "updated": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
    }

# ═══════════════════════════════════════════════════════════════════
# DATA — NEWS & SIGNALS
# ═══════════════════════════════════════════════════════════════════

def fetch_news():
    """Fetch financial news from Yahoo Finance RSS."""
    headlines = []
    url = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=^GSPC&region=US&lang=en-US"
    try:
        try:
            import feedparser
            feed = feedparser.parse(url)
            for entry in feed.entries[:20]:
                pub = ""
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    pub = time.strftime("%Y-%m-%d %H:%M", entry.published_parsed)
                headlines.append({
                    "title": entry.get("title", ""),
                    "link": entry.get("link", ""),
                    "published": pub,
                    "source": "Yahoo Finance",
                })
        except ImportError:
            # Fallback: raw XML parsing
            import xml.etree.ElementTree as ET
            r = requests.get(url, timeout=10, headers=HEADERS)
            if r.status_code == 200:
                root = ET.fromstring(r.content)
                for item in root.iter("item"):
                    title = item.findtext("title", "")
                    link = item.findtext("link", "")
                    pub = item.findtext("pubDate", "")
                    headlines.append({
                        "title": title,
                        "link": link,
                        "published": pub[:16] if pub else "",
                        "source": "Yahoo Finance",
                    })
                headlines = headlines[:20]
    except Exception as e:
        headlines = [{"title": f"Error fetching news: {e}", "link": "", "published": "", "source": "error"}]
    return headlines

def fetch_signals():
    """Fetch polymarket data and compare with team views."""
    poly = _fetch_polymarket()
    team_views = load_team_views()
    signals = []
    for m in poly[:30]:
        market_pct = round(m["yes"] * 100, 1)
        team_entry = team_views.get(m["raw_title"], {})
        team_pct = team_entry.get("estimate")
        spread = None
        dislocation = False
        if team_pct is not None:
            spread = round(team_pct - market_pct, 1)
            dislocation = abs(spread) > 10
        signals.append({
            "title": m["raw_title"],
            "market_pct": market_pct,
            "team_pct": team_pct,
            "spread": spread,
            "dislocation": dislocation,
            "source": m["source"],
        })
    # Sort: dislocations first
    signals.sort(key=lambda x: (not x["dislocation"], -(abs(x["spread"]) if x["spread"] is not None else 0)))
    return signals

def load_team_views():
    if not TEAM_VIEWS_FILE.exists():
        return {}
    try:
        return json.loads(TEAM_VIEWS_FILE.read_text())
    except:
        return {}

def save_team_view(title, estimate):
    views = load_team_views()
    views[title] = {"estimate": estimate, "updated": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M")}
    TEAM_VIEWS_FILE.write_text(json.dumps(views, indent=2))


# ═══════════════════════════════════════════════════════════════════
# DATA — SIGNALS BOOK (Paper Trading for Quick Trades)
# ═══════════════════════════════════════════════════════════════════

def _default_signals_book():
    return {
        "starting_capital": 10000,
        "trades": [],
        "created": datetime.datetime.utcnow().strftime("%Y-%m-%d"),
    }

def load_signals_book():
    if not SIGNALS_BOOK_FILE.exists():
        data = _default_signals_book()
        SIGNALS_BOOK_FILE.write_text(json.dumps(data, indent=2))
        return data
    try:
        return json.loads(SIGNALS_BOOK_FILE.read_text())
    except:
        data = _default_signals_book()
        SIGNALS_BOOK_FILE.write_text(json.dumps(data, indent=2))
        return data

def save_signals_book(data):
    SIGNALS_BOOK_FILE.write_text(json.dumps(data, indent=2))

def compute_signals_portfolio():
    data = load_signals_book()
    trades = data.get("trades", [])
    open_trades = [t for t in trades if t.get("status") == "open"]
    closed_trades = [t for t in trades if t.get("status") == "closed"]
    realized_pnl = sum(t.get("pnl", 0) for t in closed_trades)
    capital_in_use = sum(t.get("size", 0) * t.get("entry_price", 0) for t in open_trades)
    wins = sum(1 for t in closed_trades if t.get("pnl", 0) > 0)
    win_rate = round(wins / len(closed_trades) * 100, 1) if closed_trades else 0
    return {
        "starting_capital": data["starting_capital"],
        "realized_pnl": round(realized_pnl, 2),
        "capital_in_use": round(capital_in_use, 2),
        "available_capital": round(data["starting_capital"] + realized_pnl - capital_in_use, 2),
        "open_count": len(open_trades),
        "closed_count": len(closed_trades),
        "win_rate": win_rate,
        "open_trades": open_trades,
        "closed_trades": closed_trades[-50:],
        "updated": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
    }


# ═══════════════════════════════════════════════════════════════════
# DATA — ORG OVERVIEW
# ═══════════════════════════════════════════════════════════════════

def parse_team_file(filepath):
    """Parse a team markdown file for tasks and tables."""
    name = filepath.stem
    try:
        text = filepath.read_text()
    except:
        return {"name": name, "tasks": [], "tables": [], "error": "Could not read file"}

    tasks = []
    # Parse checkbox tasks
    for line in text.split("\n"):
        line_stripped = line.strip()
        if line_stripped.startswith("- [x]"):
            tasks.append({"text": line_stripped[6:].strip()[:80], "done": True})
        elif line_stripped.startswith("- [ ]"):
            tasks.append({"text": line_stripped[6:].strip()[:80], "done": False})

    # Parse markdown tables
    tables = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("|") and line.endswith("|"):
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                row = lines[i].strip()
                cells = [c.strip() for c in row.split("|")[1:-1]]
                if cells and not all(set(c) <= set("- :") for c in cells):  # skip separator rows
                    table_lines.append(cells)
                i += 1
            if table_lines:
                tables.append({"headers": table_lines[0], "rows": table_lines[1:]})
            continue
        i += 1

    total = len(tasks)
    done = sum(1 for t in tasks if t["done"])
    return {
        "name": name,
        "total_tasks": total,
        "completed": done,
        "completion_pct": round(done / total * 100) if total else 0,
        "open_tasks": [t for t in tasks if not t["done"]][:10],
        "tables": tables[:5],
    }

def compute_org():
    team_files = ["Trading.md", "Operations.md", "Business.md", "KintsugiFund.md", "Efficiency.md"]
    teams = []
    for fname in team_files:
        fp = TEAMS_DIR / fname
        if fp.exists():
            teams.append(parse_team_file(fp))
        else:
            teams.append({"name": fname.replace(".md", ""), "total_tasks": 0, "completed": 0,
                          "completion_pct": 0, "open_tasks": [], "tables": [], "error": "File not found"})
    return {
        "teams": teams,
        "updated": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
    }


# ─── Background refresh ─────────────────────────────────────────────
def refresh():
    while True:
        vix = fetch_vix()
        funding = fetch_funding()
        arb = fetch_arb()
        ops = compute_ops_data()
        news = fetch_news()
        sigs = fetch_signals()
        org = compute_org()
        with lock:
            cache["vix"] = vix
            cache["funding"] = funding
            cache["arb"] = arb
            cache["updated"] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            ops_cache.update(ops)
            news_cache["headlines"] = news
            news_cache["updated"] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            signals_cache["markets"] = sigs
            signals_cache["updated"] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            org_cache.update(org)
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
    entry = request.get_json()
    if not entry:
        return jsonify({"error": "no data"}), 400
    entry["date"] = entry.get("date", datetime.datetime.utcnow().strftime("%Y-%m-%d"))
    entry["time"] = entry.get("time", datetime.datetime.utcnow().strftime("%H:%M"))
    with open(COST_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return jsonify({"ok": True})

@app.route("/api/portfolio")
def api_portfolio():
    return jsonify(compute_portfolio())

@app.route("/api/portfolio/trade", methods=["POST"])
def api_portfolio_trade():
    trade = request.get_json()
    if not trade:
        return jsonify({"error": "no data"}), 400
    data = load_portfolio()
    trade["timestamp"] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    data.setdefault("trades", []).append(trade)
    # Update positions if provided
    if "strategy" in trade and "new_value" in trade:
        for p in data["positions"]:
            if p["strategy"] == trade["strategy"]:
                old = p["current_value"]
                p["current_value"] = float(trade["new_value"])
                p["pnl"] = round(p["current_value"] - p["entry_price"], 2)
                p["status"] = trade.get("status", p["status"])
    save_portfolio(data)
    return jsonify({"ok": True})

@app.route("/api/news")
def api_news():
    with lock:
        return jsonify(news_cache)

@app.route("/api/signals")
def api_signals():
    with lock:
        return jsonify(signals_cache)

@app.route("/api/signals/view", methods=["POST"])
def api_signals_view():
    body = request.get_json()
    if not body or "title" not in body or "estimate" not in body:
        return jsonify({"error": "need title and estimate"}), 400
    save_team_view(body["title"], float(body["estimate"]))
    return jsonify({"ok": True})

@app.route("/api/signals/portfolio")
def api_signals_portfolio():
    return jsonify(compute_signals_portfolio())

@app.route("/api/signals/trade", methods=["POST"])
def api_signals_trade():
    body = request.get_json()
    if not body or "market" not in body:
        return jsonify({"error": "need at least 'market'"}), 400
    data = load_signals_book()
    trade = {
        "id": len(data.get("trades", [])) + 1,
        "timestamp": body.get("timestamp", datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M")),
        "market": body["market"],
        "direction": body.get("direction", "long"),
        "size": float(body.get("size", 0)),
        "entry_price": float(body.get("entry_price", 0)),
        "rationale": body.get("rationale", ""),
        "screenshot_path": body.get("screenshot_path", ""),
        "exit_timestamp": None,
        "exit_price": None,
        "exit_screenshot_path": None,
        "pnl": None,
        "status": "open",
    }
    data.setdefault("trades", []).append(trade)
    save_signals_book(data)
    return jsonify({"ok": True, "trade_id": trade["id"]})

@app.route("/api/signals/trade/exit", methods=["POST"])
def api_signals_trade_exit():
    body = request.get_json()
    if not body or "trade_id" not in body or "exit_price" not in body:
        return jsonify({"error": "need trade_id and exit_price"}), 400
    data = load_signals_book()
    trade_id = int(body["trade_id"])
    for t in data.get("trades", []):
        if t.get("id") == trade_id and t.get("status") == "open":
            t["exit_timestamp"] = body.get("exit_timestamp", datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M"))
            t["exit_price"] = float(body["exit_price"])
            t["exit_screenshot_path"] = body.get("screenshot_path", "")
            multiplier = 1 if t["direction"] == "long" else -1
            t["pnl"] = round((t["exit_price"] - t["entry_price"]) * t["size"] * multiplier, 2)
            t["status"] = "closed"
            save_signals_book(data)
            return jsonify({"ok": True, "pnl": t["pnl"]})
    return jsonify({"error": "trade not found or already closed"}), 404

@app.route("/api/signals/screenshot", methods=["POST"])
def api_signals_screenshot():
    import base64 as b64mod
    body = request.get_json()
    if not body or "image" not in body:
        return jsonify({"error": "need base64 'image'"}), 400
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    fname = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S") + ".png"
    fpath = SCREENSHOTS_DIR / fname
    fpath.write_bytes(b64mod.b64decode(body["image"]))
    return jsonify({"ok": True, "filename": fname, "path": str(fpath)})

@app.route("/api/signals/screenshot/<filename>")
def api_signals_screenshot_serve(filename):
    from flask import send_from_directory
    return send_from_directory(str(SCREENSHOTS_DIR), filename)

@app.route("/api/etf")
def api_etf():
    return jsonify(compute_etf_portfolio())

@app.route("/api/etf/trade", methods=["POST"])
def api_etf_trade():
    trade = request.get_json()
    if not trade:
        return jsonify({"error": "no data"}), 400
    data = load_etf_portfolio()
    trade["timestamp"] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    data.setdefault("trades", []).append(trade)
    if "position_id" in trade and "new_value" in trade:
        for p in data.get("positions", []):
            if p.get("id") == trade["position_id"]:
                p["current_value"] = float(trade["new_value"])
                p["pnl"] = round(p["current_value"] - p.get("cost_basis", 0), 2)
                p["status"] = trade.get("status", p.get("status", "open"))
    save_etf_portfolio(data)
    return jsonify({"ok": True})

@app.route("/api/org")
def api_org():
    with lock:
        return jsonify(org_cache)

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
:root{--bg:#0a0e17;--card:#111827;--border:#1e293b;--text:#e2e8f0;--dim:#64748b;--green:#22c55e;--yellow:#eab308;--red:#ef4444;--blue:#3b82f6;--cyan:#06b6d4;--purple:#a855f7;--orange:#f97316}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:14px;min-height:100vh;min-height:100dvh;overflow-x:hidden}

/* ─── NAV ─── */
.topnav{position:sticky;top:0;z-index:100;background:#0d1117;border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 16px;height:52px;gap:8px;-webkit-overflow-scrolling:touch;overflow-x:auto}
.topnav .logo{font-size:15px;font-weight:700;color:var(--cyan);white-space:nowrap;margin-right:12px;letter-spacing:1px;flex-shrink:0}
.topnav .logo span{color:var(--dim);font-weight:400;font-size:12px}
.nav-btn{background:none;border:1px solid transparent;color:var(--dim);padding:8px 14px;border-radius:6px;cursor:pointer;font-size:13px;font-weight:500;white-space:nowrap;transition:all .15s;flex-shrink:0}
.nav-btn:hover{color:var(--text);background:#ffffff08}
.nav-btn.active{color:var(--cyan);border-color:var(--cyan);background:#06b6d410}
.nav-right{margin-left:auto;display:flex;align-items:center;gap:8px;flex-shrink:0}
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

/* ─── BIG STATS ─── */
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

/* ─── PIE CHART (CSS only) ─── */
.pie-container{display:flex;align-items:center;gap:20px;margin:12px 0}
.pie{width:120px;height:120px;border-radius:50%;position:relative;flex-shrink:0}
.pie-legend{display:flex;flex-direction:column;gap:6px}
.pie-legend-item{display:flex;align-items:center;gap:8px;font-size:13px}
.pie-legend-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}

/* ─── DISLOCATION ALERT ─── */
.dislocation{background:#ef444418;border:1px solid #ef444440;border-radius:8px;padding:10px 14px;margin-bottom:8px}
.dislocation .dis-title{font-weight:600;color:var(--red);font-size:13px}
.dislocation .dis-detail{font-size:12px;color:var(--dim);margin-top:4px}

/* ─── NEWS ITEM ─── */
.news-item{padding:10px 0;border-bottom:1px solid #1a1f2e}
.news-item:last-child{border-bottom:none}
.news-item .news-title{font-size:13px;color:var(--text);text-decoration:none;font-weight:500}
.news-item .news-title:hover{color:var(--cyan)}
.news-item .news-meta{font-size:11px;color:var(--dim);margin-top:3px}

/* ─── TEAM VIEW INPUT ─── */
.team-input{display:flex;gap:6px;align-items:center;margin-top:4px}
.team-input input{background:#0d1117;border:1px solid var(--border);color:var(--text);padding:4px 8px;border-radius:4px;width:60px;font-size:12px}
.team-input button{background:var(--cyan);color:#000;border:none;padding:4px 10px;border-radius:4px;font-size:11px;cursor:pointer;font-weight:600}
.team-input button:hover{opacity:0.85}

/* ─── ORG PROGRESS BAR ─── */
.progress-bar{background:#1e293b;border-radius:4px;height:8px;overflow:hidden;margin-top:4px}
.progress-bar .fill{height:100%;border-radius:4px;transition:width .3s}

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
  .pie{width:90px;height:90px}
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
  <button class="nav-btn active" onclick="showPage('trading',this)">📊 Trading</button>
  <button class="nav-btn" onclick="showPage('portfolio',this)">📈 Portfolio</button>
  <button class="nav-btn" onclick="showPage('news',this)">📰 News & Signals</button>
  <button class="nav-btn" onclick="showPage('etf',this)">🏦 ETF Mirror</button>
  <button class="nav-btn" onclick="showPage('ops',this)">⚙️ Ops & Costs</button>
  <button class="nav-btn" onclick="showPage('org',this)">🏢 Org Overview</button>
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

<!-- ═══ PORTFOLIO PAGE ═══ -->
<div class="page" id="page-portfolio">
  <div id="portfolio-content"><div class="loading">Loading portfolio…</div></div>
  <div class="footer">Paper trading portfolio • $10K starting capital • Auto-refresh every 60s</div>
</div>

<!-- ═══ NEWS & SIGNALS PAGE ═══ -->
<div class="page" id="page-news">
  <div id="news-content"><div class="loading">Loading news & signals…</div></div>
  <div class="footer">Sources: Yahoo Finance RSS, Polymarket • Team views stored locally</div>
</div>

<!-- ═══ ETF MIRROR PAGE ═══ -->
<div class="page" id="page-etf">
  <div id="etf-content"><div class="loading">Loading ETF mirror portfolio…</div></div>
  <div class="footer">ETF Mirror Portfolio • Mirrors main trades via ETFs/options • $10K paper capital</div>
</div>

<!-- ═══ OPS PAGE ═══ -->
<div class="page" id="page-ops">
  <div id="ops-content"><div class="loading">Loading ops data…</div></div>
  <div class="footer">Model tiering active since Feb 20, 2026 • Baseline: All-Opus ($75/1M output tokens)</div>
</div>

<!-- ═══ ORG PAGE ═══ -->
<div class="page" id="page-org">
  <div id="org-content"><div class="loading">Loading org overview…</div></div>
  <div class="footer">Data from ~/ClawSystem/Obsidian/Agents/Teams/ • Auto-refresh every 5 min</div>
</div>

<script>
// ─── Navigation ──────────────────────────────────────────────
function showPage(name, btn) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('page-'+name).classList.add('active');
  if(btn) btn.classList.add('active');
  if (name === 'ops') loadOps();
  if (name === 'trading') loadTrading();
  if (name === 'portfolio') loadPortfolio();
  if (name === 'news') loadNews();
  if (name === 'etf') loadETF();
  if (name === 'org') loadOrg();
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

function pnlClass(v) { return v >= 0 ? 'pos' : 'neg'; }
function pnlSign(v) { return v >= 0 ? '+$'+v.toFixed(2) : '-$'+Math.abs(v).toFixed(2); }

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

// ═══ PORTFOLIO RENDER ════════════════════════════════════════
function renderPortfolio(d) {
  let html = '';

  // Big stats
  html += '<div class="big-stats">';
  html += '<div class="big-stat neutral"><div class="num">$'+d.total_value.toLocaleString()+'</div><div class="lbl">Total Value</div></div>';
  html += '<div class="big-stat '+(d.total_pnl>=0?'savings':'alert-red')+'"><div class="num">'+pnlSign(d.total_pnl)+'</div><div class="lbl">Total P&L</div></div>';
  html += '<div class="big-stat neutral"><div class="num">'+d.total_pnl_pct+'%</div><div class="lbl">Return</div></div>';
  html += '<div class="big-stat neutral"><div class="num">$'+d.starting_capital.toLocaleString()+'</div><div class="lbl">Starting Capital</div></div>';
  html += '</div>';

  // Allocation pie + strategies
  html += '<div class="grid">';

  // Pie card
  const colors = ['#3b82f6','#22c55e','#eab308'];
  let conic = '';
  let pct = 0;
  d.strategies.forEach(function(s,i){
    const end = pct + s.allocation_pct;
    conic += (i>0?',':'') + colors[i]+' '+pct+'% '+end+'%';
    pct = end;
  });
  html += '<div class="card"><h2><span class="icon">🥧</span> ALLOCATION</h2>';
  html += '<div class="pie-container">';
  html += '<div class="pie" style="background:conic-gradient('+conic+')"></div>';
  html += '<div class="pie-legend">';
  d.strategies.forEach(function(s,i){
    html += '<div class="pie-legend-item"><div class="pie-legend-dot" style="background:'+colors[i]+'"></div>'+s.name+' ('+s.allocation_pct+'% / $'+s.allocated.toLocaleString()+')</div>';
  });
  html += '</div></div></div>';

  // Summary card
  html += '<div class="card"><h2><span class="icon">📊</span> SUMMARY</h2>';
  html += '<div class="stat-row"><span class="label">Starting Capital</span><span class="val">$'+d.starting_capital.toLocaleString()+'</span></div>';
  html += '<div class="stat-row"><span class="label">Current Value</span><span class="val">$'+d.total_value.toLocaleString()+'</span></div>';
  html += '<div class="stat-row"><span class="label">Total P&L</span><span class="val '+pnlClass(d.total_pnl)+'">'+pnlSign(d.total_pnl)+' ('+d.total_pnl_pct+'%)</span></div>';
  html += '<div class="stat-row"><span class="label">Strategies</span><span class="val">'+d.strategies.length+'</span></div>';
  html += '<div class="stat-row"><span class="label">Open Positions</span><span class="val">'+d.positions.filter(function(p){return p.status==='open'}).length+'</span></div>';
  html += '</div>';
  html += '</div>';

  // Positions table
  html += '<div class="card full" style="margin-top:14px"><h2><span class="icon">📋</span> POSITIONS</h2>';
  if(d.positions.length){
    html += '<div style="overflow-x:auto"><table>';
    html += '<tr><th>Strategy</th><th>Entry Date</th><th>Entry Price</th><th>Current Value</th><th>P&L</th><th>Status</th></tr>';
    d.positions.forEach(function(p){
      const pnl = p.pnl || (p.current_value - p.entry_price);
      html += '<tr><td>'+p.strategy+'</td><td>'+p.entry_date+'</td><td>$'+p.entry_price.toFixed(2)+'</td>';
      html += '<td>$'+p.current_value.toFixed(2)+'</td>';
      html += '<td class="'+pnlClass(pnl)+'">'+pnlSign(pnl)+'</td>';
      html += '<td><span class="badge">'+(p.status||'open')+'</span></td></tr>';
    });
    html += '</table></div>';
  } else {
    html += '<div class="empty">No positions yet</div>';
  }
  html += '</div>';

  // Trade log
  html += '<div class="card full" style="margin-top:14px"><h2><span class="icon">📝</span> TRADE LOG</h2>';
  if(d.trades&&d.trades.length){
    html += '<div style="overflow-x:auto"><table>';
    html += '<tr><th>Time</th><th>Strategy</th><th>Action</th><th>Amount</th><th>Notes</th></tr>';
    d.trades.slice().reverse().forEach(function(t){
      html += '<tr><td>'+(t.timestamp||'—')+'</td><td>'+(t.strategy||'—')+'</td>';
      html += '<td>'+(t.action||'—')+'</td><td>'+(t.amount?'$'+parseFloat(t.amount).toFixed(2):'—')+'</td>';
      html += '<td style="color:var(--dim)">'+(t.notes||'')+'</td></tr>';
    });
    html += '</table></div>';
  } else {
    html += '<div class="empty">No trades logged yet. Use POST /api/portfolio/trade to log trades.</div>';
  }
  html += '</div>';

  $('portfolio-content').innerHTML = html;
}

// ═══ NEWS & SIGNALS RENDER ═══════════════════════════════════
function renderNews(newsData, sigData, sbData) {
  let html = '';

  // ─── Signals Book Summary ───
  if(sbData){
    html += '<div class="big-stats">';
    html += '<div class="big-stat neutral"><div class="num">$'+sbData.starting_capital.toLocaleString()+'</div><div class="lbl">Signals Capital</div></div>';
    html += '<div class="big-stat '+(sbData.realized_pnl>=0?'savings':'alert-red')+'"><div class="num">'+(sbData.realized_pnl>=0?'+$':'-$')+Math.abs(sbData.realized_pnl).toFixed(2)+'</div><div class="lbl">Realized P&L</div></div>';
    html += '<div class="big-stat neutral"><div class="num">'+sbData.win_rate+'%</div><div class="lbl">Win Rate</div></div>';
    html += '<div class="big-stat neutral"><div class="num">'+sbData.open_count+'</div><div class="lbl">Open Positions</div></div>';
    html += '</div>';

    // Open positions table
    if(sbData.open_trades&&sbData.open_trades.length){
      html += '<div class="card full" style="margin-bottom:14px"><h2><span class="icon">📗</span> SIGNALS BOOK — OPEN POSITIONS</h2>';
      html += '<div style="overflow-x:auto"><table>';
      html += '<tr><th>ID</th><th>Market</th><th>Dir</th><th>Size</th><th>Entry</th><th>Rationale</th><th>📷</th></tr>';
      sbData.open_trades.forEach(function(t){
        const scr = t.screenshot_path ? '<a href="/api/signals/screenshot/'+t.screenshot_path.split('/').pop()+'" target="_blank">📷</a>' : '';
        html += '<tr><td>'+t.id+'</td><td>'+t.market+'</td><td><span class="badge">'+(t.direction||'long').toUpperCase()+'</span></td>';
        html += '<td>'+t.size+'</td><td>$'+parseFloat(t.entry_price).toFixed(2)+'</td>';
        html += '<td style="color:var(--dim);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+t.rationale+'</td>';
        html += '<td>'+scr+'</td></tr>';
      });
      html += '</table></div></div>';
    }

    // Closed trades
    if(sbData.closed_trades&&sbData.closed_trades.length){
      html += '<div class="card full" style="margin-bottom:14px"><h2><span class="icon">📕</span> SIGNALS BOOK — RECENT CLOSED</h2>';
      html += '<div style="overflow-x:auto"><table>';
      html += '<tr><th>ID</th><th>Market</th><th>Dir</th><th>Entry</th><th>Exit</th><th>P&L</th><th>📷</th></tr>';
      sbData.closed_trades.slice().reverse().forEach(function(t){
        const scrEntry = t.screenshot_path ? '<a href="/api/signals/screenshot/'+t.screenshot_path.split('/').pop()+'" target="_blank">📷</a>' : '';
        const scrExit = t.exit_screenshot_path ? '<a href="/api/signals/screenshot/'+t.exit_screenshot_path.split('/').pop()+'" target="_blank">📷</a>' : '';
        const scr = [scrEntry, scrExit].filter(Boolean).join(' ');
        html += '<tr><td>'+t.id+'</td><td>'+t.market+'</td><td><span class="badge">'+(t.direction||'long').toUpperCase()+'</span></td>';
        html += '<td>$'+parseFloat(t.entry_price).toFixed(2)+'</td><td>$'+parseFloat(t.exit_price).toFixed(2)+'</td>';
        html += '<td class="'+pnlClass(t.pnl)+'">'+pnlSign(t.pnl)+'</td>';
        html += '<td>'+scr+'</td></tr>';
      });
      html += '</table></div></div>';
    }
  }

  // Dislocations first
  const dislocations = (sigData.markets||[]).filter(function(s){return s.dislocation});
  if(dislocations.length){
    html += '<div class="card full" style="margin-bottom:14px"><h2><span class="icon">🚨</span> DISLOCATION ALERTS</h2>';
    dislocations.forEach(function(s){
      html += '<div class="dislocation"><div class="dis-title">⚠️ '+s.title+'</div>';
      html += '<div class="dis-detail">Market: '+s.market_pct+'% • Team: '+s.team_pct+'% • Spread: <strong style="color:var(--red)">'+Math.abs(s.spread).toFixed(1)+'%</strong>';
      html += ' — '+(s.spread>0?'Team MORE bullish':'Team LESS bullish')+'</div></div>';
    });
    html += '</div>';
  }

  html += '<div class="grid">';

  // News feed
  html += '<div class="card"><h2><span class="icon">📰</span> FINANCIAL NEWS</h2>';
  const headlines = newsData.headlines||[];
  if(headlines.length){
    headlines.forEach(function(n){
      html += '<div class="news-item">';
      if(n.link){
        html += '<a class="news-title" href="'+n.link+'" target="_blank">'+n.title+'</a>';
      } else {
        html += '<div class="news-title">'+n.title+'</div>';
      }
      html += '<div class="news-meta">'+n.source+(n.published?' • '+n.published:'')+'</div></div>';
    });
  } else {
    html += '<div class="empty">No headlines available</div>';
  }
  html += '</div>';

  // Signals / Team views
  html += '<div class="card"><h2><span class="icon">🎯</span> PREDICTION MARKETS — TEAM VIEWS</h2>';
  const markets = sigData.markets||[];
  if(markets.length){
    markets.slice(0,20).forEach(function(s){
      const isDis = s.dislocation;
      html += '<div style="padding:8px 0;border-bottom:1px solid #1a1f2e'+(isDis?';background:#ef444410;margin:0 -16px;padding-left:16px;padding-right:16px':'')+'">';
      html += '<div style="font-size:13px;font-weight:500'+(isDis?';color:var(--red)':'')+'">'+s.title.substring(0,60)+(s.title.length>60?'…':'')+'</div>';
      html += '<div style="display:flex;gap:12px;align-items:center;margin-top:4px;font-size:12px">';
      html += '<span style="color:var(--dim)">Market: <strong style="color:var(--text)">'+s.market_pct+'%</strong></span>';
      if(s.team_pct!==null){
        html += '<span style="color:var(--dim)">Team: <strong style="color:var(--cyan)">'+s.team_pct+'%</strong></span>';
        html += '<span style="color:'+(isDis?'var(--red)':'var(--dim)')+'">Spread: '+(s.spread>0?'+':'')+s.spread+'%</span>';
      }
      html += '</div>';
      html += '<div class="team-input"><input type="number" min="0" max="100" placeholder="%" id="tv-'+btoa(s.title).substring(0,12)+'"'+(s.team_pct!==null?' value="'+s.team_pct+'"':'')+'>';
      html += '<button onclick="setTeamView(\''+s.title.replace(/'/g,"\\'")+'\',\'tv-'+btoa(s.title).substring(0,12)+'\')">Set</button></div>';
      html += '</div>';
    });
  } else {
    html += '<div class="empty">No prediction market data</div>';
  }
  html += '</div>';

  html += '</div>';
  $('news-content').innerHTML = html;
}

async function setTeamView(title, inputId) {
  const val = parseFloat(document.getElementById(inputId).value);
  if(isNaN(val)||val<0||val>100){alert('Enter 0-100');return}
  try {
    await fetch('/api/signals/view',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:title,estimate:val})});
    loadNews();
  }catch(e){console.error(e)}
}

// ═══ OPS RENDER ══════════════════════════════════════════════
function renderOps(d){
  const t = d.totals || {};
  const sessions = d.sessions || [];
  const tiers = d.tiers || {};

  let html = '';
  html += '<div class="policy-banner"><span class="icon">⚠️</span> <strong>Priority:</strong>&nbsp;Underpowered model usage is RED severity. Quality over savings — when in doubt, tier UP.</div>';

  html += '<div class="big-stats">';
  html += '<div class="big-stat savings"><div class="num">$'+(t.savings||0).toFixed(2)+'</div><div class="lbl">Saved Today</div></div>';
  html += '<div class="big-stat savings"><div class="num">'+(t.pct_savings||0)+'%</div><div class="lbl">vs Opus Baseline</div></div>';
  html += '<div class="big-stat neutral"><div class="num">$'+(t.actual||0).toFixed(2)+'</div><div class="lbl">Actual Cost</div></div>';
  html += '<div class="big-stat '+(t.underpowered_alerts>0?'alert-red':'neutral')+'"><div class="num">'+(t.underpowered_alerts||0)+'</div><div class="lbl">🔴 Underpowered</div></div>';
  html += '<div class="big-stat '+(t.overpowered_alerts>0?'alert-yellow':'neutral')+'"><div class="num">'+(t.overpowered_alerts||0)+'</div><div class="lbl">🟡 Overpowered</div></div>';
  html += '<div class="big-stat neutral"><div class="num">'+(t.avg_mfs||0)+'</div><div class="lbl">Avg Model Fit</div></div>';
  html += '</div>';

  html += '<div class="tier-legend">';
  for (const [key, val] of Object.entries(tiers)) {
    html += '<div class="tier-item"><div class="tier-dot" style="background:'+val.color+'"></div><div class="tier-info"><div class="tier-name">'+key+' '+val.name+'</div><div class="tier-cost">'+val.cost+' • '+val.use+'</div></div></div>';
  }
  html += '</div>';

  html += '<div class="card full" style="margin-top:0">';
  html += '<h2><span class="icon">📋</span> SESSION LOG — TODAY</h2>';
  if (sessions.length) {
    html += '<div style="overflow-x:auto"><table>';
    html += '<tr><th>Time</th><th>Task</th><th>Model</th><th>Tier</th><th>Actual</th><th>Opus Would Be</th><th>Saved</th><th>Fit Score</th></tr>';
    sessions.forEach(function(s) {
      html += '<tr><td>'+s.time+'</td><td>'+s.name+'</td><td>'+s.model+'</td><td>'+tierBadge(s.tier)+'</td>';
      html += '<td>$'+s.cost_actual.toFixed(2)+'</td><td style="color:var(--dim)">$'+s.cost_opus.toFixed(2)+'</td>';
      html += '<td class="pos">$'+s.savings.toFixed(2)+'</td><td>'+mfsDots(s.mfs)+'</td></tr>';
    });
    html += '</table></div>';
  } else {
    html += '<div class="empty">No sessions logged yet today</div>';
  }
  html += '</div>';

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

// ═══ ORG RENDER ══════════════════════════════════════════════
function renderOrg(d) {
  let html = '';
  const teams = d.teams || [];

  // Summary stats
  const totalTasks = teams.reduce(function(a,t){return a+t.total_tasks},0);
  const totalDone = teams.reduce(function(a,t){return a+t.completed},0);
  const overallPct = totalTasks ? Math.round(totalDone/totalTasks*100) : 0;

  html += '<div class="big-stats">';
  html += '<div class="big-stat neutral"><div class="num">'+teams.length+'</div><div class="lbl">Teams</div></div>';
  html += '<div class="big-stat neutral"><div class="num">'+totalTasks+'</div><div class="lbl">Total Tasks</div></div>';
  html += '<div class="big-stat savings"><div class="num">'+totalDone+'</div><div class="lbl">Completed</div></div>';
  html += '<div class="big-stat '+(overallPct>=50?'savings':'alert-yellow')+'"><div class="num">'+overallPct+'%</div><div class="lbl">Completion Rate</div></div>';
  html += '</div>';

  // Team cards
  html += '<div class="grid">';
  teams.forEach(function(team){
    html += '<div class="card"><h2><span class="icon">👥</span> '+team.name.toUpperCase()+'</h2>';
    if(team.error){
      html += '<div class="empty">'+team.error+'</div>';
    } else {
      html += '<div class="stat-row"><span class="label">Total Tasks</span><span class="val">'+team.total_tasks+'</span></div>';
      html += '<div class="stat-row"><span class="label">Completed</span><span class="val pos">'+team.completed+'</span></div>';
      html += '<div class="stat-row"><span class="label">Completion</span><span class="val">'+team.completion_pct+'%</span></div>';
      html += '<div class="progress-bar"><div class="fill" style="width:'+team.completion_pct+'%;background:'+(team.completion_pct>=50?'var(--green)':team.completion_pct>=25?'var(--yellow)':'var(--red)')+'"></div></div>';

      if(team.open_tasks&&team.open_tasks.length){
        html += '<div style="margin-top:10px"><div style="font-size:11px;color:var(--dim);margin-bottom:6px">OPEN TASKS:</div>';
        team.open_tasks.forEach(function(t){
          html += '<div style="font-size:12px;padding:3px 0;color:var(--text)">☐ '+t.text+'</div>';
        });
        html += '</div>';
      }
    }
    html += '</div>';
  });
  html += '</div>';

  // Tables from team files (e.g. Operations model tiering)
  teams.forEach(function(team){
    if(team.tables&&team.tables.length){
      team.tables.forEach(function(tbl){
        html += '<div class="card full" style="margin-top:14px"><h2><span class="icon">📊</span> '+team.name.toUpperCase()+' — DATA TABLE</h2>';
        html += '<div style="overflow-x:auto"><table><tr>';
        tbl.headers.forEach(function(h){html+='<th>'+h+'</th>'});
        html += '</tr>';
        tbl.rows.forEach(function(row){
          html += '<tr>';
          row.forEach(function(cell){html+='<td>'+cell+'</td>'});
          html += '</tr>';
        });
        html += '</table></div></div>';
      });
    }
  });

  $('org-content').innerHTML = html;
}

// ═══ DATA LOADERS ════════════════════════════════════════════
async function loadTrading(){
  try{const r=await fetch('/api/trading');const d=await r.json();renderTrading(d)}catch(e){console.error(e)}
}
async function loadOps(){
  try{const r=await fetch('/api/ops');const d=await r.json();renderOps(d)}catch(e){console.error(e)}
}
async function loadPortfolio(){
  try{const r=await fetch('/api/portfolio');const d=await r.json();renderPortfolio(d)}catch(e){console.error(e)}
}
async function loadNews(){
  try{
    const [nr,sr,sbr]=await Promise.all([fetch('/api/news'),fetch('/api/signals'),fetch('/api/signals/portfolio')]);
    const [nd,sd,sbd]=await Promise.all([nr.json(),sr.json(),sbr.json()]);
    renderNews(nd,sd,sbd);
  }catch(e){console.error(e)}
}
async function loadOrg(){
  try{const r=await fetch('/api/org');const d=await r.json();renderOrg(d)}catch(e){console.error(e)}
}
async function loadETF(){
  try{const r=await fetch('/api/etf');const d=await r.json();renderETF(d)}catch(e){console.error(e)}
}

function renderETF(d) {
  const el = $('etf-content');
  if (!d || d.error) { el.innerHTML='<div class="card">Error loading ETF data</div>'; return; }
  const pnlColor = d.total_pnl >= 0 ? 'var(--green)' : 'var(--red)';
  const openPos = d.positions.filter(p => p.status === 'open');
  const monPos = d.positions.filter(p => p.status === 'monitoring');
  const closedPos = d.positions.filter(p => p.status === 'closed');

  let h = '<div class="big-stats">';
  h += '<div class="big-stat"><div class="label">Starting Capital</div><div class="num">$'+d.starting_capital.toLocaleString()+'</div></div>';
  h += '<div class="big-stat"><div class="label">Portfolio Value</div><div class="num" style="color:'+pnlColor+'">$'+d.total_value.toLocaleString()+'</div></div>';
  h += '<div class="big-stat"><div class="label">Total P&L</div><div class="num" style="color:'+pnlColor+'">$'+d.total_pnl.toFixed(2)+' ('+d.total_pnl_pct.toFixed(1)+'%)</div></div>';
  h += '<div class="big-stat"><div class="label">Open / Monitoring</div><div class="num">'+openPos.length+' / '+monPos.length+'</div></div>';
  h += '</div>';

  // Strategy allocation with mirror info
  h += '<div class="card full"><h3>📊 Strategy Allocation (ETF Mirror)</h3><table><thead><tr><th>ETF Strategy</th><th>Mirrors</th><th>Alloc %</th><th>Allocated</th></tr></thead><tbody>';
  for (const s of d.strategies) {
    h += '<tr><td>'+s.name+'</td><td style="color:var(--dim)">'+(s.mirrors||'-')+'</td><td>'+s.allocation_pct+'%</td><td>$'+s.allocated.toLocaleString()+'</td></tr>';
  }
  h += '</tbody></table></div>';

  // Open positions
  if (openPos.length > 0 || monPos.length > 0) {
    h += '<div class="card full"><h3>📈 Open Positions</h3><table><thead><tr><th>Strategy</th><th>Trade</th><th>Ticker</th><th>Entry</th><th>Value</th><th>P&L</th><th>Status</th></tr></thead><tbody>';
    for (const p of [...openPos, ...monPos]) {
      const pc = p.pnl >= 0 ? 'var(--green)' : 'var(--red)';
      const statusBadge = p.status === 'monitoring' ? '<span style="color:var(--yellow)">👁 Monitoring</span>' : '<span style="color:var(--green)">● Open</span>';
      h += '<tr><td>'+p.strategy+'</td><td style="font-size:12px">'+p.description+'</td><td><strong>'+(p.ticker||'-')+'</strong></td><td>'+p.entry_price+'</td><td>$'+p.current_value.toLocaleString()+'</td><td style="color:'+pc+'">$'+p.pnl.toFixed(2)+'</td><td>'+statusBadge+'</td></tr>';
    }
    h += '</tbody></table></div>';
  }

  // Mirrors reference
  h += '<div class="card full"><h3>🔗 Mirror Mapping</h3><table><thead><tr><th>ETF Position</th><th>Mirrors (Main Portfolio)</th><th>Notes</th></tr></thead><tbody>';
  for (const p of d.positions) {
    h += '<tr><td><strong>'+(p.ticker||'-')+'</strong> — '+(p.description||'').substring(0,50)+'</td><td style="color:var(--dim)">'+(p.mirrors||'-')+'</td><td style="font-size:11px">'+(p.notes||'').substring(0,80)+'</td></tr>';
  }
  h += '</tbody></table></div>';

  // Trade log
  if (d.trades && d.trades.length > 0) {
    h += '<div class="card full"><h3>📝 Trade Log</h3><table><thead><tr><th>Time</th><th>Action</th><th>Description</th><th>Strategy</th></tr></thead><tbody>';
    for (const t of d.trades.slice(-20).reverse()) {
      const ac = t.action==='OPEN'?'var(--green)':t.action==='CLOSE'?'var(--red)':'var(--yellow)';
      h += '<tr><td>'+t.timestamp+'</td><td style="color:'+ac+'">'+t.action+'</td><td>'+t.description+'</td><td>'+t.strategy+'</td></tr>';
    }
    h += '</tbody></table></div>';
  }

  h += '<div style="color:var(--dim);font-size:11px;margin-top:8px">Updated: '+d.updated+'</div>';
  el.innerHTML = h;
}

// Initial load
loadTrading();
setInterval(loadTrading,300000);
setInterval(function(){
  const active = document.querySelector('.page.active');
  if(!active) return;
  const id = active.id;
  if(id==='page-ops') loadOps();
  if(id==='page-portfolio') loadPortfolio();
  if(id==='page-news') loadNews();
  if(id==='page-etf') loadETF();
  if(id==='page-org') loadOrg();
},60000);
</script>
</body>
</html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
