# QuantLab Trading Dashboard

Real-time trading dashboard combining three monitors:
- **VIX Monitor** — Current level, signal (SELL/NEUTRAL/DON'T SELL), term structure
- **Funding Rates** — Top crypto funding rate opportunities from Binance, Bybit, Gate.io
- **Arb Scanner** — Polymarket vs Kalshi prediction market arbitrage (spread > 3%)

## Deploy on Replit

1. Create a new **Python** Repl on [replit.com](https://replit.com)
2. Upload all files from this directory (or import from GitHub)
3. Click **Run** — Replit auto-detects the `.replit` config
4. Dashboard serves on the provided Replit URL

## Run Locally

```bash
pip install -r requirements.txt
python main.py
# Open http://localhost:5000
```

## How It Works

- Flask serves a single-page dark-theme dashboard
- Background thread fetches all data sources every 5 minutes
- Frontend auto-refreshes via `/api/data` JSON endpoint
- No database needed — all data is live from APIs

## Notes

- Binance/Bybit may be geo-blocked in some regions (use VPN if needed)
- Kalshi/Polymarket APIs are public but may rate-limit
- VIX data comes from Yahoo Finance (yfinance)
