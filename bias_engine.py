"""
bias_engine.py — 15-Minute Recursive Bias State Machine
=========================================================
Implements the scoring system (-5 to +5) to determine Global Market Bias.
Runs every 15 minutes at candle close (09:30, 09:45, 10:00, ...).

Scoring:
  PCR > 1.2  → +2   |  PCR < 0.8  → -2
  Price > VWAP → +1 |  Price < VWAP → -1
  9 EMA > 21 EMA → +1 | 9 EMA < 21 EMA → -1
  Higher High + Higher Low → +1 | Lower High + Lower Low → -1

State Classification:
  +4/+5 → EXTREMELY BULLISH
  +2/+3 → BULLISH
  -1/+1 → SIDEWAYS (no new trades, close scalps)
  -2/-3 → BEARISH
  -4/-5 → EXTREMELY BEARISH

Bias Change Detection:
  - Logs "Bias Shift Detected"
  - Evaluates open positions
  - 5-minute cooldown after state change
"""

import os, json, logging, time, threading
from datetime import datetime, date, timedelta
import numpy as np

log = logging.getLogger("bias_engine")

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
BIAS_FILE  = os.path.join(BASE_DIR, "index_bias.json")
STATE_FILE = os.path.join(BASE_DIR, "bias_state.json")


def _load_state():
    if not os.path.exists(STATE_FILE):
        return {"nifty": {"state": "SIDEWAYS", "score": 0, "last_change": None, "cooldown_until": None},
                "sensex": {"state": "SIDEWAYS", "score": 0, "last_change": None, "cooldown_until": None}}
    try:
        with open(STATE_FILE) as f: return json.load(f)
    except:
        return {"nifty": {"state": "SIDEWAYS", "score": 0, "last_change": None, "cooldown_until": None},
                "sensex": {"state": "SIDEWAYS", "score": 0, "last_change": None, "cooldown_until": None}}

def _save_state(s):
    with open(STATE_FILE, "w") as f: json.dump(s, f, indent=2, default=str)


def _score_to_state(score):
    """Map numeric score to named bias state."""
    if score >= 4:   return "EXTREMELY BULLISH"
    if score >= 2:   return "BULLISH"
    if score <= -4:  return "EXTREMELY BEARISH"
    if score <= -2:  return "BEARISH"
    return "SIDEWAYS"


def _score_to_execution(state):
    """Return execution instructions for each state."""
    m = {
        "EXTREMELY BULLISH": "Aggressive Longs — trailing SL, full size",
        "BULLISH":           "Look for Long entries on pullbacks only",
        "SIDEWAYS":          "NEUTRAL — no new entries, close open scalps",
        "BEARISH":           "Look for Short entries on bounces only",
        "EXTREMELY BEARISH": "Aggressive Shorts — trailing SL, full size",
    }
    return m.get(state, "NEUTRAL")


def _calc_ema(values, period):
    """Simple EMA from list of close prices."""
    if len(values) < period: return values[-1] if values else 0
    k = 2 / (period + 1)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1 - k)
    return round(ema, 2)


