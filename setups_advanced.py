"""
setups_advanced.py — High & Medium Priority Institutional Setups
================================================================
Adds 6 new setups to the Options Radar scanner:

HIGH PRIORITY:
  1. Liquidity Sweep + Reversal (enhanced — equal highs/lows detection)
  2. Higher Timeframe Daily S/R Level + 5-min trigger
  3. PCR Extreme Reversal (contrarian)

MEDIUM PRIORITY:
  4. VIX Divergence (price vs VIX mismatch)
  5. FII Options Positioning (daily NSE data)
  6. 0DTE Gamma Scalp rules (expiry-day tightening)

Each detector returns: {signal, name, score_bonus, meta}
score_bonus is added ON TOP of the base setup score.
"""

import os, json, logging, time, requests
from datetime import datetime, date, timedelta
import pandas as pd
import numpy as np

log = logging.getLogger("setups_adv")

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
FII_CACHE = os.path.join(BASE_DIR, "fii_cache.json")

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept":     "application/json, text/plain, */*",
    "Referer":    "https://www.nseindia.com",
}

# ═══════════════════════════════════════════════════════════════════════════════
# 1. LIQUIDITY SWEEP + REVERSAL (ENHANCED)
# ═══════════════════════════════════════════════════════════════════════════════

def det_liq_sweep_v2(df: pd.DataFrame) -> dict:
    """
    Enhanced liquidity sweep detector.

    Original _det_sweep was too simple — just checked last 2 candles.
    This version:
    1. Identifies EQUAL HIGHS / EQUAL LOWS (buy-side / sell-side liquidity pools)
       — within 0.1% of each other across last 20 candles
    2. Detects when price sweeps THROUGH that level (wick beyond it)
    3. Confirms reversal: close BACK inside the range
    4. Requires rejection candle after sweep (pin bar / engulfing)

    Why this is better: Institutions specifically target equal highs/lows because
    retail traders pile stop losses there. The sweep = institutions filling orders
    then driving price the OTHER way.

    Score bonus: +1.5 (highest conviction reversal signal)
    """
    empty = {"signal": None, "name": "", "score_bonus": 0, "meta": {}}
    if len(df) < 15:
        return empty

    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    o = df["open"].values
    n = len(df)

    # ── Find equal highs (within 0.1%) in last 20 candles ────────────────────
    lookback = min(20, n - 3)
    recent_highs = h[n - lookback - 2: n - 2]
    recent_lows  = l[n - lookback - 2: n - 2]

    if len(recent_highs) < 5:
        return empty

    max_high = recent_highs.max()
    min_low  = recent_lows.min()
    tol_h    = max_high * 0.001   # 0.1% tolerance for "equal" highs
    tol_l    = min_low  * 0.001

    # Count how many highs are near the max high (equal highs = liquidity pool)
    equal_highs_count = sum(1 for x in recent_highs if abs(x - max_high) <= tol_h)
    equal_lows_count  = sum(1 for x in recent_lows  if abs(x - min_low)  <= tol_l)

    last2_h = h[n - 2]
    last2_l = l[n - 2]
    last2_c = c[n - 2]
    last1_h = h[n - 1]
    last1_l = l[n - 1]
    last1_c = c[n - 1]
    last1_o = o[n - 1]
    last2_o = o[n - 2]

    # ── BULLISH sweep: swept BELOW equal lows, then closed back above ─────────
    if equal_lows_count >= 2:
        # Sweep candle went below the equal lows pool
        if last2_l < min_low and last2_c > min_low:
            # Reversal confirmed — last candle bullish close
            if last1_c > last1_o:
                # Rejection strength: how far it swept vs body
                sweep_depth = min_low - last2_l
                body_size   = abs(last1_c - last1_o)
                if sweep_depth > 0:
                    return {
                        "signal":      "BULL",
                        "name":        "Liquidity Sweep + Reversal (Bull)",
                        "score_bonus": 1.5,
                        "meta": {
                            "swept_level": round(min_low, 2),
                            "sweep_depth": round(sweep_depth, 2),
                            "equal_lows":  equal_lows_count,
                        }
                    }

    # ── BEARISH sweep: swept ABOVE equal highs, then closed back below ────────
    if equal_highs_count >= 2:
        if last2_h > max_high and last2_c < max_high:
            if last1_c < last1_o:
                sweep_depth = last2_h - max_high
                if sweep_depth > 0:
                    return {
                        "signal":      "BEAR",
                        "name":        "Liquidity Sweep + Reversal (Bear)",
                        "score_bonus": 1.5,
                        "meta": {
                            "swept_level": round(max_high, 2),
                            "sweep_depth": round(sweep_depth, 2),
                            "equal_highs": equal_highs_count,
                        }
                    }

    return empty


# ═══════════════════════════════════════════════════════════════════════════════
# 2. HIGHER TIMEFRAME (DAILY) S/R + 5-MIN TRIGGER
# ═══════════════════════════════════════════════════════════════════════════════

def calc_daily_levels(df_daily: pd.DataFrame, ltp: float) -> dict:
    """
    Compute key daily-timeframe S/R levels:
    - Weekly High/Low (last 5 trading days)
    - Monthly High/Low (last 22 trading days)
    - Previous Day High/Low (PDH/PDL)
    - Key psychological levels (round numbers within 2%)
    Returns dict of levels closest to current price.
    """
    if df_daily.empty or len(df_daily) < 5:
        return {}

    try:
        df = df_daily.copy()
        df["date"] = df["datetime"].dt.date
        days = sorted(df["date"].unique())

        # Previous Day
        pdh = pdl = 0
        if len(days) >= 2:
            prev = df[df["date"] == days[-2]]
            if not prev.empty:
                pdh = float(prev["high"].max())
                pdl = float(prev["low"].min())

        # Weekly (last 5 days)
        wk_data = df[df["date"].isin(days[-5:])] if len(days) >= 5 else df
        wkh = float(wk_data["high"].max())
        wkl = float(wk_data["low"].min())

        # Monthly (last 22 days)
        mo_data = df[df["date"].isin(days[-22:])] if len(days) >= 22 else df
        moh = float(mo_data["high"].max())
        mol = float(mo_data["low"].min())

        # Key psychological round levels within 3% of current price
        psych_levels = []
        if ltp > 0:
            magnitude = 10 ** (len(str(int(ltp))) - 2)  # e.g. for 1460 → 100
            base = round(ltp / magnitude) * magnitude
            for mult in range(-5, 6):
                lvl = base + mult * magnitude
                if abs(lvl - ltp) / ltp <= 0.03 and lvl > 0:
                    psych_levels.append(round(lvl, 2))

        return {
            "pdh": round(pdh, 2),
            "pdl": round(pdl, 2),
            "weekly_high": round(wkh, 2),
            "weekly_low":  round(wkl, 2),
            "monthly_high": round(moh, 2),
            "monthly_low":  round(mol, 2),
            "psych_levels": psych_levels,
        }
    except Exception as e:
        log.debug(f"Daily levels error: {e}")
        return {}


def det_htf_level_entry(df_5min: pd.DataFrame, daily_levels: dict, ltp: float) -> dict:
    """
    Higher Timeframe Level + 5-min Trigger.

    Logic:
    1. Price is near a key daily level (within 0.3%)
    2. Last 5-min candle shows entry trigger (pin bar, engulfing, or rejection)
    3. Signal: buy CE at daily support, buy PE at daily resistance

    Score bonus: +1.2 (institutional level = high conviction)
    """
    empty = {"signal": None, "name": "", "score_bonus": 0, "meta": {}}
    if not daily_levels or df_5min.empty or len(df_5min) < 3:
        return empty
    if ltp <= 0:
        return empty

    tolerance = ltp * 0.003  # 0.3% proximity to level

    # Check resistance levels (PE opportunity)
    resistance_levels = {
        "PDH":          daily_levels.get("pdh", 0),
        "Weekly High":  daily_levels.get("weekly_high", 0),
        "Monthly High": daily_levels.get("monthly_high", 0),
    }
    # Check support levels (CE opportunity)
    support_levels = {
        "PDL":          daily_levels.get("pdl", 0),
        "Weekly Low":   daily_levels.get("weekly_low", 0),
        "Monthly Low":  daily_levels.get("monthly_low", 0),
    }

    # Check psychological levels
    for lvl in daily_levels.get("psych_levels", []):
        if lvl > ltp:
            resistance_levels[f"Psychological {lvl}"] = lvl
        else:
            support_levels[f"Psychological {lvl}"] = lvl

    last_c = df_5min.iloc[-1]
    last_p = df_5min.iloc[-2]

    # Trigger: rejection or engulfing candle at the level
    body   = abs(last_c["close"] - last_c["open"])
    total  = last_c["high"] - last_c["low"]
    if total == 0:
        return empty

    upper_wick = last_c["high"] - max(last_c["close"], last_c["open"])
    lower_wick = min(last_c["close"], last_c["open"]) - last_c["low"]

    # Near resistance → look for bearish rejection
    for level_name, level_val in resistance_levels.items():
        if level_val <= 0: continue
        if abs(ltp - level_val) <= tolerance and ltp <= level_val * 1.003:
            # Bearish trigger: upper wick rejection or bearish candle
            is_bear_trigger = (upper_wick / total > 0.45) or (last_c["close"] < last_c["open"] and last_c["close"] < last_p["close"])
            if is_bear_trigger:
                return {
                    "signal":      "BEAR",
                    "name":        f"HTF Level Rejection ({level_name})",
                    "score_bonus": 1.2,
                    "meta": {
                        "level":      level_val,
                        "level_name": level_name,
                        "ltp":        ltp,
                        "proximity":  round((level_val - ltp) / level_val * 100, 2),
                    }
                }

    # Near support → look for bullish bounce
    for level_name, level_val in support_levels.items():
        if level_val <= 0: continue
        if abs(ltp - level_val) <= tolerance and ltp >= level_val * 0.997:
            # Bullish trigger: lower wick bounce or bullish candle
            is_bull_trigger = (lower_wick / total > 0.45) or (last_c["close"] > last_c["open"] and last_c["close"] > last_p["close"])
            if is_bull_trigger:
                return {
                    "signal":      "BULL",
                    "name":        f"HTF Level Bounce ({level_name})",
                    "score_bonus": 1.2,
                    "meta": {
                        "level":      level_val,
                        "level_name": level_name,
                        "ltp":        ltp,
                        "proximity":  round((ltp - level_val) / level_val * 100, 2),
                    }
                }

    return empty


# ═══════════════════════════════════════════════════════════════════════════════
# 3. PCR EXTREME REVERSAL (CONTRARIAN)
# ═══════════════════════════════════════════════════════════════════════════════

def det_pcr_extreme(chain: dict, direction: str) -> dict:
    """
    PCR Extreme Reversal — contrarian signal.

    Logic:
    - PCR < 0.5: everyone is bearish → contrarian BUY CE (market likely to bounce)
    - PCR > 1.8: everyone is bullish → contrarian BUY PE (market likely to fall)
    - At extremes, the crowd is already positioned → move is exhausted

    Important: This OVERRIDES the trend direction when extremes hit.
    It generates a CONTRARIAN signal regardless of price action direction.

    Score bonus: +1.0 when confirming, -0.5 when contradicting (penalty)
    """
    empty = {"signal": None, "name": "", "score_bonus": 0, "meta": {}}
    if not chain:
        return empty

    pcr = float(chain.get("pcr", 0))
    if pcr <= 0:
        return empty

    if pcr < 0.50:
        # Extreme bearishness — contrarian buy CE
        severity = "EXTREME" if pcr < 0.35 else "HIGH"
        bonus = 1.2 if pcr < 0.35 else 0.8
        if direction == "BULL":
            # Signal agrees with contrarian view
            return {
                "signal":      "BULL",
                "name":        f"PCR Extreme ({severity}) Contrarian CE",
                "score_bonus": bonus,
                "meta": {
                    "pcr":      pcr,
                    "extreme":  "BEARISH EXTREME",
                    "insight":  f"PCR {pcr:.2f} = everyone bearish. Contrarian: buy CE.",
                }
            }
        else:
            # Direction contradicts PCR extreme — penalty
            return {
                "signal":      None,
                "name":        "PCR Extreme contradicts direction",
                "score_bonus": -0.5,
                "meta":        {"pcr": pcr, "extreme": "BEARISH EXTREME"}
            }

    if pcr > 1.80:
        # Extreme bullishness — contrarian buy PE
        severity = "EXTREME" if pcr > 2.0 else "HIGH"
        bonus = 1.2 if pcr > 2.0 else 0.8
        if direction == "BEAR":
            return {
                "signal":      "BEAR",
                "name":        f"PCR Extreme ({severity}) Contrarian PE",
                "score_bonus": bonus,
                "meta": {
                    "pcr":      pcr,
                    "extreme":  "BULLISH EXTREME",
                    "insight":  f"PCR {pcr:.2f} = everyone bullish. Contrarian: buy PE.",
                }
            }
        else:
            return {
                "signal":      None,
                "name":        "PCR Extreme contradicts direction",
                "score_bonus": -0.5,
                "meta":        {"pcr": pcr, "extreme": "BULLISH EXTREME"}
            }

    return empty


# ═══════════════════════════════════════════════════════════════════════════════
# 4. VIX DIVERGENCE
# ═══════════════════════════════════════════════════════════════════════════════

def det_vix_divergence(price_chg: float, vix_chg: float, direction: str) -> dict:
    """
    VIX Divergence — price vs VIX mismatch detector.

    Two key divergences:
    A. Nifty DROPS but VIX DOESN'T SPIKE (fear absent = fake selloff)
       → Contrarian BUY CE. Real fear would push VIX > 1.5% up.

    B. Nifty RISES but VIX DOESN'T FALL (fear persists = weak rally)
       → Contrarian BUY PE. Real strength would push VIX down.

    vix_chg: intraday VIX % change (positive = VIX rising = more fear)
    price_chg: Nifty/stock % change today

    Score bonus: +0.8
    """
    empty = {"signal": None, "name": "", "score_bonus": 0, "meta": {}}

    if vix_chg == 0:
        return empty

    # A: Price falls but VIX doesn't spike much → fake selloff → BUY CE
    if price_chg < -0.5 and vix_chg < 1.0:
        if direction == "BULL":
            return {
                "signal":      "BULL",
                "name":        "VIX Divergence — Fake Selloff (CE Buy)",
                "score_bonus": 0.8,
                "meta": {
                    "price_chg": price_chg,
                    "vix_chg":   vix_chg,
                    "insight":   "Price fell but fear (VIX) didn't spike. Likely a fake move.",
                }
            }

    # B: Price rises but VIX stays elevated → weak rally → BUY PE
    if price_chg > 0.5 and vix_chg > 0.5:
        if direction == "BEAR":
            return {
                "signal":      "BEAR",
                "name":        "VIX Divergence — Weak Rally (PE Buy)",
                "score_bonus": 0.8,
                "meta": {
                    "price_chg": price_chg,
                    "vix_chg":   vix_chg,
                    "insight":   "Price rose but VIX stayed high. Fear persists. Weak rally.",
                }
            }

    return empty


# ═══════════════════════════════════════════════════════════════════════════════
# 5. FII OPTIONS POSITIONING
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_fii_data() -> dict:
    """
    Fetch FII options positioning from NSE India.
    NSE publishes daily FII derivative stats (F&O participant data).
    Returns net CE/PE position for FIIs.
    Cached to file, refreshed once daily after 6 PM.
    """
    # Check cache
    cache = _load_fii_cache()
    cache_date = cache.get("date", "")
    today = date.today().isoformat()

    # If cached today and market closed, return cache
    now = datetime.now()
    if cache_date == today and now.hour >= 15:
        return cache

    try:
        session = requests.Session()
        session.get("https://www.nseindia.com", headers=NSE_HEADERS, timeout=8)

        # NSE participant-wise OI data
        resp = session.get(
            "https://www.nseindia.com/api/FO-participant-wise-trading-data",
            headers=NSE_HEADERS, timeout=12
        )
        if resp.status_code != 200:
            return cache or _fii_fallback()

        data = resp.json()
        if not data:
            return cache or _fii_fallback()

        # Parse FII row (typically category "FII/FPI")
        fii_row = None
        for row in data.get("data", []):
            cat = row.get("client_type", "") or row.get("category", "")
            if "FII" in cat.upper() or "FPI" in cat.upper():
                fii_row = row
                break

        if not fii_row:
            return cache or _fii_fallback()

        # Extract CE and PE net positions
        # Fields: index_call_long, index_call_short, index_put_long, index_put_short
        ce_long  = float(fii_row.get("index_call_long", 0) or 0)
        ce_short = float(fii_row.get("index_call_short", 0) or 0)
        pe_long  = float(fii_row.get("index_put_long", 0) or 0)
        pe_short = float(fii_row.get("index_put_short", 0) or 0)

        net_ce = ce_long - ce_short  # positive = FII net CE buyers (bullish)
        net_pe = pe_long - pe_short  # positive = FII net PE buyers (bearish)

        # Determine FII positioning signal
        if net_ce > 0 and net_ce > abs(net_pe) * 1.5:
            signal = "BULL"
            insight = f"FIIs net buying CE ({net_ce:+.0f} contracts). Bullish positioning."
        elif net_pe > 0 and net_pe > abs(net_ce) * 1.5:
            signal = "BEAR"
            insight = f"FIIs net buying PE ({net_pe:+.0f} contracts). Bearish positioning."
        else:
            signal = "NEUTRAL"
            insight = f"FII positioning mixed. CE: {net_ce:+.0f}, PE: {net_pe:+.0f}."

        result = {
            "date":     today,
            "net_ce":   round(net_ce, 0),
            "net_pe":   round(net_pe, 0),
            "signal":   signal,
            "insight":  insight,
            "ce_long":  round(ce_long, 0),
            "ce_short": round(ce_short, 0),
            "pe_long":  round(pe_long, 0),
            "pe_short": round(pe_short, 0),
            "updated":  now.strftime("%H:%M"),
        }
        _save_fii_cache(result)
        return result

    except Exception as e:
        log.warning(f"FII data fetch error: {e}")
        return cache or _fii_fallback()


def det_fii_positioning(direction: str) -> dict:
    """
    FII Positioning confirmation/contradiction detector.

    If FII is net CE buyer → bullish institutional positioning → confirms CE buy.
    If FII is net PE buyer → bearish institutional positioning → confirms PE buy.
    Contradicting FII = penalty.

    Score bonus: +0.7 (confirm), -0.3 (contradict)
    """
    empty = {"signal": None, "name": "", "score_bonus": 0, "meta": {}}
    try:
        fii = fetch_fii_data()
        sig = fii.get("signal", "NEUTRAL")

        if sig == "NEUTRAL":
            return empty

        if sig == "BULL" and direction == "BULL":
            return {
                "signal":      "BULL",
                "name":        "FII CE Net Buy Confirmation",
                "score_bonus": 0.7,
                "meta":        {"fii_net_ce": fii.get("net_ce"), "insight": fii.get("insight")},
            }
        if sig == "BEAR" and direction == "BEAR":
            return {
                "signal":      "BEAR",
                "name":        "FII PE Net Buy Confirmation",
                "score_bonus": 0.7,
                "meta":        {"fii_net_pe": fii.get("net_pe"), "insight": fii.get("insight")},
            }
        # Contradiction penalty
        return {
            "signal":      None,
            "name":        "FII positioning contradicts trade direction",
            "score_bonus": -0.3,
            "meta":        {"fii_signal": sig, "trade_dir": direction},
        }
    except Exception as e:
        log.debug(f"FII positioning error: {e}")
        return empty


def _load_fii_cache() -> dict:
    if not os.path.exists(FII_CACHE): return {}
    try:
        with open(FII_CACHE) as f: return json.load(f)
    except: return {}


def _save_fii_cache(data: dict):
    try:
        with open(FII_CACHE, "w") as f: json.dump(data, f, indent=2)
    except: pass


def _fii_fallback() -> dict:
    return {
        "date": date.today().isoformat(), "net_ce": 0, "net_pe": 0,
        "signal": "NEUTRAL", "insight": "FII data unavailable.",
        "updated": datetime.now().strftime("%H:%M"),
    }


def run_fii_fetch_loop():
    """Background thread: refresh FII data after market close each day."""
    while True:
        try:
            now = datetime.now()
            # Fetch once after market close (6 PM)
            if now.hour >= 18 and now.weekday() < 5:
                log.info("Fetching FII positioning data...")
                fetch_fii_data()
                log.info("FII data updated.")
                time.sleep(3600 * 12)  # Wait 12h before next fetch
                continue
        except Exception as e:
            log.error(f"FII fetch loop error: {e}")
        time.sleep(3600)  # Check hourly


# ═══════════════════════════════════════════════════════════════════════════════
# 6. 0DTE GAMMA SCALP (EXPIRY DAY RULES)
# ═══════════════════════════════════════════════════════════════════════════════

def det_0dte_gamma(df_5min: pd.DataFrame, chain: dict, ltp: float, is_expiry_day: bool) -> dict:
    """
    0DTE Gamma Scalp — expiry-day specific rules.

    On expiry day, ATM options have extreme gamma:
    - Small spot moves = huge % option moves
    - ATM options are cheapest → best RR for day trades

    Rules:
    1. Only fires ON EXPIRY DAY
    2. Entry window: 9:30 AM – 11:30 AM or 2:00 PM – 3:00 PM
    3. Price must break out of first 15-min range (mini ORB)
    4. PCR must not be neutral (must have directional lean)
    5. Max Pain deviation: price > 100pts from max pain (favors reversion)

    Score bonus: +1.0 (expiry gamma = high conviction intraday)
    """
    empty = {"signal": None, "name": "", "score_bonus": 0, "meta": {}}

    if not is_expiry_day:
        return empty

    now = datetime.now()
    t   = now.hour * 60 + now.minute

    # Only in valid gamma windows
    morning_window   = (9 * 60 + 30) <= t <= (11 * 60 + 30)
    afternoon_window = (14 * 60 + 0) <= t <= (15 * 60 + 0)
    if not morning_window and not afternoon_window:
        return empty

    if len(df_5min) < 3:
        return empty

    # Mini ORB on expiry (first 15 min = first 3 candles)
    orb_hi = df_5min.iloc[:3]["high"].max() if len(df_5min) >= 3 else 0
    orb_lo = df_5min.iloc[:3]["low"].min()  if len(df_5min) >= 3 else 0
    curr   = df_5min.iloc[-1]["close"]

    if orb_hi == 0 or orb_lo == 0:
        return empty

    # PCR direction
    pcr = chain.get("pcr", 1.0) if chain else 1.0
    max_pain = chain.get("max_pain", ltp) if chain else ltp

    # Max Pain deviation (price pulled toward max pain by expiry)
    pain_deviation = ltp - max_pain
    max_pain_bias  = "BULL" if pain_deviation < -80 else ("BEAR" if pain_deviation > 80 else None)

    # Gamma scalp: bullish breakout above ORB
    if curr > orb_hi and pcr >= 1.0:
        return {
            "signal":      "BULL",
            "name":        "0DTE Gamma Scalp (Expiry Bull)",
            "score_bonus": 1.0,
            "meta": {
                "orb_hi":        round(orb_hi, 2),
                "orb_lo":        round(orb_lo, 2),
                "max_pain":      round(max_pain, 2),
                "pain_dev":      round(pain_deviation, 0),
                "pcr":           round(pcr, 2),
                "window":        "Morning" if morning_window else "Afternoon",
            }
        }

    # Gamma scalp: bearish breakdown below ORB
    if curr < orb_lo and pcr <= 1.0:
        return {
            "signal":      "BEAR",
            "name":        "0DTE Gamma Scalp (Expiry Bear)",
            "score_bonus": 1.0,
            "meta": {
                "orb_hi":    round(orb_hi, 2),
                "orb_lo":    round(orb_lo, 2),
                "max_pain":  round(max_pain, 2),
                "pain_dev":  round(pain_deviation, 0),
                "pcr":       round(pcr, 2),
                "window":    "Morning" if morning_window else "Afternoon",
            }
        }

    # Max Pain reversion (even without ORB break)
    if max_pain_bias and afternoon_window:
        return {
            "signal":      max_pain_bias,
            "name":        f"0DTE Max Pain Reversion ({max_pain_bias})",
            "score_bonus": 0.8,
            "meta": {
                "max_pain":  round(max_pain, 2),
                "pain_dev":  round(pain_deviation, 0),
                "ltp":       ltp,
            }
        }

    return empty


# ═══════════════════════════════════════════════════════════════════════════════
# MASTER INTEGRATION FUNCTION
# ═══════════════════════════════════════════════════════════════════════════════

def run_advanced_setups(
    df:             pd.DataFrame,        # 5-min candle data
    df_daily:       pd.DataFrame,        # daily candle data (10+ days)
    chain:          dict,                # option chain from build_option_chain
    quote:          dict,                # current quote
    ltp:            float,
    vix:            float,
    vix_prev:       float,               # VIX at start of day
    direction:      str,                 # BULL or BEAR (from primary setup)
    is_expiry_day:  bool = False,
) -> dict:
    """
    Runs ALL advanced setups and returns aggregated result:
    {
        setups_fired: [list of fired setup names],
        score_bonus:  total bonus to add to base score,
        best_setup:   name of highest-bonus setup (overrides base setup name if better),
        meta:         combined metadata from all setups,
        penalties:    total score penalty,
    }
    """
    results = {
        "setups_fired": [],
        "score_bonus":  0.0,
        "best_setup":   None,
        "best_bonus":   0.0,
        "meta":         {},
        "penalties":    0.0,
    }

    price_chg = ((ltp - quote.get("prev_close", ltp)) / quote.get("prev_close", ltp) * 100) \
                if quote.get("prev_close", ltp) > 0 else 0

    vix_chg   = ((vix - vix_prev) / vix_prev * 100) if vix_prev > 0 else 0

    # Daily levels for HTF entry
    daily_levels = calc_daily_levels(df_daily, ltp) if not df_daily.empty else {}

    # Run all detectors
    detectors = [
        ("liquidity_sweep",   det_liq_sweep_v2(df)),
        ("htf_level",         det_htf_level_entry(df, daily_levels, ltp)),
        ("pcr_extreme",       det_pcr_extreme(chain, direction)),
        ("vix_divergence",    det_vix_divergence(price_chg, vix_chg, direction)),
        ("fii_positioning",   det_fii_positioning(direction)),
        ("gamma_0dte",        det_0dte_gamma(df, chain, ltp, is_expiry_day)),
    ]

    for name, result in detectors:
        if not result:
            continue

        bonus    = result.get("score_bonus", 0)
        signal   = result.get("signal")
        setup_nm = result.get("name", "")
        meta     = result.get("meta", {})

        if bonus < 0:
            # Penalty
            results["penalties"] += abs(bonus)
            results["meta"][name] = {"penalty": bonus, "reason": setup_nm}
            continue

        if bonus > 0 and signal in (direction, None) or (signal and signal == direction):
            results["setups_fired"].append(setup_nm)
            results["score_bonus"] += bonus
            results["meta"][name]   = meta

            if bonus > results["best_bonus"]:
                results["best_bonus"] = bonus
                results["best_setup"] = setup_nm

    results["score_bonus"] = round(results["score_bonus"], 2)
    results["penalties"]   = round(results["penalties"], 2)

    return results


def get_fii_current() -> dict:
    """Public accessor for FII data (used by server endpoint)."""
    return _load_fii_cache()
