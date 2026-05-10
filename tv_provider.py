"""
tv_provider.py — Real-time index data via TradingView (tvDatafeed)

Fetches Nifty 50, Sensex, India VIX, and GIFT Nifty every 3 seconds.
Persists all values to index_state.json.

Persistence rule: if a fetch fails, the OLD price is kept unchanged.
Only last_updated is not refreshed, so the dashboard detects staleness
via its age-colour logic (green < 1 min, orange > 5 min).
"""

import os, json, time, logging
from datetime import datetime, date

import config

log = logging.getLogger("tv")

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "index_state.json")

# Primary symbols to try, plus per-symbol fallback exchanges
TV_SYMBOLS = [
    # (state-key,   symbol,     primary-exchange,  fallback-exchange-or-None)
    ("nifty",      "NIFTY",    "NSE",             None),
    ("sensex",     "SENSEX",   "BSE",             None),
    ("vix",        "INDIAVIX", "NSE",             None),
    ("gift_nifty", "NIFTY1!",  "NSEIX",           "NSE"),   # NSEIX can be flaky on weekends
]


# ─── STATE I/O ────────────────────────────────────────────────────────────────

def _load() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(state: dict):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, default=str, indent=2)
    except Exception as e:
        print(f"TV ERROR: could not write index_state.json: {e}")


# ─── NSE COLD-START SEED ──────────────────────────────────────────────────────

def _seed_from_nse(state: dict) -> dict:
    """
    Pre-populate state from NSE + yfinance if index_state.json is empty.
    This ensures the dashboard always shows something even before TV connects.
    """
    print("TV: seeding index_state.json from NSE (cold start)...")
    try:
        import requests as _req
        s = _req.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.nseindia.com",
        })
        s.get("https://www.nseindia.com", timeout=8)

        def _nse_index(index_param):
            try:
                r = s.get(f"https://www.nseindia.com/api/equity-stockIndices?index={index_param}", timeout=5)
                d = r.json()["data"][0]
                ltp = round(float(d["lastPrice"]), 2)
                pc  = round(float(d["previousClose"]), 2)
                return {
                    "price":        ltp,
                    "prev_close":   pc,
                    "change":       round(float(d["change"]), 2),
                    "change_pct":   round(float(d["pChange"]), 2),
                    "high":         round(float(d.get("dayHigh", d.get("high", ltp))), 2),
                    "low":          round(float(d.get("dayLow",  d.get("low",  ltp))), 2),
                    "last_updated": datetime.now().isoformat(),
                    "source":       "NSE (seed)",
                }
            except Exception as e:
                print(f"TV: NSE seed error for {index_param}: {e}")
                return None

        nifty = _nse_index("NIFTY%2050")
        vix   = _nse_index("INDIA%20VIX")
        if nifty:
            state.setdefault("nifty", nifty)
            print(f"TV: seeded NIFTY = {nifty['price']}")
        if vix:
            state.setdefault("vix", {"price": vix["price"], "last_updated": vix["last_updated"], "source": "NSE (seed)"})
            print(f"TV: seeded VIX   = {vix['price']}")

    except Exception as e:
        print(f"TV: NSE session seed error: {e}")

    try:
        import yfinance as yf
        info = yf.Ticker("^BSESN").fast_info
        ltp = round(float(info.get("lastPrice") or info.get("previousClose") or 0), 2)
        pc  = round(float(info.get("previousClose") or 0), 2)
        if ltp > 0:
            state.setdefault("sensex", {
                "price":        ltp,
                "prev_close":   pc,
                "change":       round(ltp - pc, 2),
                "change_pct":   round((ltp - pc) / pc * 100, 2) if pc else 0,
                "high":         round(float(info.get("dayHigh") or 0), 2),
                "low":          round(float(info.get("dayLow") or 0), 2),
                "last_updated": datetime.now().isoformat(),
                "source":       "yfinance (seed)",
            })
            print(f"TV: seeded SENSEX = {ltp}")
    except Exception as e:
        print(f"TV: yfinance seed error: {e}")

    return state


# ─── TV HELPERS ───────────────────────────────────────────────────────────────

def _connect(TvDatafeed) -> object:
    """
    Try authenticated login, fall back to anonymous.
    Prints the exact error so you can diagnose login issues.
    """
    if config.TV_USERNAME and config.TV_PASSWORD:
        print(f"TV: attempting login as {config.TV_USERNAME}...")
        try:
            tv = TvDatafeed(config.TV_USERNAME, config.TV_PASSWORD)
            print("TV: authenticated login OK.")
            return tv
        except Exception as e:
            print(f"TV: authenticated login FAILED — {type(e).__name__}: {e}")
            print("TV: falling back to anonymous mode...")

    try:
        tv = TvDatafeed()
        print("TV: anonymous login OK.")
        return tv
    except Exception as e:
        print(f"TV: anonymous login FAILED — {type(e).__name__}: {e}")
        return None


