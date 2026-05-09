"""
server.py — Options Radar v10
Endpoints:
  GET  /              → dashboard.html
  GET  /api/data      → radar_cache.json (full scan, market_open always live)
  GET  /api/quotes    → index_quotes.json (5s live prices)
  GET  /api/bias      → index_bias.json (15m bias/PCR)
  GET  /api/history   → trade_history.json (permanent record)
  GET  /api/stats     → history summary stats
  POST /api/expire    → manually move a trade to history
  POST /api/update_history → update an existing history record
  GET  /api/health    → system status
"""
import os, json
from datetime import datetime
from fastapi import FastAPI, Response, Body

from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import config, scanner, trade_tracker, learner, strategy_hougaard, analytics

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
QUOTES_FILE = os.path.join(BASE_DIR, "index_quotes.json")
BIAS_FILE   = os.path.join(BASE_DIR, "index_bias.json")


@app.get("/", response_class=HTMLResponse)
async def serve():
    p = os.path.join(BASE_DIR, "dashboard.html")
    if not os.path.exists(p):
        return HTMLResponse("<h2>dashboard.html not found</h2>", status_code=404)
    with open(p) as f:
        return HTMLResponse(f.read())


@app.get("/api/data")
async def get_data():
    is_open = scanner._market_open()
    if not os.path.exists(config.CACHE_FILE):
        return JSONResponse({
            "meta": {"scanned_at": None, "vix": 0, "vix_env": "Waiting...",
                     "market_open": is_open, "total": 0, "strong": 0,
                     "lb": 0, "sc": 0, "sb": 0, "news_count": 0},
            "nifty": {}, "sensex": {}, "stocks": [], "news": [], "today_trades": {}
        })
    with open(config.CACHE_FILE) as f:
        data = json.load(f)
    if "meta" in data:
        data["meta"]["market_open"] = is_open  # always live

    # ── LIVE STATUS MERGE ─────────────────────────────────────────────────
    # The cache is written every 5 min. If a trade was manually expired or
    # hit SL/target between scans, merge the live status from trades_state.json
    # so the dashboard reflects it immediately without waiting for next scan.
    live_trades = trade_tracker.get_today_trades()
    expired_set = trade_tracker.get_expired_symbols_today()

    stocks = data.get("stocks", [])
    updated = []
    for s in stocks:
        sym    = s.get("symbol", "")
        strike = s.get("strike", "")
        # Remove if manually expired
        if sym in expired_set:
            continue  # don't show expired trades in active list
        # Update status_label from live trade tracker
        live_key = f"{sym}_{str(strike).replace(' ','_')}_{__import__('datetime').date.today().isoformat()}"
        live = live_trades.get(live_key)
        if live:
            status = live.get("status", "ACTIVE")
            hit_at = live.get("hit_at", "")
            if status == "TARGET":
                s["status_label"] = f"✓ TARGET HIT at {hit_at}"
                s["trade_status"]  = "TARGET"
                s["hit_at"]        = hit_at
            elif status == "SL_HIT":
                s["status_label"] = f"✗ SL HIT at {hit_at}"
                s["trade_status"]  = "SL_HIT"
                s["hit_at"]        = hit_at
            else:
                s["trade_status"] = "ACTIVE"
            # Propagate live LTP and its timestamp to dashboard
            if live.get("option_ltp"):
                s["option_ltp"] = live["option_ltp"]
            if live.get("last_ltp_update"):
                s["last_ltp_update"] = live["last_ltp_update"]
        updated.append(s)
    data["stocks"] = updated
    data["today_trades"] = live_trades
    return JSONResponse(data)


@app.get("/api/quotes")
async def get_quotes():
    if not os.path.exists(QUOTES_FILE):
        return JSONResponse({
            "nifty":  {"ltp": 0, "chg_pts": 0, "chg_pct": 0},
            "sensex": {"ltp": 0, "chg_pts": 0, "chg_pct": 0},
            "vix":    {"ltp": 0},
            "updated_at": None, "market_open": scanner._market_open()
        })
    with open(QUOTES_FILE) as f:
        return JSONResponse(json.load(f))


