"""
strategy_hougaard.py — "Best Loser Wins" Momentum Pyramider
=============================================================
Based on Tom Hougaard's methodology from "Best Loser Wins".
TESTING ONLY — setup name = "Testing" in all trade records.

Strategy Rules:
1. Entry: Opening Range Breakout (first 15-min range)
   - Long:  Price breaks 15-min High + PCR > 1.1 + Price > VWAP
   - Short: Price breaks 15-min Low  + PCR < 0.9 + Price < VWAP
   - Must be consistent with 15-min Bias Engine state

2. Instrument: ATM or slightly ITM weekly expiry options

3. Pyramiding (The Hougaard Add):
   - Start with 25% position (1 lot conceptually)
   - Add 1 unit every 20pts move in Nifty / 50pts in Sensex
   - Only add if position is in unrealized profit
   - Never average down
   - Trail SL to break-even of new entry on each add
   - Max 4 adds (100% total size)

4. Exit (Momentum Exit — no fixed target):
   - Price closes opposite side of 9 EMA
   - 5-min candle closes below prev 5-min low (longs) or above prev 5-min high (shorts)
   - Climax volume spike + reversal candle

5. Initial SL: 15-20% of option premium or entry candle low
"""

import os, json, logging, time
from datetime import datetime, date
from collections import defaultdict

log = logging.getLogger("hougaard")

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
HOUGAARD_FILE = os.path.join(BASE_DIR, "hougaard_trades.json")

NIFTY_ADD_PTS   = 20   # add unit every 20 Nifty points
SENSEX_ADD_PTS  = 50   # add unit every 50 Sensex points
MAX_UNITS       = 4    # max 4 units total
INITIAL_UNITS   = 1    # start with 1 unit (25% of max)
SL_PCT          = 0.18 # 18% initial SL on premium


def _load_trades():
    if not os.path.exists(HOUGAARD_FILE): return {}
    try:
        with open(HOUGAARD_FILE) as f: return json.load(f)
    except: return {}

def _save_trades(t):
    with open(HOUGAARD_FILE, "w") as f: json.dump(t, f, indent=2, default=str)


def _get_orb(df_5min):
    """Get Opening Range Breakout levels from first 15 min (first 3 x 5-min candles)."""
    if df_5min.empty or len(df_5min) < 3:
        return None, None
    first_3 = df_5min.iloc[:3]
    orb_high = float(first_3["high"].max())
    orb_low  = float(first_3["low"].min())
    return orb_high, orb_low


def _calc_ema_list(series, period):
    return series.ewm(span=period, adjust=False).mean()


def check_entry_signal(fyers, sym_str, is_nifty=True):
    """
    Check if opening range breakout entry conditions are met.
    Returns (signal: 'LONG'|'SHORT'|None, reason: str)
    """
    try:
        import scanner as sc
        import config
        import bias_engine

        qs  = sc.fetch_quotes(fyers, [sym_str], batch_size=2, delay=0.1)
        q   = qs.get(sym_str, {})
        ltp = float(q.get("ltp") or q.get("prev_close") or 0)
        if ltp == 0: return None, "No LTP"

        # Need 5-min candles for ORB and momentum exit check
        df5 = sc.fetch_candles(fyers, sym_str, tf=5, days=3)
        if df5.empty or len(df5) < 6:
            return None, "Insufficient 5-min data"

        # Today's candles only
        today_df = df5[df5["datetime"].dt.date == date.today()]
        if len(today_df) < 3:
            return None, "Need at least 3 candles (15 min) for ORB"

        orb_high, orb_low = _get_orb(today_df)
        if orb_high is None: return None, "ORB not yet formed"

        # VWAP
        vwap = sc._calc_vwap(today_df)

        # Option chain for PCR
        step  = config.NIFTY_STRIKE_STEP if is_nifty else config.SENSEX_STRIKE_STEP
        chain = sc.build_option_chain(fyers, sym_str, ltp, step, is_index=True)
        pcr   = float(chain.get("pcr", 1.0)) if chain else 1.0

        # Bias Engine state
        key   = "nifty" if is_nifty else "sensex"
        bias  = bias_engine.get_current_bias(key)
        state = bias.get("state", "SIDEWAYS")

        # ── Long signal ──────────────────────────────────────────────────────
        if (ltp > orb_high and
            pcr > 1.1 and
            ltp > vwap and
            state in ("BULLISH", "EXTREMELY BULLISH")):
            reason = (f"ORB Long: LTP {ltp} broke ORB high {orb_high} | "
                      f"PCR {pcr:.2f} > 1.1 | VWAP {vwap} | Bias {state}")
            return "LONG", reason

        # ── Short signal ─────────────────────────────────────────────────────
        if (ltp < orb_low and
            pcr < 0.9 and
            ltp < vwap and
            state in ("BEARISH", "EXTREMELY BEARISH")):
            reason = (f"ORB Short: LTP {ltp} broke ORB low {orb_low} | "
                      f"PCR {pcr:.2f} < 0.9 | VWAP {vwap} | Bias {state}")
            return "SHORT", reason

        return None, f"No breakout. ORB: {orb_low}-{orb_high} | LTP: {ltp} | PCR: {pcr:.2f} | Bias: {state}"

    except Exception as e:
        log.error(f"Hougaard entry check error: {e}")
        return None, str(e)


