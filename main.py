"""
main.py — Options Radar v10
- GIFT Nifty removed
- Nifty/Sensex chain tabs removed (trades still given, appear in Overview)
- Permanent trade history via trade_tracker
"""
import os, sys, json, time, threading, logging, webbrowser
from datetime import datetime
import uvicorn
import config, scanner, trade_tracker, learner, bias_engine, strategy_hougaard, news_fetcher, analytics
import server as srv

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-7s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("main")

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
QUOTES_FILE = os.path.join(BASE_DIR, "index_quotes.json")
BIAS_FILE   = os.path.join(BASE_DIR, "index_bias.json")
INDEX_SYMS  = [config.NIFTY_SYMBOL, config.SENSEX_SYMBOL, config.VIX_SYMBOL]


# ─── THREADS ──────────────────────────────────────────────────────────────────

def run_end_of_day_cleanup():
    """
    Runs once per day at 15:30 (market close).
    Moves all unclaimed ACTIVE trades to NON_EXECUTED.
    """
    import time
    last_cleanup_date = None
    while True:
        try:
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            # Only run once per day, at 15:30+
            if now.hour == 15 and now.minute >= 30 and last_cleanup_date != today:
                log.info("=== EOD CLEANUP STARTED ===")
                try:
                    trade_tracker.cleanup_trades(force_execute=True)
                    log.info("=== EOD CLEANUP COMPLETED SUCCESSFULLY ===")
                except Exception as e:
                    log.error(f"EOD cleanup failed: {e}")
                last_cleanup_date = today  # Mark done even on error to stop retries
                time.sleep(300)  # Sleep 5 min after cleanup
                continue
        except Exception as e:
            log.error(f"EOD scheduler error: {e}")
        time.sleep(60)


def run_web_server():
    uvicorn.run(srv.app, host=config.SERVER_HOST, port=config.SERVER_PORT,
                log_level="warning")


def run_fast_quotes(fyers):
    """Updates index_quotes.json every 5 seconds during market hours."""
    log.info("Fast quotes started (5s).")
    last = {
        "nifty":  {"ltp": 0, "chg_pts": 0, "chg_pct": 0, "high": 0, "low": 0},
        "sensex": {"ltp": 0, "chg_pts": 0, "chg_pct": 0, "high": 0, "low": 0},
        "vix":    {"ltp": 0},
    }
    while True:
        try:
            is_open = scanner._market_open()
            if is_open:
                qs = scanner.fetch_quotes(fyers, INDEX_SYMS, batch_size=10, delay=0.1)
                for sym, key in [(config.NIFTY_SYMBOL, "nifty"),
                                 (config.SENSEX_SYMBOL, "sensex")]:
                    q = qs.get(sym, {})
                    # Use ltp if available; after market close Fyers may return ltp=0
                    # but prev_close always has the last known price
                    raw_ltp = q.get("ltp", 0)
                    pc      = q.get("prev_close", 0)
                    ltp     = raw_ltp if raw_ltp > 0 else pc  # fallback to prev_close
                    if ltp > 0:
                        pts = round(ltp - pc, 2) if pc > 0 else 0
                        pct = round((pts / pc * 100) if pc > 0 else 0, 2)
                        last[key] = {"ltp": round(ltp,2), "chg_pts": pts,
                                     "chg_pct": pct,
                                     "high": round(q.get("high",0),2),
                                     "low":  round(q.get("low",0),2),
                                     "prev_close": round(pc,2)}
                vq = qs.get(config.VIX_SYMBOL, {})
                if vq.get("ltp", 0) > 0:
                    last["vix"] = {"ltp": round(vq["ltp"], 2)}
            with open(QUOTES_FILE, "w") as f:
                json.dump({**last, "market_open": scanner._market_open(),
                           "updated_at": datetime.now().isoformat()}, f)
        except Exception as e:
            log.debug(f"Fast quotes error: {e}")
        time.sleep(5)