@app.get("/api/bias")
async def get_bias():
    if not os.path.exists(BIAS_FILE):
        return JSONResponse({"nifty": {}, "sensex": {}, "updated_at": None})
    with open(BIAS_FILE) as f:
        return JSONResponse(json.load(f))


@app.get("/api/history")
async def get_history():
    """Full permanent trade history, newest first."""
    return JSONResponse(trade_tracker.get_all_history())


@app.get("/api/stats")
async def get_stats():
    """Summary stats for trade history."""
    return JSONResponse(trade_tracker.get_history_stats())


@app.post("/api/expire")
async def expire_trade(body: dict = Body(...)):
    """
    Manually move an active trade to expired history.
    Body: {
      symbol, strike, trade_date,
      taken: bool,
      user_entry: float|null,
      user_exit: float|null,
      notes: string
    }
    """
    result = trade_tracker.manually_expire_trade(
        symbol     = body.get("symbol"),
        strike     = body.get("strike"),
        trade_date = body.get("trade_date"),
        taken      = body.get("taken", False),
        user_entry = body.get("user_entry"),
        user_exit  = body.get("user_exit"),
        notes      = body.get("notes", ""),
    )
    return JSONResponse(result)


@app.post("/api/update_history")
async def update_history(body: dict = Body(...)):
    """
    Update an existing history record (e.g. add exit price later).
    Body: { key, taken, user_entry, user_exit, notes }
    """
    result = trade_tracker.update_history_record(
        key        = body.get("key"),
        taken      = body.get("taken", False),
        user_entry = body.get("user_entry"),
        user_exit  = body.get("user_exit"),
        notes      = body.get("notes", ""),
    )
    return JSONResponse(result)


@app.get("/api/executed")
async def get_executed():
    return JSONResponse(trade_tracker.get_executed_trades())


@app.get("/api/non_executed")
async def get_non_executed():
    return JSONResponse(trade_tracker.get_non_executed_trades())


@app.get("/api/learning_report")
async def get_learning_report():
    """Latest weekly learning report and current score multipliers."""
    report   = learner.get_latest_report()
    lrn_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "learnings.json")
    learnings = {}
    if os.path.exists(lrn_file):
        with open(lrn_file) as f:
            learnings = json.load(f)
    return JSONResponse({"report": report, "learnings": learnings})


@app.post("/api/run_learning")
async def run_learning_now():
    """Manually trigger learning analysis (for testing or end-of-week override)."""
    try:
        result = learner.analyze(label="manual")
        return JSONResponse({"ok": True, "weeks_analyzed": result.get("weeks_analyzed"),
                             "vol_threshold": result.get("vol_threshold")})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/api/hougaard")
async def get_hougaard():
    """Hougaard strategy trades — all historical records."""
    return JSONResponse(strategy_hougaard.get_all_trades())


@app.get("/api/hougaard/active")
async def get_hougaard_active():
    """Today's active Hougaard trades."""
    return JSONResponse(strategy_hougaard.get_active_trades())



# ─── INSTANT TRADE NOTIFICATIONS ──────────────────────────────────────────
# Removed complex SSE - trades now sync via regular data fetch polling
def _notify_new_trade(trade_symbol, trade_data):
    """Called whenever a new trade is detected. Currently used for logging."""
    pass



@app.get("/api/analytics")
async def get_analytics():
    """Full analytics: Greeks, IV ranks, breadth, sector, GIFT Nifty."""
    return JSONResponse(analytics.get_cached_analytics())


@app.get("/api/iv_ranks")
async def get_iv_ranks():
    data = analytics.get_cached_analytics()
    return JSONResponse(data.get("iv_ranks", {}))


@app.get("/api/market_breadth")
async def get_market_breadth():
    data = analytics.get_cached_analytics()
    return JSONResponse(data.get("market_breadth", {}))


@app.get("/api/sector_heatmap")
async def get_sector_heatmap():
    data = analytics.get_cached_analytics()
    return JSONResponse(data.get("sector_heatmap", []))


@app.get("/api/gift_nifty")
async def get_gift_nifty():
    data = analytics.get_cached_analytics()
    return JSONResponse(data.get("gift_nifty", {}))


@app.get("/api/health")

async def health():
    return {"status": "ok", "time": datetime.now().isoformat(),
            "market_open": scanner._market_open()}