def _fetch_prev_close(tv, symbol: str, exchange: str, Interval) -> float:
    """Return yesterday's close via 3 daily bars. Returns 0.0 on failure."""
    try:
        df = tv.get_hist(symbol=symbol, exchange=exchange,
                         interval=Interval.in_daily, n_bars=3)
        if df is None or df.empty:
            return 0.0
        today     = date.today()
        last_date = df.index[-1].date() if hasattr(df.index[-1], "date") else today
        if last_date >= today and len(df) >= 2:
            return round(float(df.iloc[-2]["close"]), 2)
        return round(float(df.iloc[-1]["close"]), 2)
    except Exception as e:
        print(f"TV: prev_close fetch error {symbol}/{exchange}: {e}")
        return 0.0


def _get_hist_safe(tv, symbol, exchange, Interval):
    """
    Fetch 1-minute bars. Returns (df, actual_exchange_used) or (None, exchange).
    """
    try:
        df = tv.get_hist(symbol=symbol, exchange=exchange,
                         interval=Interval.in_1_minute, n_bars=3)
        if df is not None and not df.empty:
            return df, exchange
    except Exception as e:
        print(f"TV: get_hist({symbol}/{exchange}) error: {e}")
    return None, exchange


# ─── MAIN LOOP ────────────────────────────────────────────────────────────────

def run_tv_loop():
    """
    Daemon thread entry point.
    Connects to TradingView, loops every 3 seconds, reconnects on failure.
    """
    try:
        from tvDatafeed import TvDatafeed, Interval
    except ImportError:
        print("TV ERROR: tvDatafeed not installed. Run:")
        print("  pip install git+https://github.com/rongardF/tvdatafeed.git")
        return

    state           = _load()
    prev_closes     = {}     # key → float
    daily_refreshed = {}     # key → date
    tv              = None
    tick            = 0

    # Seed from NSE if we have nothing to show yet
    needs_seed = not any(state.get(k, {}).get("price", 0) > 0
                         for k, *_ in TV_SYMBOLS)
    if needs_seed:
        state = _seed_from_nse(state)
        _save(state)

    while True:
        tick += 1
        print(f"DEBUG: TV Loop Tick #{tick}  [{datetime.now().strftime('%H:%M:%S')}]")

        # ── (Re)connect ───────────────────────────────────────────────────────
        if tv is None:
            tv = _connect(TvDatafeed)
            if tv is None:
                print("TV: both login modes failed — retrying in 30s")
                time.sleep(30)
                continue

        try:
            now   = datetime.now()
            today = date.today()

            for key, symbol, primary_exchange, fallback_exchange in TV_SYMBOLS:

                # ── Refresh prev_close once per calendar day ──────────────────
                if daily_refreshed.get(key) != today:
                    pc = _fetch_prev_close(tv, symbol, primary_exchange, Interval)
                    if pc == 0.0 and fallback_exchange:
                        pc = _fetch_prev_close(tv, symbol, fallback_exchange, Interval)
                    if pc > 0:
                        prev_closes[key]     = pc
                        daily_refreshed[key] = today
                        print(f"TV: {symbol} prev_close = {pc}")
                    elif state.get(key, {}).get("prev_close", 0) > 0:
                        prev_closes[key] = state[key]["prev_close"]

                # ── Fetch latest bar ──────────────────────────────────────────
                df, used_exchange = _get_hist_safe(tv, symbol, primary_exchange, Interval)

                # Try fallback exchange if primary returned nothing
                if df is None and fallback_exchange:
                    print(f"TV: {symbol}/{primary_exchange} empty — trying {fallback_exchange}")
                    df, used_exchange = _get_hist_safe(tv, symbol, fallback_exchange, Interval)

                if df is None or df.empty:
                    print(f"TV: {symbol} — no data on either exchange, keeping last value")
                    continue

                ltp = round(float(df.iloc[-1]["close"]), 2)
                if ltp <= 0:
                    print(f"TV: {symbol} returned ltp=0, skipping")
                    continue

                pc      = prev_closes.get(key, state.get(key, {}).get("prev_close", 0))
                chg     = round(ltp - pc, 2) if pc > 0 else 0
                chg_pct = round(chg / pc * 100, 2) if pc > 0 else 0
                high    = round(float(df.iloc[-1]["high"]) if "high" in df.columns else ltp, 2)
                low_val = round(float(df.iloc[-1]["low"])  if "low"  in df.columns else ltp, 2)

                state[key] = {
                    "price":        ltp,
                    "prev_close":   round(pc, 2),
                    "change":       chg,
                    "change_pct":   chg_pct,
                    "high":         high,
                    "low":          low_val,
                    "last_updated": now.isoformat(),
                    "source":       f"TradingView/{used_exchange}",
                }
                print(f"TV: {symbol:<10} = {ltp:>10.2f}  ({chg_pct:+.2f}%)")

            state["updated_at"] = now.isoformat()
            _save(state)

        except Exception as e:
            print(f"TV: loop body error — {type(e).__name__}: {e}")
            print("TV: forcing reconnect on next tick")
            tv = None

        time.sleep(3)
