# Options Radar v10 — Project Context for Claude

## What This Project Is

A personal trading dashboard for Indian F&O (Futures & Options) markets. It:
- Scans 50 top NSE F&O stocks every 5 minutes during market hours
- Gives trade setups (entry, SL, target, direction) with scoring
- Shows live Nifty/Sensex/VIX/GIFT Nifty prices via TradingView
- Pushes instant browser notifications when a new trade is found
- Tracks trade history (executed, skipped, outcomes)

**Single user. Runs locally on Mac. No cloud deployment yet.**

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.14, FastAPI, uvicorn |
| Frontend | Vanilla HTML/JS/CSS (single file: `dashboard.html`) |
| Broker API | Fyers API v3 (Python SDK) |
| Index data | tvDatafeed (TradingView, free) + NSE unofficial API + yfinance |
| Real-time push | Server-Sent Events (SSE) via FastAPI StreamingResponse |
| State storage | JSON files (no database) |
| Server | localhost:8000 |

---

## File Map

```
fyers_bot/
├── main.py              # Entry point. Starts all threads. Run: python3 main.py
├── server.py            # FastAPI app. All HTTP endpoints + SSE stream
├── scanner.py           # Core scanner. Fetches quotes, scores stocks, finds trades
├── trade_tracker.py     # Manages active trades, history, executed/skipped state
├── tv_provider.py       # TradingView feed. Updates index_state.json every 3s
├── analytics.py         # Market breadth, IV ranks, sector heatmap (runs every 15min)
├── bias_engine.py       # PCR, max pain, support/resistance for Nifty/Sensex
├── strategy_hougaard.py # Hougaard strategy implementation (separate from main scanner)
├── learner.py           # Weekly learning loop - adjusts score multipliers from history
├── news_fetcher.py      # RSS feed aggregator. Saves to market_news.json
├── setups_advanced.py   # 6 institutional setups (BOS, EMA confluence, etc.)
├── gift_nifty.py        # Legacy GIFT Nifty module (now superseded by tv_provider.py)
├── config.py            # ALL credentials and settings (gitignored — never commit)
├── config.example.py    # Template showing structure of config.py
├── login.py             # Run once to generate Fyers access token
├── dashboard.html       # Entire frontend (1400+ lines, single file)
│
├── index_state.json     # Written by tv_provider.py — live TV index prices
├── index_quotes.json    # Written by main.py — NSE/yfinance index prices (fallback)
├── radar_cache.json     # Written by scanner.py — full scan results
├── trades_state.json    # Written by trade_tracker.py — active trades today
├── trade_history.json   # Permanent trade history
├── market_news.json     # Written by news_fetcher.py
├── analytics_cache.json # Written by analytics.py
├── index_bias.json      # Written by bias_engine.py
├── access_token.txt     # Fyers OAuth token (gitignored)
└── learnings.json       # Score multipliers from weekly learning
```

---

## How to Run

```bash
# First time only — get Fyers token
python3 login.py

# Start the bot
python3 main.py
# Dashboard opens automatically at http://localhost:8000
```

**Dependencies to install if missing:**
```bash
pip3 install requests uvicorn fastapi yfinance fyers-apiv3 pandas numpy feedparser --break-system-packages
pip3 install git+https://github.com/rongardF/tvdatafeed.git --break-system-packages
```

---

## API Endpoints (server.py)

| Endpoint | Description |
|----------|-------------|
| `GET /` | Serves dashboard.html |
| `GET /api/data` | Full scan results (radar_cache.json + live trade status merge) |
| `GET /api/quotes` | NSE/yfinance index prices (fallback when TV is down) |
| `GET /api/indices` | TradingView live index prices (index_state.json) |
| `GET /api/news` | Market news articles (market_news.json) |
| `GET /api/stream` | SSE stream — dashboard subscribes for instant trade push |
| `GET /api/bias` | PCR, max pain, S/R levels |
| `GET /api/analytics` | Market breadth, IV ranks, sector heatmap |
| `GET /api/history` | Full permanent trade history |
| `GET /api/health` | Status check |
| `POST /api/expire` | Manually move trade to history |
| `GET /api/test-trigger` | Injects mock trade + fires SSE (for testing notifications) |

---

## Thread Architecture (main.py)

All threads are daemon threads started in `main()`:

```
main thread          → run_scanner_loop()     — full scan every 5 min
daemon thread 1      → run_web_server()       — uvicorn FastAPI
daemon thread 2      → run_fast_quotes()      — NSE+yfinance every 3s
daemon thread 3      → run_bias_updater()     — PCR/SR every 15 min
daemon thread 4      → bias_engine.run_bias_engine()
daemon thread 5      → learner.run_weekly_learner_loop()
daemon thread 6      → strategy_hougaard.run_hougaard_loop() (Nifty)
daemon thread 7      → strategy_hougaard.run_hougaard_loop() (Sensex)
daemon thread 8      → run_end_of_day_cleanup()
daemon thread 9      → news_fetcher.run_news_loop()
daemon thread 10     → tv_provider.run_tv_loop()    — TV feed every 3s
daemon thread 11     → analytics.run_analytics_loop()
```

