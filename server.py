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
import os, json, asyncio, threading
from datetime import datetime
from fastapi import FastAPI, Response, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import config, scanner, trade_tracker, learner, strategy_hougaard, analytics

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
QUOTES_FILE   = os.path.join(BASE_DIR, "index_quotes.json")
BIAS_FILE     = os.path.join(BASE_DIR, "index_bias.json")
NEWS_FILE     = os.path.join(BASE_DIR, "market_news.json")
TV_STATE_FILE = os.path.join(BASE_DIR, "index_state.json")

# ─── SSE BROADCAST ────────────────────────────────────────────────────────────
_sse_loop:   asyncio.AbstractEventLoop = None
_sse_queues: list                      = []
_sse_lock                              = threading.Lock()

@app.on_event("startup")
async def _capture_loop():
    global _sse_loop
    _sse_loop = asyncio.get_event_loop()

def _broadcast(payload: dict):
    """Push a scan-update event to every connected SSE client. Thread-safe."""
    if _sse_loop is None:
        return
    msg = json.dumps(payload, default=str)
    with _sse_lock:
        for q in list(_sse_queues):
            try:
                _sse_loop.call_soon_threadsafe(q.put_nowait, msg)
            except Exception:
                pass

def _notify_new_trade(trade_symbol: str, trade_data: dict):
    """Called by scanner when a new trade is found. Triggers instant dashboard refresh."""
    _broadcast({
        "type":   "scan_update",
        "symbol": trade_symbol,
        "score":  trade_data.get("score", 0),
        "setup":  trade_data.get("setup", ""),
    })


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


@app.get("/api/indices")
async def get_indices():
    """Live index data from TradingView (Nifty, Sensex, VIX, GIFT Nifty)."""
    if not os.path.exists(TV_STATE_FILE):
        return JSONResponse({"nifty": {}, "sensex": {}, "vix": {}, "gift_nifty": {}})
    with open(TV_STATE_FILE) as f:
        return JSONResponse(json.load(f))


@app.get("/api/stream")
async def sse_stream(request: Request):
    """Server-Sent Events stream. Dashboard subscribes and gets instant trade alerts."""
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    with _sse_lock:
        _sse_queues.append(q)

    async def generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=25)
                    yield f"data: {msg}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"  # keepalive — stops proxies from closing the connection
        finally:
            with _sse_lock:
                if q in _sse_queues:
                    _sse_queues.remove(q)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


@app.get("/api/news")
async def get_news():
    if not os.path.exists(NEWS_FILE):
        return JSONResponse({"articles": [], "updated_at": None, "count": 0})
    with open(NEWS_FILE) as f:
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


# ─── TEST TRIGGER (development only) ──────────────────────────────────────────
@app.get("/api/test-trigger")
async def test_trigger():
    """
    Injects a fake trade into trades_state.json and broadcasts an SSE event so
    the dashboard fires its notification + sound. No Fyers login required.
    Safe to call repeatedly — wipes the previous MOCK trade each time.
    """
    from datetime import date as _date

    MOCK_SYM    = "MOCK-RELIANCE"
    MOCK_STRIKE = "2500 CE"
    today       = _date.today().isoformat()

    # Remove any leftover mock trade so every run produces a fresh key
    active = trade_tracker._load_active()
    stale  = [k for k in list(active.keys()) if "MOCK" in k]
    for k in stale:
        del active[k]
    trade_tracker._save_active(active)

    # Register the mock trade — returns the new key, or None if somehow still locked
    key = trade_tracker.register_trade(
        symbol      = MOCK_SYM,
        direction   = "BULL",
        strike      = MOCK_STRIKE,
        entry       = 85.0,
        sl_price    = 60.0,
        tgt_price   = 140.0,
        setup       = "BOS + EMA Confluence",
        given_at    = datetime.now().strftime("%H:%M"),
        expiry      = "05 Jun",
        expiry_date = today,
        sector      = "Test",
        extra       = {
            "score":      5.5,
            "option_type": "CE",
            "vol_surge":  2.1,
            "rr_ratio":   "1:2",
            "iv_rank":    45,
            "sl_amt":     25.0,
            "tgt_amt":    55.0,
            "oi_signal":  "Long Buildup",
            "oi_dir":     "BULL",
        },
    )

    if not key:
        return JSONResponse({"ok": False, "msg": "Mock trade already exists and could not be cleared."}, status_code=500)

    # Broadcast SSE → dashboard calls fetchFull() → sees new key → notification fires
    _notify_new_trade(MOCK_SYM, {"score": 5.5, "setup": "BOS + EMA Confluence"})

    return JSONResponse({"ok": True, "key": key,
                         "msg": f"Mock trade injected: {MOCK_SYM} {MOCK_STRIKE}. Watch the dashboard."})