def execute_entry(fyers, sym_str, signal, is_nifty=True):
    """
    Execute initial entry if signal is valid.
    Buys ATM or slightly ITM option.
    Returns trade record or None.
    """
    try:
        import scanner as sc
        import config

        qs  = sc.fetch_quotes(fyers, [sym_str], batch_size=2, delay=0.1)
        q   = qs.get(sym_str, {})
        ltp = float(q.get("ltp") or 0)
        if ltp == 0: return None

        is_nifty_sym = is_nifty
        base  = "NIFTY" if is_nifty_sym else "SENSEX"
        exch  = "NSE" if is_nifty_sym else "BSE"
        step  = config.NIFTY_STRIKE_STEP if is_nifty_sym else config.SENSEX_STRIKE_STEP

        expiry = sc.get_nifty_expiries(1)[0] if is_nifty_sym else sc.get_sensex_expiries(1)[0]
        atm    = round(ltp / step) * step

        opt_type = "CE" if signal == "LONG" else "PE"
        # Slightly ITM for better delta (1 strike ITM)
        if signal == "LONG":
            strike = atm - step  # 1 strike ITM for CE
        else:
            strike = atm + step  # 1 strike ITM for PE

        ask, bid, ltp_opt, sym_used = sc.get_live_option_price(fyers, base, exch, expiry, strike, opt_type)
        if ask == 0:
            # Fallback to ATM
            ask, bid, ltp_opt, sym_used = sc.get_live_option_price(fyers, base, exch, expiry, atm, opt_type)
            strike = atm

        if ask == 0:
            log.warning(f"Hougaard: No option price for {base} {strike} {opt_type}")
            return None

        entry    = ask
        sl_price = round(entry * (1 - SL_PCT), 1)
        given_at = datetime.now().strftime("%H:%M")

        trade = {
            "strategy":   "Testing",      # Setup name per spec
            "setup_name": "Testing — Hougaard Momentum Pyramider",
            "symbol":     base + "50" if is_nifty else base,
            "direction":  signal,
            "opt_type":   opt_type,
            "strike":     strike,
            "atm":        atm,
            "expiry":     sc.fmt_exp(expiry),
            "entry":      entry,
            "sl_price":   sl_price,
            "tgt_price":  None,           # No fixed target — momentum exit
            "units":      INITIAL_UNITS,
            "max_units":  MAX_UNITS,
            "add_levels": [],             # spot price levels where units were added
            "avg_entry":  entry,
            "status":     "ACTIVE",
            "given_at":   given_at,
            "date":       date.today().isoformat(),
            "spot_at_entry": ltp,
            "ltp_option": ltp_opt,
            "add_trigger_pts": NIFTY_ADD_PTS if is_nifty else SENSEX_ADD_PTS,
            "is_nifty":   is_nifty,
            "exits":      [],
        }

        trades = _load_trades()
        key    = f"{base}_{date.today().isoformat()}"
        trades[key] = trade
        _save_trades(trades)
        log.info(f"Hougaard: Entry {signal} {base} {strike}{opt_type} @ ₹{entry} | SL ₹{sl_price}")
        return trade

    except Exception as e:
        log.error(f"Hougaard execute_entry error: {e}")
        return None