---

## Dashboard JS Architecture (dashboard.html)

All JavaScript is in one `<script>` block (lines 553–1429). Key functions:

| Function | Purpose |
|----------|---------|
| `fetchFull()` | Fetches /api/data — main scan results, trades, news |
| `fetchQuotes()` | Fetches /api/quotes every 3s — fallback index prices |
| `fetchIndices()` | Fetches /api/indices every 3s — TV live prices |
| `fetchNews()` | Fetches /api/news every 3 min — independent of scan |
| `fetchBias()` | Fetches /api/bias every 60s |
| `fetchAnalytics()` | Fetches /api/analytics every 60s |
| `_checkNewTrades()` | Dedup new trades, fire browser notifications |
| `sendNotif()` | Browser Notification API + Web Audio ping |
| `buildOvCard()` | Renders full Nifty/Sensex overview card (needs idx.ltp) |
| `updateCardLive()` | Updates existing card prices without full rebuild |
| `_applyTvIndex()` | Inside fetchIndices — builds card if not built, updates if built |
| `renderRadar()` | Renders the F&O Radar tab trade rows |
| `renderOvStocks()` | Renders trade cards on Overview tab |

**Critical pattern:** `fetchIndices()` checks if `card-nifty-ltp` element exists before deciding to call `buildOvCard()` (full rebuild) or `updateCardLive()` (price update only). This prevents `fetchFull()` from overwriting TV-built cards with empty scanner data.

**SSE flow:** `EventSource('/api/stream')` → `onmessage` → `fetchFull()` → `_checkNewTrades()` → notification

---

## State Files — What Writes What

| File | Written by | Read by |
|------|-----------|---------|
| `index_state.json` | `tv_provider.py` every 3s | `server.py /api/indices` |
| `index_quotes.json` | `main.py run_fast_quotes()` every 3s | `server.py /api/quotes` |
| `radar_cache.json` | `scanner.py scan_all()` every 5 min | `server.py /api/data` |
| `trades_state.json` | `trade_tracker.py` | `server.py /api/data` (merged) |
| `market_news.json` | `news_fetcher.py` every 30 min | `server.py /api/news` |
| `analytics_cache.json` | `analytics.py` every 15 min | `server.py /api/analytics` |
| `index_bias.json` | `bias_engine.py` every 15 min | `server.py /api/bias` |
| `learnings.json` | `learner.py` weekly | `scanner.py score_stock()` |

---

## config.py Structure (never commit — use config.example.py as reference)

```python
CLIENT_ID    = "..."       # Fyers app client ID
SECRET_KEY   = "..."       # Fyers secret key
REDIRECT_URI = "http://127.0.0.1:5000"
TV_USERNAME  = "..."       # TradingView email
TV_PASSWORD  = "..."       # TradingView password
FNO_UNIVERSE = [...]       # 50 NSE F&O symbols
SECTOR_MAP   = {...}       # Symbol → sector mapping
STOCK_STRIKE_STEP = {...}  # Strike intervals per stock
MIN_SCORE_FOR_RADAR = 5.0  # Minimum score to show in radar
SCAN_INTERVAL_SECONDS = 300
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8000
```

---

## Known Issues / Decisions

- **No database** — all state is JSON files. Works fine for single user. PostgreSQL migration is a future plan.
- **Fyers token expires daily** — must run `python3 login.py` each morning before market opens.
- **TradingView Sensex flaky** — BSE exchange connection drops randomly. `tv_provider.py` keeps last known value on failure.
- **TV data is last traded price** — when market is closed, TV shows Friday's close. This is correct behaviour.
- **Market breadth / IV ranks** — only update during market hours. Show `--` when closed.
- **`gift_nifty.py`** — legacy file, kept for reference. GIFT Nifty is now fetched by `tv_provider.py` using symbol `NIFTY1!` on `NSEIX` exchange.

---

## Future Plans (discussed but not built)

1. **React + Vite + Tailwind + shadcn/ui** frontend to replace single-file `dashboard.html`
2. **Web-based Fyers login** — `/login` and `/callback` endpoints so token refresh happens in browser, no terminal needed
3. **Server deployment** (DigitalOcean/Hetzner) so dashboard is accessible from anywhere
4. **TradingView Lightweight Charts** for candlestick charts on trade setups
5. **PostgreSQL** to replace JSON state files
6. Keep Python FastAPI backend — all broker/data libraries are Python-only (Fyers SDK, tvDatafeed, yfinance, pandas)

---

## GitHub

Repo: https://github.com/amritonlineshopping-eng/tradingoptionradar
Main branch: `main`