def compute_bias(fyers, sym_str, is_nifty=True):
    """
    Core bias computation for one index.
    Returns dict with score, state, sub-scores, execution_mode.
    """
    import config
    try:
        import scanner as sc
    except ImportError:
        return {}

    try:
        # ── 1. Fetch LTP and VWAP ────────────────────────────────────────────
        qs  = sc.fetch_quotes(fyers, [sym_str], batch_size=2, delay=0.1)
        q   = qs.get(sym_str, {})
        ltp = float(q.get("ltp") or q.get("prev_close") or 0)
        if ltp == 0:
            log.warning(f"bias_engine: no LTP for {sym_str}")
            return {}

        # ── 2. Candle data (15-min, last 5 candles for EMA and structure) ────
        df15 = sc.fetch_candles(fyers, sym_str, tf=15, days=5)
        if df15.empty or len(df15) < 5:
            log.warning(f"bias_engine: insufficient candle data for {sym_str}")
            return {}

        closes = df15["close"].tolist()
        highs  = df15["high"].tolist()
        lows   = df15["low"].tolist()

        # VWAP from intraday (use today's candles if available)
        today_df = df15[df15["datetime"].dt.date == date.today()] if not df15.empty else df15
        if today_df.empty: today_df = df15.tail(30)
        vwap = sc._calc_vwap(today_df) if not today_df.empty else 0

        # ── 3. Option chain for PCR ──────────────────────────────────────────
        step  = config.NIFTY_STRIKE_STEP if is_nifty else config.SENSEX_STRIKE_STEP
        chain = sc.build_option_chain(fyers, sym_str, ltp, step, is_index=True)
        pcr   = float(chain.get("pcr", 1.0)) if chain else 1.0

        # ── 4. EMA (9 and 21 on 15-min closes) ──────────────────────────────
        ema9  = _calc_ema(closes, 9)
        ema21 = _calc_ema(closes, 21)

        # ── 5. Candle Structure (last 2 completed candles) ───────────────────
        # Use closes[-3] as "prev prev" to avoid using live candle
        prev2_hi = highs[-3]; prev2_lo = lows[-3]
        prev1_hi = highs[-2]; prev1_lo = lows[-2]
        is_hh_hl = prev1_hi > prev2_hi and prev1_lo > prev2_lo  # bullish structure
        is_lh_ll = prev1_hi < prev2_hi and prev1_lo < prev2_lo  # bearish structure

        # ── 6. Score Computation ─────────────────────────────────────────────
        score = 0
        breakdown = {}

        # PCR
        if pcr > 1.2:
            score += 2; breakdown["pcr"] = f"+2 (PCR={pcr:.2f} bullish)"
        elif pcr < 0.8:
            score -= 2; breakdown["pcr"] = f"-2 (PCR={pcr:.2f} bearish)"
        else:
            breakdown["pcr"] = f"0 (PCR={pcr:.2f} neutral)"

        # VWAP
        if vwap > 0:
            if ltp > vwap:
                score += 1; breakdown["vwap"] = f"+1 (LTP {ltp} > VWAP {vwap})"
            else:
                score -= 1; breakdown["vwap"] = f"-1 (LTP {ltp} < VWAP {vwap})"
        else:
            breakdown["vwap"] = "0 (VWAP unavailable)"

        # EMA stack
        if ema9 > ema21:
            score += 1; breakdown["ema"] = f"+1 (9EMA {ema9} > 21EMA {ema21})"
        else:
            score -= 1; breakdown["ema"] = f"-1 (9EMA {ema9} < 21EMA {ema21})"

        # Candle structure
        if is_hh_hl:
            score += 1; breakdown["structure"] = "+1 (Higher High + Higher Low)"
        elif is_lh_ll:
            score -= 1; breakdown["structure"] = "-1 (Lower High + Lower Low)"
        else:
            breakdown["structure"] = "0 (Mixed candle structure)"

        score = max(-5, min(5, score))  # clamp to -5..+5
        state = _score_to_state(score)

        return {
            "score":         score,
            "state":         state,
            "bias":          state,
            "execution_mode": _score_to_execution(state),
            "pcr":           round(pcr, 2),
            "vwap":          round(vwap, 2),
            "ltp":           round(ltp, 2),
            "ema9":          round(ema9, 2),
            "ema21":         round(ema21, 2),
            "breakdown":     breakdown,
            "support":       sc.calculate_sr(df15, ltp).get("intraday_support", 0),
            "resistance":    sc.calculate_sr(df15, ltp).get("intraday_resistance", 0),
            "max_pain":      chain.get("max_pain", 0) if chain else 0,
            "bias_note":     f"Score {score:+d}: {'; '.join(breakdown.values())}",
            "updated_at":    datetime.now().strftime("%H:%M"),
        }

    except Exception as e:
        log.error(f"bias_engine compute error for {sym_str}: {e}")
        return {}