def check_add_or_exit(fyers, sym_str, is_nifty=True):
    """
    Check if we should add to position or exit.
    Called every 5 minutes during market hours.
    """
    try:
        import scanner as sc
        import config

        base = "NIFTY" if is_nifty else "SENSEX"
        key  = f"{base}_{date.today().isoformat()}"
        trades = _load_trades()
        if key not in trades: return

        t = trades[key]
        if t["status"] != "ACTIVE": return

        qs  = sc.fetch_quotes(fyers, [sym_str], batch_size=2, delay=0.1)
        q   = qs.get(sym_str, {})
        spot = float(q.get("ltp") or 0)
        if spot == 0: return

        expiry_dt = None
        try:
            from datetime import date as d_cls
            yr  = int("20" + t["expiry"][5:7])
            mn  = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                   "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}[t["expiry"][2:5]]
            dy  = int(t["expiry"][:2])
            expiry_dt = d_cls(yr, mn, dy)
        except: pass

        step = config.NIFTY_STRIKE_STEP if is_nifty else config.SENSEX_STRIKE_STEP
        ask, bid, ltp_opt, _ = sc.get_live_option_price(
            fyers, base, "NSE" if is_nifty else "BSE",
            expiry_dt or sc.get_nifty_expiries(1)[0],
            t["strike"], t["opt_type"]
        )

        # ── SL Check ──────────────────────────────────────────────────────────
        if ltp_opt > 0 and ltp_opt <= t["sl_price"]:
            t["status"]    = "SL_HIT"
            t["exit_price"] = ltp_opt
            t["exit_time"]  = datetime.now().strftime("%H:%M")
            t["pnl_pts"]    = round(ltp_opt - t["avg_entry"], 2)
            trades[key]     = t
            _save_trades(trades)
            log.info(f"Hougaard SL HIT: {base} {t['strike']}{t['opt_type']} @ ₹{ltp_opt}")
            return

        # ── Momentum Exit Check (5-min candles) ───────────────────────────────
        df5 = sc.fetch_candles(fyers, sym_str, tf=5, days=2)
        if not df5.empty and len(df5) >= 3:
            today_df = df5[df5["datetime"].dt.date == date.today()]
            if len(today_df) >= 3:
                ema9 = sc._calc_ema_list if hasattr(sc, "_calc_ema_list") else None
                closes = today_df["close"]
                ema9_series = closes.ewm(span=9, adjust=False).mean()
                curr_close  = float(closes.iloc[-1])
                curr_ema9   = float(ema9_series.iloc[-1])
                prev_low    = float(today_df["low"].iloc[-2])
                prev_high   = float(today_df["high"].iloc[-2])

                exit_reason = None
                if t["direction"] == "LONG":
                    if curr_close < curr_ema9:
                        exit_reason = "Close below 9 EMA"
                    elif curr_close < prev_low:
                        exit_reason = "Close below prev 5-min low"
                else:  # SHORT
                    if curr_close > curr_ema9:
                        exit_reason = "Close above 9 EMA"
                    elif curr_close > prev_high:
                        exit_reason = "Close above prev 5-min high"

                if exit_reason:
                    t["status"]     = "MOMENTUM_EXIT"
                    t["exit_price"] = ltp_opt if ltp_opt > 0 else ask
                    t["exit_time"]  = datetime.now().strftime("%H:%M")
                    t["exit_reason"] = exit_reason
                    t["pnl_pts"]    = round((t["exit_price"] or 0) - t["avg_entry"], 2)
                    trades[key]     = t
                    _save_trades(trades)
                    log.info(f"Hougaard MOMENTUM EXIT: {base} — {exit_reason} | PnL: ₹{t['pnl_pts']}")
                    return

        # ── Add Unit Check ────────────────────────────────────────────────────
        add_pts = t["add_trigger_pts"]
        units   = t["units"]
        if units >= MAX_UNITS: return
        if ltp_opt <= 0 or ask <= 0: return

        spot_entry = t["spot_at_entry"]
        spot_moves = spot - spot_entry if t["direction"] == "LONG" else spot_entry - spot
        required_adds = int(spot_moves / add_pts)
        current_adds  = len(t["add_levels"])

        if required_adds > current_adds and ltp_opt > t["avg_entry"]:
            # Add 1 unit
            new_units    = units + 1
            new_avg      = round((t["avg_entry"] * units + ask) / new_units, 2)
            new_sl       = round(new_avg * (1 - SL_PCT * 0.5), 1)  # tighter SL after add

            t["add_levels"].append({
                "spot": spot, "option_ask": ask, "units_after": new_units,
                "new_avg": new_avg, "new_sl": new_sl, "time": datetime.now().strftime("%H:%M")
            })
            t["units"]     = new_units
            t["avg_entry"] = new_avg
            t["sl_price"]  = new_sl  # trail SL to break-even of new entry
            t["ltp_option"] = ltp_opt
            trades[key]    = t
            _save_trades(trades)
            log.info(f"Hougaard ADD #{new_units}: {base} spot@{spot} | opt ask ₹{ask} | "
                     f"avg ₹{new_avg} | new SL ₹{new_sl}")

        elif spot_moves > 0:
            # Update LTP
            t["ltp_option"] = ltp_opt
            trades[key] = t
            _save_trades(trades)

    except Exception as e:
        log.error(f"Hougaard check_add_or_exit error: {e}")