def run_bias_updater(fyers):
    """Bias/PCR/SR — runs immediately at start, then every 15 min."""
    log.info("Bias updater started (now + every 15 min).")

    def fetch_bias_for(sym_str, step):
        try:
            q   = scanner.fetch_quotes(fyers, [sym_str], batch_size=5, delay=0.1).get(sym_str, {})
            ltp = q.get("ltp", 0)
            if ltp == 0: return {}
            chain = scanner.build_option_chain(fyers, sym_str, ltp, step, is_index=True)
            df    = scanner.fetch_candles(fyers, sym_str, tf=5, days=5)
            sr    = scanner.calculate_sr(df, ltp)
            rec   = chain.get("recommendation", {}) if chain else {}
            bias  = chain.get("bias", "NO DATA") if chain else "NO DATA"
            return {
                "bias":       bias,
                "bias_note":  rec.get("bias_note", ""),
                "pcr":        chain.get("pcr", 0) if chain else 0,
                "max_pain":   chain.get("max_pain", 0) if chain else 0,
                "support":    sr.get("intraday_support", sr.get("support", 0)),
                "resistance": sr.get("intraday_resistance", sr.get("resistance", 0)),
                "pivot":      sr.get("pivot", 0),
                "s1": sr.get("s1",0), "r1": sr.get("r1",0),
                "s2": sr.get("s2",0), "r2": sr.get("r2",0),
                "intraday_support":    sr.get("intraday_support", 0),
                "intraday_resistance": sr.get("intraday_resistance", 0),
                "updated_at": datetime.now().strftime("%H:%M"),
            }
        except Exception as e:
            log.error(f"Bias error for {sym_str}: {e}")
            return {}

    def do_update():
        log.info("Updating bias/PCR/SR...")
        nifty_bias  = fetch_bias_for(config.NIFTY_SYMBOL,  config.NIFTY_STRIKE_STEP)
        time.sleep(3)
        sensex_bias = fetch_bias_for(config.SENSEX_SYMBOL, config.SENSEX_STRIKE_STEP)
        with open(BIAS_FILE, "w") as f:
            json.dump({"nifty": nifty_bias, "sensex": sensex_bias,
                       "updated_at": datetime.now().isoformat()}, f)
        log.info("Bias updated.")

    if scanner._market_open():
        try: do_update()
        except Exception as e: log.error(f"Bias initial: {e}")

    while True:
        time.sleep(900)
        if scanner._market_open():
            try: do_update()
            except Exception as e: log.error(f"Bias loop: {e}")