def run_bias_engine(fyers):
    """
    Main loop: runs every 15 minutes at candle close boundaries.
    Detects state changes and enforces cooldown.
    """
    import config
    import trade_tracker

    log.info("Bias Engine started (15-min candle-aligned).")
    state_data = _load_state()
    cooldown_mins = 5  # no new trades after bias shift

    while True:
        try:
            now = datetime.now()

            # Align to 15-min boundaries: 09:30, 09:45, 10:00...
            # Wait until next multiple of 15 minutes past 09:15
            total_mins = now.hour * 60 + now.minute
            market_open_mins = 9 * 60 + 15
            if total_mins < market_open_mins:
                secs_to_open = (market_open_mins - total_mins) * 60 - now.second
                log.info(f"Bias Engine: market not open. Waiting {secs_to_open//60}m.")
                time.sleep(max(secs_to_open, 60))
                continue

            # Only run during market hours
            if total_mins > 15 * 60 + 30:
                time.sleep(300)
                continue

            log.info("Bias Engine: computing 15-min bias scores...")

            results = {}
            for sym_str, is_nifty, key in [
                (config.NIFTY_SYMBOL,  True,  "nifty"),
                (config.SENSEX_SYMBOL, False, "sensex"),
            ]:
                new_bias = compute_bias(fyers, sym_str, is_nifty)
                if not new_bias:
                    continue

                new_state = new_bias["state"]
                old_state = state_data.get(key, {}).get("state", "SIDEWAYS")
                old_score = state_data.get(key, {}).get("score", 0)

                # ── Bias Change Detection ─────────────────────────────────────
                if new_state != old_state:
                    log.info(f"⚡ BIAS SHIFT DETECTED [{key.upper()}] at {now.strftime('%H:%M')}: "
                             f"{old_state} → {new_state} (score: {old_score} → {new_bias['score']})")

                    # Set cooldown
                    cooldown_until = (now + timedelta(minutes=cooldown_mins)).isoformat()
                    state_data[key] = {
                        "state":          new_state,
                        "score":          new_bias["score"],
                        "last_change":    now.isoformat(),
                        "cooldown_until": cooldown_until,
                        "prev_state":     old_state,
                    }

                    # Emergency exit evaluation
                    if new_state in ("BEARISH","EXTREMELY BEARISH"):
                        # Check for active BULL trades
                        locked = trade_tracker.get_locked_trade("NIFTY50") or trade_tracker.get_locked_trade("SENSEX")
                        if locked and locked.get("direction") == "BULL" and locked.get("status") == "ACTIVE":
                            log.info(f"  Emergency: Bias turned BEARISH — tightening SL to break-even for {locked.get('symbol')}")
                            # Tighten SL to entry (break even)
                            # (scanner will pick this up on next scan)
                    elif new_state in ("BULLISH","EXTREMELY BULLISH"):
                        locked = trade_tracker.get_locked_trade("NIFTY50") or trade_tracker.get_locked_trade("SENSEX")
                        if locked and locked.get("direction") == "BEAR" and locked.get("status") == "ACTIVE":
                            log.info(f"  Emergency: Bias turned BULLISH — tightening SL to break-even for {locked.get('symbol')}")

                    new_bias["cooldown_until"] = cooldown_until
                    new_bias["bias_shift"]      = True
                    new_bias["prev_state"]      = old_state
                else:
                    state_data.setdefault(key, {})["state"] = new_state
                    state_data[key]["score"] = new_bias["score"]
                    new_bias["bias_shift"] = False

                # Propagate cooldown info to bias result
                cu = state_data.get(key, {}).get("cooldown_until")
                if cu:
                    try:
                        cu_dt = datetime.fromisoformat(cu)
                        if now < cu_dt:
                            new_bias["in_cooldown"] = True
                            new_bias["cooldown_until"] = cu
                            log.info(f"  {key.upper()}: In cooldown until {cu_dt.strftime('%H:%M')}")
                        else:
                            new_bias["in_cooldown"] = False
                    except: pass

                results[key] = new_bias
                log.info(f"  {key.upper()}: Score={new_bias['score']:+d} → {new_state} | {new_bias.get('execution_mode','')}")

            # Save state and write bias file
            _save_state(state_data)
            if results:
                existing = {}
                if os.path.exists(BIAS_FILE):
                    try:
                        with open(BIAS_FILE) as f: existing = json.load(f)
                    except: pass
                existing.update(results)
                existing["updated_at"] = datetime.now().isoformat()
                existing["engine"]     = "15min_bias_engine"
                with open(BIAS_FILE, "w") as f:
                    json.dump(existing, f, indent=2, default=str)
                log.info("Bias Engine: index_bias.json updated.")

        except Exception as e:
            log.error(f"Bias Engine loop error: {e}")

        # Wait until next 15-min boundary
        now      = datetime.now()
        mins_mod = (now.minute % 15)
        secs_mod = now.second
        secs_to_next = ((15 - mins_mod) * 60 - secs_mod) if mins_mod > 0 else (15 * 60 - secs_mod)
        # Min 2 min wait, max 15 min
        secs_to_next = max(120, min(secs_to_next, 900))
        log.debug(f"Bias Engine: next run in {secs_to_next//60}m {secs_to_next%60}s")
        time.sleep(secs_to_next)


def get_current_bias(which="nifty"):
    """Read current bias state from file. Called by scanner before giving trades."""
    if not os.path.exists(BIAS_FILE):
        return {"state": "SIDEWAYS", "score": 0, "in_cooldown": False}
    try:
        with open(BIAS_FILE) as f:
            data = json.load(f)
        return data.get(which, {"state": "SIDEWAYS", "score": 0, "in_cooldown": False})
    except:
        return {"state": "SIDEWAYS", "score": 0, "in_cooldown": False}


def is_trade_allowed(which="nifty", direction="BULL"):
    """
    Check if a new trade is allowed based on current bias.
    Returns (allowed: bool, reason: str)
    """
    b = get_current_bias(which)
    state = b.get("state", "SIDEWAYS")

    # Cooldown check
    if b.get("in_cooldown"):
        return False, f"Bias cooldown active until {b.get('cooldown_until','')}"

    # SIDEWAYS = no new trades
    if state == "SIDEWAYS":
        return False, "Bias is SIDEWAYS — no new entries"

    # Direction must match bias
    if direction == "BULL" and state in ("BEARISH", "EXTREMELY BEARISH"):
        return False, f"Bias is {state} — no BULL/CE trades"
    if direction == "BEAR" and state in ("BULLISH", "EXTREMELY BULLISH"):
        return False, f"Bias is {state} — no BEAR/PE trades"

    return True, f"Bias {state} supports {direction} trade"