def get_active_trades():
    """Returns today's Hougaard trades for display."""
    trades = _load_trades()
    today  = date.today().isoformat()
    return {k: v for k, v in trades.items() if v.get("date") == today}


def get_all_trades():
    """All historical Hougaard trades for analysis."""
    return _load_trades()


def run_hougaard_loop(fyers, is_nifty=True):
    """
    Background loop: checks entry at open, then monitors for adds/exits every 5 min.
    Separate threads for Nifty and Sensex.
    """
    import config
    sym_str = config.NIFTY_SYMBOL if is_nifty else config.SENSEX_SYMBOL
    base    = "NIFTY" if is_nifty else "SENSEX"
    name    = "NIFTY50" if is_nifty else "SENSEX"
    log.info(f"Hougaard loop started for {name}.")

    entry_checked = False
    last_date     = None

    while True:
        try:
            now = datetime.now()
            today = date.today().isoformat()

            # Reset daily
            if last_date != today:
                entry_checked = False
                last_date = today

            total_mins = now.hour * 60 + now.minute

            # Only run during market hours
            if total_mins < 9*60+15 or total_mins > 15*60+30:
                time.sleep(60)
                continue

            # Check entry after first 15 min (after ORB forms)
            if total_mins >= 9*60+30 and not entry_checked:
                trades = get_active_trades()
                key    = f"{base}_{today}"
                if key not in trades:
                    signal, reason = check_entry_signal(fyers, sym_str, is_nifty)
                    log.info(f"Hougaard {name}: {signal or 'No signal'} — {reason[:80]}")
                    if signal:
                        execute_entry(fyers, sym_str, signal, is_nifty)
                entry_checked = True

            # Monitor every 5 min
            if total_mins >= 9*60+30:
                check_add_or_exit(fyers, sym_str, is_nifty)

        except Exception as e:
            log.error(f"Hougaard loop error {base}: {e}")

        time.sleep(300)  # 5-min cycle