def run_scanner_loop(fyers):
    """Full scan every 5 min during market hours."""
    log.info("Scanner loop started (5 min).")
    cleanup_done = False
    while True:
        now   = datetime.now()
        mo    = config.MARKET_OPEN_HOUR  * 60 + config.MARKET_OPEN_MIN
        mc    = config.MARKET_CLOSE_HOUR * 60 + config.MARKET_CLOSE_MIN
        ct    = now.hour * 60 + now.minute
        is_wd = now.weekday() < 5

        # Nightly cleanup at 11 PM — move all today's trades to history
        if is_wd and now.hour >= 23 and not cleanup_done:
            trade_tracker.cleanup_trades()
            cleanup_done = True
        if now.hour < 23:
            cleanup_done = False

        if is_wd and mo <= ct <= mc:
            try:
                payload = scanner.scan_all(fyers)
                scanner.save_cache(payload)
            except Exception as e:
                log.error(f"Scan error: {e}")
        elif is_wd and ct > mc and ct < 23 * 60:
            time.sleep(1800)
            continue

        time.sleep(config.SCAN_INTERVAL_SECONDS)


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "=" * 55)
    print("  OPTIONS RADAR v10")
    print("=" * 55)

    if not os.path.exists(config.ACCESS_TOKEN_FILE):
        print("\n[ERROR] No token. Run: python3 login.py\n"); sys.exit(1)
    with open(config.ACCESS_TOKEN_FILE) as f:
        token = f.read().strip()
    if not token:
        print("\n[ERROR] Token empty. Run: python3 login.py\n"); sys.exit(1)

    print(f"\n  Dashboard → http://localhost:{config.SERVER_PORT}")
    print(f"  Quotes: 5s  |  Bias: now+15min  |  Scan: 5min")
    print(f"  Ctrl+C to stop.\n")

    try:
        fyers = scanner.get_fyers_client()
    except Exception as e:
        print(f"\n[ERROR] {e}\n"); sys.exit(1)

    # 1. Start web server first
    threading.Thread(target=run_web_server, daemon=True).start()
    time.sleep(1.2)

    # 2. Write initial quotes immediately so dashboard shows data on open
    log.info("Writing initial quotes...")
    try:
        qs = scanner.fetch_quotes(fyers, INDEX_SYMS, batch_size=10, delay=0.2)
        init = {
            "nifty":  {"ltp": 0, "chg_pts": 0, "chg_pct": 0, "high": 0, "low": 0},
            "sensex": {"ltp": 0, "chg_pts": 0, "chg_pct": 0, "high": 0, "low": 0},
            "vix":    {"ltp": 0},
        }
        for sym, key in [(config.NIFTY_SYMBOL,"nifty"),(config.SENSEX_SYMBOL,"sensex")]:
            q = qs.get(sym, {})
            raw_ltp = q.get("ltp", 0)
            pc      = q.get("prev_close", 0)
            ltp     = raw_ltp if raw_ltp > 0 else pc  # fallback to prev_close after close
            if ltp > 0:
                pts = round(ltp-pc,2) if pc>0 else 0
                pct = round((pts/pc*100) if pc>0 else 0,2)
                init[key] = {"ltp": round(ltp,2), "chg_pts": pts, "chg_pct": pct,
                             "high": round(q.get("high",0),2), "low": round(q.get("low",0),2),
                             "prev_close": round(pc,2)}
        vq = qs.get(config.VIX_SYMBOL, {})
        if vq.get("ltp", 0) > 0: init["vix"] = {"ltp": round(vq["ltp"],2)}
        is_open = scanner._market_open()
        with open(QUOTES_FILE, "w") as f:
            json.dump({**init, "market_open": is_open,
                       "updated_at": datetime.now().isoformat()}, f)
        log.info(f"Initial quotes written. Market open: {is_open}")
    except Exception as e:
        log.warning(f"Initial quotes: {e}")

    # 3. Open browser before long scan starts
    webbrowser.open(f"http://localhost:{config.SERVER_PORT}")

    # 4. Start fast-quotes thread
    threading.Thread(target=run_fast_quotes, args=(fyers,), daemon=True).start()

    # 5. Run initial full scan (blocks for ~8-10 min due to rate-limit delays)
    log.info("Running initial scan...")
    try:
        payload = scanner.scan_all(fyers)
        scanner.save_cache(payload)
        log.info(f"Initial scan done. {payload['meta']['total']} stocks in radar.")
    except Exception as e:
        log.warning(f"Initial scan: {e}")

    # 6. Start bias updater and continuous scanner loop
    threading.Thread(target=bias_engine.run_bias_engine, args=(fyers,), daemon=True).start()
    threading.Thread(target=learner.run_weekly_learner_loop, daemon=True).start()
    threading.Thread(target=strategy_hougaard.run_hougaard_loop, args=(fyers, True), daemon=True).start()  # Nifty
    threading.Thread(target=strategy_hougaard.run_hougaard_loop, args=(fyers, False), daemon=True).start()  # Sensex
    threading.Thread(target=run_end_of_day_cleanup, daemon=True).start()
    threading.Thread(target=news_fetcher.run_news_loop, daemon=True).start()
    # Analytics engine — gets fyers from a global set after login
    _fyers_ref = [fyers]  # mutable container so analytics loop can read updated token
    threading.Thread(target=analytics.run_analytics_loop, args=(lambda: _fyers_ref[0],), daemon=True).start()

    try:
        run_scanner_loop(fyers)
    except KeyboardInterrupt:
        print("\n\nStopped.\n"); sys.exit(0)


if __name__ == "__main__":
    main()
