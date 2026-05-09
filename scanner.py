# =============================================================================
# scanner.py v9
# =============================================================================
# KEY FIXES:
# 1. NIFTY option chain: correct base = "NIFTY" (not "NIFTY50")
#    Fyers format: NSE:NIFTY25APR24300CE  (YYMONTHABV for current month weekly)
#                  NSE:NIFTY2504224300CE  (YYMMDD for next weeks sometimes)
#    Try BOTH formats; use whichever returns data
#
# 2. SENSEX per-expiry chain: fetch_chain_for_expiry() accepts expiry param
#    so the /api/chain endpoint can return fresh data per expiry click
#
# 3. Stock option symbols: NSE stocks use DIFFERENT format from index
#    Stock: NSE:JSWSTEEL2504221280CE  (YYMMDD + strike + CE/PE)
#    This is the same as index — but we must try the right base name
#
# 4. Bias runs immediately at startup (not after 15min wait)
#
# 5. Option prices are fetched live at trade time — entry = ask price NOW
#
# 6. Index trades included in overview stocks array
#
# 7. Realistic SL/Target based on actual option chain levels, not fixed 1:2
# =============================================================================

import os, json, time, logging, feedparser
import pandas as pd
import numpy as np
from datetime import datetime, date, timedelta
from fyers_apiv3 import fyersModel
import config
import trade_tracker, learner

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("scanner")

MARKET_HOLIDAYS = {
    date(2026,1,26), date(2026,3,25), date(2026,4,3), date(2026,4,14),
    date(2026,5,1),  date(2026,8,15), date(2026,10,2), date(2026,10,24),
    date(2026,11,14),date(2026,11,15),date(2026,12,25),
}

BULLISH_KW = ["rate cut","repo cut","stimulus","buyback","beats estimate","profit rises","revenue grows",
    "upgrade","strong results","fii buying","fii inflow","crude falls","oil drops","rupee strengthens",
    "gdp growth","rally","breakout","dividend","record high","acquisition","earnings up","recovery","surplus",
    "fed pivot","rate pause","inflation cools","us market up","dow up","nasdaq up","global rally","risk on"]
BEARISH_KW = ["rate hike","hawkish","inflation rises","miss estimate","profit falls","revenue drops",
    "downgrade","weak results","fii selling","fii outflow","crude rises","oil jumps","rupee weakens",
    "recession","gdp falls","margin pressure","debt","selloff","crash","correction","us fed hawkish",
    "dollar strengthens","china slowdown","war","geopolitical","sanctions","tariff","banking crisis",
    "default","earnings miss","profit warning","guidance cut"]
INDIA_KW = ["india","rbi","sebi","nse","bse","nifty","sensex","rupee","fii","dii","repo","inflation","gdp",
    "crude","oil","dollar","gold","us fed","federal reserve","interest rate","reliance","hdfc","tcs","infosys",
    "wipro","sbi","icici","kotak","axis","bajaj","maruti","tatamotors","itc","airtel","adani","ntpc","ongc",
    "bpcl","sunpharma","tatasteel","jsw","bank","banking","it sector","pharma","auto","energy","fmcg","metal",
    "telecom","budget","quarterly results","earnings","dividend","ipo","merger","acquisition"]
STOCK_KW = {
    "RELIANCE":["reliance","jio"],"HDFCBANK":["hdfc bank"],"INFY":["infosys"],"TCS":["tcs"],
    "TATAMOTORS":["tata motors"],"BAJFINANCE":["bajaj finance"],"SBIN":["sbi","state bank"],
    "AXISBANK":["axis bank"],"ICICIBANK":["icici bank"],"BHARTIARTL":["airtel","bharti"],
    "SUNPHARMA":["sun pharma"],"TATASTEEL":["tata steel"],"ITC":["itc"],"JSWSTEEL":["jsw steel"],
    "NTPC":["ntpc"],"ONGC":["ongc"],"BANKING":["bank","banking","rbi","repo","rate","npa"],
    "IT":["it sector","tech","dollar","us dollar","fed","nasdaq"],"PHARMA":["pharma","drug","fda"],
    "AUTO":["auto","ev","automobile"],"NIFTY":["nifty","sensex","index","market","fii","dii","vix","sebi"],
}

# ==============================================================================
# EXPIRY HELPERS
# ==============================================================================

def _is_td(d): return d.weekday() < 5 and d not in MARKET_HOLIDAYS

def _prev_td(d):
    d -= timedelta(days=1)
    while not _is_td(d): d -= timedelta(days=1)
    return d

def _next_or_same_td(d):
    while not _is_td(d): d += timedelta(days=1)
    return d

def get_nifty_expiries(n=8):
    """Nifty weekly expiry = every Tuesday"""
    today = date.today()
    out = []
    # Find next Tuesday
    days_ahead = (1 - today.weekday()) % 7  # 1 = Tuesday
    cur = today + timedelta(days=days_ahead)
    while len(out) < n:
        e = cur if _is_td(cur) else _prev_td(cur)
        out.append(e)
        cur += timedelta(weeks=1)
    return out

def get_sensex_expiries(n=8):
    """Sensex weekly expiry = every Thursday"""
    today = date.today()
    out = []
    days_ahead = (3 - today.weekday()) % 7  # 3 = Thursday
    cur = today + timedelta(days=days_ahead)
    while len(out) < n:
        e = cur if _is_td(cur) else _prev_td(cur)
        out.append(e)
        cur += timedelta(weeks=1)
    return out

def get_monthly_expiry(weekday=1, months=3):
    today = date.today()
    out = []
    for m in range(months):
        mo = (today.month + m - 1) % 12 + 1
        yr = today.year + (today.month + m - 1) // 12
        last = date(yr+1,1,1) - timedelta(days=1) if mo == 12 else date(yr,mo+1,1) - timedelta(days=1)
        d = last
        while d.weekday() != weekday: d -= timedelta(days=1)
        out.append(d if _is_td(d) else _prev_td(d))
    return out

def fmt_exp(d): return d.strftime("%d-%b-%y").upper()

def get_stock_weekly_expiry():
    """
    NSE F&O stock weekly expiry = every Thursday.
    Returns nearest future Thursday. If today is Thursday and before 15:30, return today.
    """
    today = date.today()
    days_to_thu = (3 - today.weekday()) % 7  # 3 = Thursday
    if days_to_thu == 0:
        # Today is Thursday — if market still open use today, else next Thursday
        now = datetime.now()
        if now.hour < 15 or (now.hour == 15 and now.minute <= 30):
            return today
        days_to_thu = 7
    expiry = today + timedelta(days=days_to_thu)
    return expiry


def build_expiry_calendar():
    nw = get_nifty_expiries()
    nm = get_monthly_expiry(1)
    sw = get_sensex_expiries()
    sm = get_monthly_expiry(3)
    tl = lambda ds: [{"date": d.isoformat(), "label": fmt_exp(d)} for d in ds]
    return {
        "nifty":  {"weekly": tl(nw), "monthly": tl(nm), "current": fmt_exp(nw[0]) if nw else "—", "expiry_day": "Tuesday"},
        "sensex": {"weekly": tl(sw), "monthly": tl(sm), "current": fmt_exp(sw[0]) if sw else "—", "expiry_day": "Thursday"},
    }


# ==============================================================================
# OPTION SYMBOL FORMATS — CRITICAL FIX
# ==============================================================================
#
# Fyers NSE Index option format (NIFTY):
#   CURRENT WEEK (monthly expiry months) : NSE:NIFTY25APR24300CE  (YYMONTHABV + strike + opt)
#   WEEKLY (non-expiry months)           : NSE:NIFTY2504224300CE  (YYMMDD + strike + opt)
#
# Fyers BSE Index option format (SENSEX):
#   Same pattern: BSE:SENSEX25APR78600PE or BSE:SENSEX2504278600PE
#
# Fyers NSE Stock option format:
#   NSE:JSWSTEEL2504221280CE  (YYMMDD + strike + opt)
#   NSE:JSWSTEEL25APR1280CE   (YYMONTHABV + strike + opt)
#
# The key insight: try BOTH formats, use whichever returns ltp > 0
# ==============================================================================

def _is_monthly_expiry(expiry, is_nse_index=True):
    """
    Check if an expiry date is the monthly expiry (last Thu for NSE stocks/Nifty, last Thu for Sensex).
    Monthly expiry = last Thursday of the month for NSE, last Thursday for BSE Sensex.
    Nifty monthly = last Tuesday of month (from Sep 2025 SEBI change).
    """
    # Find the last occurrence of the weekday in the month
    import calendar
    year, month = expiry.year, expiry.month
    # Get last day of month
    last_day = calendar.monthrange(year, month)[1]
    last_date = expiry.replace(day=last_day)
    # Find last Tuesday (weekday=1) for Nifty, last Thursday (weekday=3) for others
    target_wd = 1 if is_nse_index else 3
    while last_date.weekday() != target_wd:
        last_date -= timedelta(days=1)
    return expiry == last_date


def option_sym_candidates(base, exch, expiry, strike, opt_type):
    """
    Returns list of candidate symbols to try in correct priority order.

    Fyers weekly format: YY + M_code + DD
      where M_code is single digit 1-9 for Jan-Sep, and letters O/N/D for Oct/Nov/Dec
      Example: May 5 2026 → "26505"  (NOT "260505")
    Fyers monthly format: YY + MonthAbv (e.g. "26MAY")

    For weekly expiries we MUST use the weekly format — otherwise Fyers
    returns prices for the monthly contract.
    """
    si = int(strike)
    yy = expiry.strftime("%y")
    # Month code: 1-9 as digit, Oct=O, Nov=N, Dec=D
    month_codes = {1:"1",2:"2",3:"3",4:"4",5:"5",6:"6",7:"7",8:"8",9:"9",10:"O",11:"N",12:"D"}
    mc = month_codes[expiry.month]
    dd = f"{expiry.day:02d}"
    weekly_code  = f"{yy}{mc}{dd}"                # 26505  (correct Fyers weekly format)
    yymmdd       = expiry.strftime("%y%m%d")      # 260505 (some symbols use this)
    yymonthabv   = expiry.strftime("%y%b").upper() # 26MAY  (monthly format)

    is_nse_idx = exch == "NSE" and base in ("NIFTY", "SENSEX")
    is_monthly  = _is_monthly_expiry(expiry, is_nse_index=(exch == "NSE"))

    if exch == "BSE":
        if is_monthly:
            return [
                f"BSE:{base}{yymonthabv}{si}{opt_type}",
                f"BSE:{base}{weekly_code}{si}{opt_type}",
                f"BSE:{base}{yymmdd}{si}{opt_type}",
            ]
        else:
            # Weekly: weekly_code (YY+M+DD) is the actual Fyers weekly format
            return [
                f"BSE:{base}{weekly_code}{si}{opt_type}",
                f"BSE:{base}{yymmdd}{si}{opt_type}",
                f"BSE:{base}{yymonthabv}{si}{opt_type}",
            ]
    else:  # NSE
        if is_monthly:
            return [
                f"NSE:{base}{yymonthabv}{si}{opt_type}",
                f"NSE:{base}{weekly_code}{si}{opt_type}",
                f"NSE:{base}{yymmdd}{si}{opt_type}",
            ]
        else:
            # Weekly: weekly_code MUST come first — yymonthabv would give monthly prices
            return [
                f"NSE:{base}{weekly_code}{si}{opt_type}",
                f"NSE:{base}{yymmdd}{si}{opt_type}",
                f"NSE:{base}{yymonthabv}{si}{opt_type}",
            ]


# ==============================================================================
# FYERS CONNECTION
# ==============================================================================

def get_fyers_client():
    if not os.path.exists(config.ACCESS_TOKEN_FILE):
        raise FileNotFoundError("\n[ERROR] No token. Run: python3 login.py\n")
    with open(config.ACCESS_TOKEN_FILE) as f:
        token = f.read().strip()
    if not token:
        raise ValueError("Token empty.")
    fyers = fyersModel.FyersModel(client_id=config.CLIENT_ID, token=token, log_path=os.path.dirname(__file__))
    log.info("Fyers connected.")
    return fyers


# ==============================================================================
# DATA FETCHING
# ==============================================================================

def fetch_quotes(fyers, symbols, batch_size=25, delay=0.5, retries=2):
    """
    Batch quote fetch with rate-limit handling.
    batch_size=25: Fyers allows ~10 req/sec; smaller batches = less likely to hit limit
    delay=0.5s between batches
    retries=2: retry once on rate-limit errors
    """
    if not symbols: return {}
    result = {}
    symbols = list(dict.fromkeys(symbols))  # deduplicate preserving order
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i+batch_size]
        for attempt in range(retries + 1):
            try:
                data = fyers.quotes({"symbols": ",".join(batch)})
                code = data.get("code", -1)
                if code == 200 and "d" in data:
                    for item in data["d"]:
                        v = item["v"]
                        result[item["n"]] = {
                            "ltp":        v.get("lp", 0),
                            "open":       v.get("open_price", 0),
                            "high":       v.get("high_price", 0),
                            "low":        v.get("low_price", 0),
                            "prev_close": v.get("prev_close_price", 0),
                            "volume":     v.get("volume", 0),
                            "oi":         v.get("oi", 0),
                            "bid":        v.get("bid", 0),
                            "ask":        v.get("ask", 0),
                        }
                    break  # success
                elif "request limit" in str(data.get("message","")).lower() or "rate" in str(data.get("message","")).lower():
                    wait = (attempt + 1) * 2.0
                    log.warning(f"Rate limit hit batch {i//batch_size}, waiting {wait}s (attempt {attempt+1})")
                    time.sleep(wait)
                else:
                    log.warning(f"Quote failed batch {i//batch_size}: {data.get('message','')}")
                    break
            except Exception as e:
                log.warning(f"Quote fetch error batch {i//batch_size}: {e}")
                break
        # Polite delay between batches to avoid rate limiting
        if i + batch_size < len(symbols):
            time.sleep(delay)
    return result


def fetch_candles(fyers, symbol, tf=5, days=10):
    dfr = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    dto = date.today().strftime("%Y-%m-%d")
    try:
        data = fyers.history({"symbol": symbol, "resolution": str(tf), "date_format": "1",
                              "range_from": dfr, "range_to": dto, "cont_flag": "1"})
        if data.get("code") != 200 or "candles" not in data:
            return pd.DataFrame()
        df = pd.DataFrame(data["candles"], columns=["timestamp","open","high","low","close","volume"])
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="s")
        return df.sort_values("datetime").reset_index(drop=True)
    except Exception as e:
        log.warning(f"Candle fetch error {symbol}: {e}")
        return pd.DataFrame()


def get_live_option_price(fyers, base, exch, expiry, strike, opt_type):
    """
    Fetches the ACTUAL live Ask/Bid/LTP for a specific option right now.
    Returns (ask, bid, ltp, symbol_used) or (0, 0, 0, None) if not available.
    """
    candidates = option_sym_candidates(base, exch, expiry, strike, opt_type)
    q = fetch_quotes(fyers, candidates)
    for s in candidates:
        d = q.get(s, {})
        ltp = d.get("ltp", 0)
        ask = d.get("ask", 0)
        bid = d.get("bid", 0)
        if ltp > 0:
            if ask == 0: ask = round(ltp * 1.02, 1)
            if bid == 0: bid = round(ltp * 0.98, 1)
            return round(ask, 1), round(bid, 1), round(ltp, 1), s
    log.debug(f"  No live price for {base} {strike} {opt_type} exp:{expiry} tried: {candidates}")
    return 0, 0, 0, None


# ==============================================================================
# OPTION CHAIN — fresh fetch per expiry
# ==============================================================================

def build_option_chain(fyers, sym, ltp, step, is_index=False, expiry=None):
    """
    Build option chain for a given symbol and expiry.
    If expiry is None, uses the nearest expiry.
    FIX #1: Correct base name — "NIFTY" not "NIFTY50"
    FIX #2: Per-expiry support for fresh chain on tab click
    """
    is_sensex = "SENSEX" in sym.upper()
    is_nifty  = ("NIFTY" in sym.upper()) and not is_sensex
    exch      = "BSE" if is_sensex else "NSE"

    # FIX #1: Correct base names for Fyers option symbols
    if is_nifty:
        base = "NIFTY"      # NOT "NIFTY50" — that's the quote symbol, not the option base
    elif is_sensex:
        base = "SENSEX"
    else:
        base = sym.replace("NSE:","").replace("BSE:","").replace("-EQ","").replace("-INDEX","")

    if expiry is None:
        if is_nifty:    expiry = get_nifty_expiries(1)[0]
        elif is_sensex: expiry = get_sensex_expiries(1)[0]
        else:           expiry = get_nifty_expiries(1)[0]

    # Reduced strikes to keep batches small and avoid Fyers rate limits
    n_strikes = 6 if is_index else 4
    atm       = round(ltp / step) * step
    strikes   = [atm + (i * step) for i in range(-n_strikes, n_strikes + 1)]

    # Build all candidate symbols — try both formats for each strike
    all_syms  = []
    sym_meta  = {}  # sym_str -> (strike, opt_type, format_idx)
    for s in strikes:
        for opt in ["CE", "PE"]:
            for fi, cand in enumerate(option_sym_candidates(base, exch, expiry, s, opt)):
                if cand not in sym_meta:
                    all_syms.append(cand)
                    sym_meta[cand] = (s, opt, fi)

    log.info(f"  Chain fetch: {base} exp:{fmt_exp(expiry)} {len(all_syms)} symbols")
    # Use smaller batch+delay to stay within Fyers rate limits
    raw = fetch_quotes(fyers, all_syms, batch_size=20, delay=0.6)

    # Find which format works (has live ltp > 0)
    working_fmt = None
    for sym_str, (s, opt, fi) in sym_meta.items():
        if raw.get(sym_str, {}).get("ltp", 0) > 0:
            working_fmt = fi
            log.debug(f"  Chain format {fi} works for {base}: {sym_str}")
            break

    if working_fmt is None:
        log.warning(f"  No option data for {base} exp:{fmt_exp(expiry)} — all formats returned 0. Market closed?")
        return {}

    # Assemble strike map
    strike_map = {}
    for sym_str, (s, opt, fi) in sym_meta.items():
        if fi != working_fmt: continue
        d   = raw.get(sym_str, {})
        ltp_opt = d.get("ltp", 0)
        ask     = d.get("ask", 0)
        bid     = d.get("bid", 0)
        oi      = d.get("oi", 0)
        vol     = d.get("volume", 0)

        if s not in strike_map:
            strike_map[s] = {"strike": s, "is_atm": s == atm}

        if ltp_opt > 0 or ask > 0 or oi > 0:
            if ask == 0: ask = round(ltp_opt * 1.02, 1)
            if bid == 0: bid = round(ltp_opt * 0.98, 1)
            strike_map[s][opt] = {
                "oi": oi, "oi_chg": 0, "volume": vol,
                "ltp": ltp_opt, "bid": bid, "ask": ask,
                "iv": 0, "delta": 0.5 if opt=="CE" else -0.5,
                "sym": sym_str,
            }

    strikes_list = sorted(strike_map.values(), key=lambda x: x["strike"])
    if not strikes_list: return {}

    # Build preliminary chain dict for analytics enrichment
    partial_chain = {"strikes": strikes_list, "atm_strike": atm, "spot": ltp}

    # Enrich chain with Black-Scholes Greeks, IV per strike, proper Max Pain
    try:
        import analytics as _an
        _enriched = _an.run_greeks_enrichment(partial_chain, ltp, expiry)
        if _enriched and _enriched.get("strikes"):
            strikes_list = _enriched["strikes"]
    except Exception as _ae:
        log.debug(f"Greeks enrichment skipped: {_ae}")

    total_ce = sum(s.get("CE",{}).get("oi",0) for s in strikes_list)
    total_pe = sum(s.get("PE",{}).get("oi",0) for s in strikes_list)
    pcr      = round(total_pe / total_ce, 2) if total_ce > 0 else 0
    max_pain = _max_pain(strikes_list)
    ce_wall  = max((s for s in strikes_list if "CE" in s),
                   key=lambda x: x["CE"].get("oi",0), default={"strike": atm})["strike"]
    pe_wall  = max((s for s in strikes_list if "PE" in s),
                   key=lambda x: x["PE"].get("oi",0), default={"strike": atm})["strike"]
    atm_ce   = strike_map.get(atm, {}).get("CE", {})
    atm_pe   = strike_map.get(atm, {}).get("PE", {})
    bias     = compute_bias(pcr)
    rec      = _chain_rec(pcr, atm, ce_wall, pe_wall, atm_ce, atm_pe, max_pain, bias)

    return {
        "atm_strike": atm, "strikes": strikes_list, "pcr": pcr,
        "max_pain": max_pain, "ce_wall": ce_wall, "pe_wall": pe_wall,
        "atm_ce": atm_ce, "atm_pe": atm_pe,
        "total_ce_oi": total_ce, "total_pe_oi": total_pe,
        "recommendation": rec, "bias": bias,
        "expiry_label": fmt_exp(expiry),
        "expiry_date": expiry.isoformat(),
        "working_format": working_fmt,
        "fetched_at": datetime.now().strftime("%H:%M:%S"),
    }


def _max_pain(strikes):
    if not strikes: return 0
    best, res = float("inf"), 0
    for ep in [s["strike"] for s in strikes]:
        pain = sum(
            s.get("CE",{}).get("oi",0) * max(0, s["strike"] - ep) +
            s.get("PE",{}).get("oi",0) * max(0, ep - s["strike"])
            for s in strikes
        )
        if pain < best: best, res = pain, ep
    return res


def compute_bias(pcr):
    if pcr <= 0: return "NO DATA"
    if pcr > 1.5: return "EXTREME BULLISH"
    if pcr > 1.2: return "BULLISH"
    if pcr >= 0.8: return "SIDEWAYS"
    if pcr >= 0.5: return "BEARISH"
    return "EXTREME BEARISH"


def _chain_rec(pcr, atm, ce_wall, pe_wall, atm_ce, atm_pe, max_pain, bias):
    if "BULLISH" in bias:
        note    = f"PCR {pcr} ({bias}). Put writers at {pe_wall}. Bullish bias."
        primary = {"action": "Buy CE", "strike": atm, "ltp": atm_ce.get("ltp", 0)}
        hedge   = {"action": "Sell CE", "strike": ce_wall}
    elif "BEARISH" in bias:
        note    = f"PCR {pcr} ({bias}). Call writers at {ce_wall}. Bearish bias."
        primary = {"action": "Buy PE", "strike": atm, "ltp": atm_pe.get("ltp", 0)}
        hedge   = {"action": "Sell PE", "strike": pe_wall}
    else:
        note    = f"PCR {pcr} (SIDEWAYS). Range {pe_wall}–{ce_wall}. Wait for breakout."
        primary = {"action": "Wait", "strike": atm, "ltp": 0}
        hedge   = {}
    return {"bias": bias, "bias_note": note, "primary": primary, "hedge": hedge,
            "support": pe_wall, "resistance": ce_wall, "max_pain": max_pain}


# ==============================================================================
# SUPPORT & RESISTANCE
# ==============================================================================

def calculate_sr(df, ltp=0):
    if df.empty: return {}
    df = df.copy()
    df["date"] = df["datetime"].dt.date
    days = sorted(df["date"].unique())
    result = {}
    if len(days) >= 2:
        prev = df[df["date"] == days[-2]]
        if not prev.empty:
            h, l, c = prev["high"].max(), prev["low"].min(), prev["close"].iloc[-1]
            pivot = round((h+l+c)/3, 2)
            result.update({
                "pivot": pivot,
                "r1": round(2*pivot - l, 2), "r2": round(pivot + (h-l), 2),
                "s1": round(2*pivot - h, 2), "s2": round(pivot - (h-l), 2),
            })
    if days:
        td = df[df["date"] == days[-1]]
        if not td.empty:
            result.update({
                "today_high": round(td["high"].max(), 2),
                "today_low":  round(td["low"].min(), 2),
                "intraday_support":    round(td["low"].min(), 2),
                "intraday_resistance": round(td["high"].max(), 2),
            })
    s1 = result.get("s1", 0); tl = result.get("today_low", 0)
    result["support"]    = max(s1, tl) if s1 > 0 else tl
    result["resistance"] = min(result.get("r1", 9999), result.get("today_high", 9999)) \
                           if result.get("r1", 0) > 0 else result.get("today_high", ltp*1.01)
    return result


# ==============================================================================
# REALISTIC SL / TARGET — FIX #5
# ==============================================================================

def _get_vix(fyers):
    """Fetch live VIX — returns cached value if fetch fails."""
    try:
        qs = fetch_quotes(fyers, [config.VIX_SYMBOL], batch_size=2, delay=0.1)
        v = qs.get(config.VIX_SYMBOL, {}).get("ltp", 0)
        return float(v) if v else 18.0
    except:
        return 18.0


def dynamic_rr(entry, chain, opt_type, ltp_index, step, vix=18.0):
    """
    FIX 3: Dynamic Risk:Reward based on VIX, time of day, PCR, and option chain levels.

    SL logic:
      VIX < 13  → SL = 20% (low vol, tight stops)
      VIX 13-17 → SL = 25% (normal)
      VIX 17-20 → SL = 30% (moderate vol)
      VIX 20-25 → SL = 35% (elevated vol, wider stops needed)
      VIX > 25  → SL = 40% (high vol, very wide stops)

    Target multiplier:
      Early session (9:15-10:30): × 2.5 (trending time, larger moves)
      Mid session  (10:30-13:00): × 2.0 (normal)
      Power hour   (13:00-15:00): × 1.75 (momentum fades)
      Last 30 min  (15:00-15:30): × 1.5 (limit risk near close)

    PCR adjustment:
      Extreme PCR (>1.5 bull or <0.5 bear): tighten SL 10%, widen target 20%
      (strong conviction = let winners run more)
    """
    now_h = datetime.now().hour
    now_m = datetime.now().minute
    now_mins = now_h * 60 + now_m

    # ── SL % based on VIX ────────────────────────────────────────────────────
    if vix < 13:
        sl_pct = 0.20
    elif vix < 17:
        sl_pct = 0.25
    elif vix < 20:
        sl_pct = 0.30
    elif vix < 25:
        sl_pct = 0.35
    else:
        sl_pct = 0.40

    # ── Target multiplier based on time of day ────────────────────────────────
    if now_mins < 9*60+30:    # before 9:30
        tgt_mult = 2.5
    elif now_mins < 10*60+30: # 9:30-10:30 (opening momentum)
        tgt_mult = 2.5
    elif now_mins < 13*60:    # 10:30-13:00 (normal)
        tgt_mult = 2.0
    elif now_mins < 15*60:    # 13:00-15:00 (power hour)
        tgt_mult = 1.75
    else:                      # 15:00+ (last 30 min)
        tgt_mult = 1.5

    # ── PCR adjustment ────────────────────────────────────────────────────────
    pcr = chain.get("pcr", 1.0) if chain else 1.0
    is_extreme_bull = pcr > 1.5 and opt_type == "CE"
    is_extreme_bear = pcr < 0.5 and opt_type == "PE"
    if is_extreme_bull or is_extreme_bear:
        sl_pct  *= 0.90   # tighter SL (high conviction)
        tgt_mult *= 1.20  # wider target

    sl_amt   = round(entry * sl_pct, 1)
    sl_price = max(round(entry - sl_amt, 1), 0.5)
    tgt_amt  = round(sl_amt * tgt_mult, 1)

    # ── Try to use chain wall for more precise target ─────────────────────────
    if chain and "strikes" in chain:
        strikes = chain["strikes"]
        ce_wall = chain.get("ce_wall", 0)
        pe_wall = chain.get("pe_wall", 0)
        if opt_type == "CE" and ce_wall and ce_wall > ltp_index:
            wall_s = next((s for s in strikes if s["strike"] == ce_wall), None)
            if wall_s and "CE" in wall_s:
                w_ltp = wall_s["CE"].get("ltp", 0)
                if w_ltp > entry + sl_amt * 1.2:  # only if chain target beats minimum
                    tgt_amt = round(w_ltp - entry, 1)
        elif opt_type == "PE" and pe_wall and pe_wall < ltp_index:
            wall_s = next((s for s in strikes if s["strike"] == pe_wall), None)
            if wall_s and "PE" in wall_s:
                w_ltp = wall_s["PE"].get("ltp", 0)
                if w_ltp > entry + sl_amt * 1.2:
                    tgt_amt = round(w_ltp - entry, 1)

    # Enforce minimum 1:1.5
    if tgt_amt < sl_amt * 1.5:
        tgt_amt = round(sl_amt * 1.5, 1)

    tgt_price = round(entry + tgt_amt, 1)
    rr = round(tgt_amt / sl_amt, 1) if sl_amt > 0 else 1.5

    return sl_price, tgt_price, sl_amt, tgt_amt, f"1:{rr}"


# Keep old name as alias for backward compat
def realistic_sl_target(entry, chain, opt_type, ltp_index, step):
    return dynamic_rr(entry, chain, opt_type, ltp_index, step, vix=18.0)


# ==============================================================================
# INDEX SCANNER
# ==============================================================================

def scan_index(fyers, sym, name, expiry_fn):
    log.info(f"  Scanning {name}...")
    quotes = fetch_quotes(fyers, [sym])
    q      = quotes.get(sym, {})
    ltp    = q.get("ltp", 0)
    if ltp == 0: return {}

    df       = fetch_candles(fyers, sym, tf=5, days=10)
    df_today = df[df["datetime"].dt.date == date.today()].copy() if not df.empty else pd.DataFrame()
    vwap     = _calc_vwap(df_today)

    is_nifty  = "NIFTY" in name and "SENSEX" not in name
    step      = config.NIFTY_STRIKE_STEP if is_nifty else config.SENSEX_STRIKE_STEP
    chain     = build_option_chain(fyers, sym, ltp, step, is_index=True)
    sr        = calculate_sr(df, ltp)

    # Generate index trade with live prices
    index_trade = generate_index_trade(fyers, sym, ltp, chain, sr, df_today, name)

    weekly  = expiry_fn(n=8)
    monthly = get_monthly_expiry(1 if is_nifty else 3, 3)
    pc      = q.get("prev_close", ltp)
    chg_pts = round(ltp - pc, 2)
    chg_pct = round((chg_pts / pc * 100) if pc > 0 else 0, 2)

    return {
        "name": name, "ltp": round(ltp,2), "chg_pts": chg_pts, "chg_pct": chg_pct,
        "open": round(q.get("open",ltp),2), "high": round(q.get("high",ltp),2),
        "low":  round(q.get("low",ltp),2),  "vwap": round(vwap,2),
        "chain": chain,
        "support":    round(sr.get("support",0),2),
        "resistance": round(sr.get("resistance",0),2),
        "pivot": sr.get("pivot",0), "r1": sr.get("r1",0), "r2": sr.get("r2",0),
        "s1": sr.get("s1",0), "s2": sr.get("s2",0),
        "intraday_support":    sr.get("intraday_support",0),
        "intraday_resistance": sr.get("intraday_resistance",0),
        "expiry_day":     "Tuesday" if is_nifty else "Thursday",
        "current_expiry": fmt_exp(weekly[0]) if weekly else "—",
        "weekly_expiries":  [{"date": d.isoformat(), "label": fmt_exp(d)} for d in weekly],
        "monthly_expiries": [{"date": d.isoformat(), "label": fmt_exp(d)} for d in monthly],
        "index_trade": index_trade,
        "scanned_at":  datetime.now().isoformat(),
    }


def generate_index_trade(fyers, sym, ltp, chain, sr, df_today, name):
    """
    FIX 1 & 4: Index trade with full lock mechanism and correct expiry.
    - Trade is locked once given — never replaced mid-session
    - Uses ONLY nearest weekly expiry for pricing
    - Fetches live option prices from that specific expiry
    """
    if not chain or ltp == 0: return {}

    is_nifty = "NIFTY" in name and "SENSEX" not in name
    display  = "NIFTY50" if is_nifty else "SENSEX"
    base     = "NIFTY" if is_nifty else "SENSEX"
    exch     = "NSE" if is_nifty else "BSE"
    step     = config.NIFTY_STRIKE_STEP if is_nifty else config.SENSEX_STRIKE_STEP

    # ── FIX 1: Check if locked trade already exists today ───────────────────
    locked = trade_tracker.get_locked_trade(display)
    if locked:
        strike_str = locked.get("strike", "")
        parts = strike_str.split()
        locked_status = locked.get("status", "ACTIVE")
        # Only fetch LTP if ACTIVE — stop fetching after SL/target hit
        if locked_status == "ACTIVE" and len(parts) == 2:
            try:
                orig_strike   = int(parts[0])
                orig_opt_type = parts[1]
                orig_exp_str  = locked.get("expiry_date", "")
                orig_expiry   = date.fromisoformat(orig_exp_str) if orig_exp_str else (get_nifty_expiries(1)[0] if is_nifty else get_sensex_expiries(1)[0])
                time.sleep(0.2)
                _, _, curr_ltp, _ = get_live_option_price(fyers, base, exch, orig_expiry, orig_strike, orig_opt_type)
                if curr_ltp > 0:
                    trade_tracker.update_status(display, strike_str, curr_ltp)
            except Exception as e:
                log.debug(f"  {display}: locked trade LTP update error: {e}")
        elif locked_status != "ACTIVE":
            log.debug(f"  {display}: trade {locked_status} — LTP fetch skipped")
            curr_ltp = locked.get("option_ltp", 0)
        status_label = trade_tracker.get_status_label(display, strike_str) or f"Given at {locked.get('given_at','')}"
        # Return locked trade data with updated LTP
        return {
            "bias":        locked.get("direction",""),
            "strike":      strike_str,
            "expiry":      locked.get("expiry",""),
            "expiry_date": locked.get("expiry_date",""),
            "entry":       locked.get("entry",0),
            "sl_price":    locked.get("sl_price",0),
            "tgt_price":   locked.get("tgt_price",0),
            "sl_amt":      locked.get("sl_amt",0),
            "tgt_amt":     locked.get("tgt_amt",0),
            "rr":          locked.get("rr_ratio","1:2"),
            "current_ltp": curr_ltp if "curr_ltp" in dir() and curr_ltp > 0 else locked.get("option_ltp",0),
            "option_ltp":  curr_ltp if "curr_ltp" in dir() and curr_ltp > 0 else locked.get("option_ltp",0),
            "given_at":    locked.get("given_at",""),
            "reason":      locked.get("oi_note",""),
            "support":     sr.get("support",0),
            "resistance":  sr.get("resistance",0),
            "pcr":         chain.get("pcr",0),
            "max_pain":    chain.get("max_pain",0),
            "status_label": status_label,
            "locked":      True,
        }

    # ── No locked trade: generate a new one ─────────────────────────────────
    bias = chain.get("bias", "NO DATA")
    atm  = chain.get("atm_strike", round(ltp / step) * step)

    if "SIDEWAYS" in bias or "NO DATA" in bias:
        if not df_today.empty and len(df_today) >= 3:
            # Use EMA direction as tiebreaker
            closes = df_today["close"]
            ema9  = closes.ewm(span=9,  adjust=False).mean().iloc[-1]
            ema21 = closes.ewm(span=21, adjust=False).mean().iloc[-1]
            if ema9 > ema21 and ltp > closes.iloc[0]:
                bias = "BULLISH"
            elif ema9 < ema21 and ltp < closes.iloc[0]:
                bias = "BEARISH"
            else:
                log.info(f"  {display}: Sideways + no EMA clarity. Skipping index trade.")
                return {}
        else:
            return {}

    opt_type = "CE" if "BULLISH" in bias else "PE"
    direction = "BULL" if opt_type == "CE" else "BEAR"

    # ── FIX 4: Use ONLY nearest weekly expiry, fetch prices from that expiry ─
    expiry = get_nifty_expiries(1)[0] if is_nifty else get_sensex_expiries(1)[0]
    exp_l  = fmt_exp(expiry)

    # Fetch live Ask price for the ATM option at this specific expiry
    ask, bid, ltp_opt, sym_used = get_live_option_price(fyers, base, exch, expiry, atm, opt_type)
    if ask == 0:
        log.warning(f"  {display}: No live option price for {atm} {opt_type} exp:{exp_l}. Skipping.")
        return {}

    entry = ask
    sl_price, tgt_price, sl_amt, tgt_amt, rr = dynamic_rr(entry, chain, opt_type, ltp, step, vix=_get_vix(fyers))

    if ltp_opt >= tgt_price:
        log.info(f"  {display}: Option already past target. Skip.")
        return {}

    given_at   = datetime.now().strftime("%H:%M")
    strike_str = f"{atm} {opt_type}"
    today_str  = date.today().isoformat()

    # ── Register trade so it gets locked ─────────────────────────────────────
    extra = {
        "score": 8.0, "oi_signal": "—", "oi_dir": direction,
        "oi_note": chain.get("recommendation",{}).get("bias_note","Based on PCR+EMA"),
        "direction": direction, "in_kill_zone": False,
        "option_type": opt_type, "expiry": exp_l,
        "expiry_date": expiry.isoformat(), "option_ltp": round(ltp_opt,1),
        "sl_amt": sl_amt, "tgt_amt": tgt_amt, "rr_ratio": rr,
        "iv_rank": 50, "vol_surge": 1.0, "atr_pct": 0,
        "vwap": chain.get("vwap",0), "in_window": False,
    }
    trade_tracker.register_trade(display, direction, strike_str, entry, sl_price, tgt_price,
                                 f"Index {bias}", given_at, expiry=exp_l,
                                 expiry_date=expiry.isoformat(),
                                 sector="Index", extra=extra)
    log.info(f"  {display}: New trade → {strike_str} entry:₹{entry} sl:₹{sl_price} tgt:₹{tgt_price} rr:{rr}")

    return {
        "bias":        bias,
        "strike":      strike_str,
        "expiry":      exp_l,
        "expiry_date": expiry.isoformat(),
        "entry":       entry,
        "sl_price":    sl_price,
        "tgt_price":   tgt_price,
        "sl_amt":      sl_amt,
        "tgt_amt":     tgt_amt,
        "rr":          rr,
        "current_ltp": ltp_opt,
        "given_at":    given_at,
        "reason":      chain.get("recommendation",{}).get("bias_note","Based on PCR+EMA"),
        "support":     sr.get("support",0),
        "resistance":  sr.get("resistance",0),
        "pcr":         chain.get("pcr",0),
        "max_pain":    chain.get("max_pain",0),
        "status_label": f"Given at {given_at}",
        "locked":      False,
    }


# ==============================================================================
# TECHNICAL INDICATORS
# ==============================================================================

def _calc_vwap(df):
    if df.empty or df["volume"].sum() == 0: return 0.0
    tp = (df["high"] + df["low"] + df["close"]) / 3
    return round(float((tp * df["volume"]).sum() / df["volume"].sum()), 2)

def _calc_atr(df, p=14):
    if len(df) < p+1: return 0.0
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return round(float(tr.rolling(p).mean().iloc[-1]), 2)

def _calc_ema(s, p): return s.ewm(span=p, adjust=False).mean()

def _avg_vol(df):
    if df.empty: return 0.0
    df = df.copy()
    df["date"] = df["datetime"].dt.date
    daily = df.groupby("date")["volume"].sum()
    return float(daily.iloc[:-1].tail(20).mean()) if len(daily) > 1 else 0.0

def _det_orb(dft):
    if len(dft) < 3: return {"signal":None,"name":""}
    hi = dft.iloc[:3]["high"].max(); lo = dft.iloc[:3]["low"].min()
    cur = dft.iloc[-1]; avg = dft["volume"].mean()
    if cur["close"] > hi and cur["volume"] > avg*1.1: return {"signal":"BULL","name":"ORB Break"}
    if cur["close"] < lo and cur["volume"] > avg*1.1: return {"signal":"BEAR","name":"ORB Break (Bear)"}
    return {"signal":None,"name":""}

def _det_vwap(dft, vwap):
    if len(dft) < 2 or vwap == 0: return {"signal":None,"name":""}
    p, c = dft.iloc[-2]["close"], dft.iloc[-1]["close"]
    if p < vwap < c: return {"signal":"BULL","name":"VWAP Reclaim"}
    if p > vwap > c: return {"signal":"BEAR","name":"VWAP Rejection"}
    return {"signal":None,"name":""}

def _det_pdh(df):
    if df.empty: return {"signal":None,"name":""}
    df = df.copy(); df["date"] = df["datetime"].dt.date
    days = sorted(df["date"].unique())
    if len(days) < 2: return {"signal":None,"name":""}
    prev = df[df["date"]==days[-2]]; td = df[df["date"]==days[-1]]
    if prev.empty or td.empty: return {"signal":None,"name":""}
    c = td.iloc[-1]["close"]
    if c > prev["high"].max(): return {"signal":"BULL","name":"PDH Break"}
    if c < prev["low"].min():  return {"signal":"BEAR","name":"PDL Break"}
    return {"signal":None,"name":""}

def _det_ema(df):
    if len(df) < 55: return {"signal":None,"name":""}
    c = df["close"]
    e9,e21,e50 = _calc_ema(c,9).iloc[-1], _calc_ema(c,21).iloc[-1], _calc_ema(c,50).iloc[-1]
    if e9>e21>e50: return {"signal":"BULL","name":"EMA Stack (Bull)"}
    if e9<e21<e50: return {"signal":"BEAR","name":"EMA Stack (Bear)"}
    return {"signal":None,"name":""}

def _det_gap(op, pc):
    if pc == 0: return {"signal":None,"name":""}
    g = (op - pc) / pc * 100
    if g >= 0.3:  return {"signal":"BULL","name":"Gap Up + Volume"}
    if g <= -0.3: return {"signal":"BEAR","name":"Gap Down + Volume"}
    return {"signal":None,"name":""}

def _det_st(df, p=10, m=3.0):
    if len(df) < p+5: return {"signal":None,"name":""}
    df = df.copy(); h, l, c = df["high"], df["low"], df["close"]
    tr   = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    atr  = tr.rolling(p).mean(); hl2 = (h+l)/2
    bu   = hl2 + m*atr; bl = hl2 - m*atr
    fu   = bu.copy(); fl = bl.copy()
    for i in range(1, len(df)):
        fu.iloc[i] = bu.iloc[i] if bu.iloc[i]<fu.iloc[i-1] or c.iloc[i-1]>fu.iloc[i-1] else fu.iloc[i-1]
        fl.iloc[i] = bl.iloc[i] if bl.iloc[i]>fl.iloc[i-1] or c.iloc[i-1]<fl.iloc[i-1] else fl.iloc[i-1]
    st = pd.Series(index=df.index, dtype=float)
    for i in range(1, len(df)):
        st.iloc[i] = fu.iloc[i] if c.iloc[i] <= fu.iloc[i] else fl.iloc[i]
    cc,cs,pc2,ps = c.iloc[-1], st.iloc[-1], c.iloc[-2], st.iloc[-2]
    if pc2<=ps and cc>cs: return {"signal":"BULL","name":"Supertrend Crossover (Bull)","st":round(cs,2)}
    if pc2>=ps and cc<cs: return {"signal":"BEAR","name":"Supertrend Crossover (Bear)","st":round(cs,2)}
    if cc>cs: return {"signal":"BULL","name":"Supertrend (Bull)","st":round(cs,2)}
    if cc<cs: return {"signal":"BEAR","name":"Supertrend (Bear)","st":round(cs,2)}
    return {"signal":None,"name":""}

def _det_breakout(df, quote):
    if len(df) < 30: return {"signal":None,"name":""}
    df = df.copy(); df["date"] = df["datetime"].dt.date
    days = sorted(df["date"].unique())
    past = df[df["date"]!=days[-1]] if len(days)>1 else df
    td   = df[df["date"]==days[-1]] if len(days)>1 else pd.DataFrame()
    if past.empty or td.empty: return {"signal":None,"name":""}
    h20  = past.groupby("date")["high"].max().tail(20).max()
    l20  = past.groupby("date")["low"].min().tail(20).min()
    curr = td.iloc[-1]["close"]; avg = past["volume"].mean(); tvol = td["volume"].sum()
    vs   = tvol/avg if avg>0 else 0
    if curr > h20 and vs >= 1.1: return {"signal":"BULL","name":"20D High Breakout"}
    if curr < l20 and vs >= 1.1: return {"signal":"BEAR","name":"20D Low Breakdown"}
    return {"signal":None,"name":""}

def _det_cpr(df):
    empty = {"signal":None,"name":"","cpr_top":0,"cpr_bot":0,"pivot":0}
    if df.empty: return empty
    df = df.copy(); df["date"] = df["datetime"].dt.date
    days = sorted(df["date"].unique())
    if len(days) < 2: return empty
    prev = df[df["date"]==days[-2]]; td = df[df["date"]==days[-1]]
    if prev.empty or td.empty: return empty
    ph,pl,pc = prev["high"].max(), prev["low"].min(), prev["close"].iloc[-1]
    pivot = (ph+pl+pc)/3; bc = (ph+pl)/2; tc = (pivot-bc)+pivot
    curr  = td.iloc[-1]["close"]
    if curr > tc: return {"signal":"BULL","name":"CPR Breakout","cpr_top":round(tc,2),"cpr_bot":round(bc,2),"pivot":round(pivot,2)}
    if curr < bc: return {"signal":"BEAR","name":"CPR Breakdown","cpr_top":round(tc,2),"cpr_bot":round(bc,2),"pivot":round(pivot,2)}
    return {"signal":None,"name":"Inside CPR","cpr_top":round(tc,2),"cpr_bot":round(bc,2),"pivot":round(pivot,2)}

def _det_ob(df):
    if len(df) < 10: return {"signal":None,"name":""}
    c=df["close"].values; o=df["open"].values; h=df["high"].values; l=df["low"].values
    for i in range(len(df)-5, max(0,len(df)-20), -1):
        if c[i]<o[i] and i+2<len(df) and c[i+1]>o[i+1] and c[i+2]>o[i+2]:
            if l[i]<=c[-1]<=h[i]*1.002: return {"signal":"BULL","name":"SMC Bullish Order Block"}
        if c[i]>o[i] and i+2<len(df) and c[i+1]<o[i+1] and c[i+2]<o[i+2]:
            if l[i]*0.998<=c[-1]<=h[i]: return {"signal":"BEAR","name":"SMC Bearish Order Block"}
    return {"signal":None,"name":""}

def _det_fvg(df):
    if len(df) < 5: return {"signal":None,"name":""}
    curr = df["close"].iloc[-1]
    for i in range(len(df)-5, max(0,len(df)-20), -1):
        if i+2 >= len(df): continue
        h0,l0 = df["high"].iloc[i], df["low"].iloc[i]
        h2,l2 = df["high"].iloc[i+2], df["low"].iloc[i+2]
        if l2>h0 and h0<=curr<=l2: return {"signal":"BULL","name":"ICT Fair Value Gap (Bull)"}
        if h2<l0 and h2<=curr<=l0: return {"signal":"BEAR","name":"ICT Fair Value Gap (Bear)"}
    return {"signal":None,"name":""}

def _det_bos(df):
    if len(df) < 20: return {"signal":None,"name":""}
    h=df["high"].values; l=df["low"].values; c=df["close"].values; n=len(df)
    def sh(i): return h[i]>h[i-1] and h[i]>h[i+1] if 0<i<n-1 else False
    def sl(i): return l[i]<l[i-1] and l[i]<l[i+1] if 0<i<n-1 else False
    shs = [h[i] for i in range(n-15,n-1) if sh(i)]
    sls = [l[i] for i in range(n-15,n-1) if sl(i)]
    if shs and c[-1]>max(shs) and c[-2]<=max(shs): return {"signal":"BULL","name":"SMC Break of Structure (Bull)"}
    if sls and c[-1]<min(sls) and c[-2]>=min(sls): return {"signal":"BEAR","name":"SMC Break of Structure (Bear)"}
    return {"signal":None,"name":""}

def _det_sweep(df):
    if len(df) < 10: return {"signal":None,"name":""}
    l=df["low"].values; h=df["high"].values; c=df["close"].values; n=len(df)
    sl = [l[i] for i in range(max(0,n-12), n-2)]
    sh = [h[i] for i in range(max(0,n-12), n-2)]
    if not sl: return {"signal":None,"name":""}
    if l[-2]<min(sl) and c[-2]<l[-2] and c[-1]>min(sl): return {"signal":"BULL","name":"ICT Liquidity Sweep (Bull)"}
    if h[-2]>max(sh) and c[-2]>h[-2] and c[-1]<max(sh): return {"signal":"BEAR","name":"ICT Liquidity Sweep (Bear)"}
    return {"signal":None,"name":""}

def _det_pin(df):
    if len(df) < 3: return {"signal":None,"name":""}
    cd = df.iloc[-1]; body = abs(cd["close"]-cd["open"]); total = cd["high"]-cd["low"]
    if total == 0: return {"signal":None,"name":""}
    lw = min(cd["open"],cd["close"]) - cd["low"]
    uw = cd["high"] - max(cd["open"],cd["close"])
    if body/total < 0.30:
        if lw/total > 0.60: return {"signal":"BULL","name":"Bullish Pin Bar"}
        if uw/total > 0.60: return {"signal":"BEAR","name":"Bearish Pin Bar"}
    return {"signal":None,"name":""}

def _det_eng(df):
    if len(df) < 2: return {"signal":None,"name":""}
    prev=df.iloc[-2]; curr=df.iloc[-1]
    pbt=max(prev["open"],prev["close"]); pbb=min(prev["open"],prev["close"])
    cbt=max(curr["open"],curr["close"]); cbb=min(curr["open"],curr["close"])
    pb=prev["close"]>prev["open"]; cb=curr["close"]>curr["open"]
    if not pb and cb and cbt>pbt and cbb<pbb: return {"signal":"BULL","name":"Bullish Engulfing"}
    if pb and not cb and cbt>pbt and cbb<pbb: return {"signal":"BEAR","name":"Bearish Engulfing"}
    return {"signal":None,"name":""}

def _det_kz(dft):
    now = datetime.now(); t = now.hour*60 + now.minute
    kz1 = (9*60+15) <= t <= (10*60+30)
    kz2 = (13*60+30) <= t <= (14*60+30)
    if (kz1 or kz2) and not dft.empty and len(dft) >= 2:
        nm = "Morning Kill Zone" if kz1 else "Afternoon Kill Zone"
        if dft.iloc[-1]["close"] > dft.iloc[-2]["close"]: return {"signal":"BULL","name":nm,"in_kz":True}
        if dft.iloc[-1]["close"] < dft.iloc[-2]["close"]: return {"signal":"BEAR","name":nm,"in_kz":True}
    return {"signal":None,"name":"","in_kz":False}


def detect_oi_signal(price_chg, oi_chg_pct):
    pt, ot = config.PRICE_CHG_THRESHOLD, config.OI_CHANGE_THRESHOLD
    if   price_chg > pt  and oi_chg_pct > ot:  return {"signal":"Long Buildup",   "dir":"BULL","score":2.0,"note":"Sustained move. Good for CE buying."}
    elif price_chg > pt  and oi_chg_pct < -ot: return {"signal":"Short Covering", "dir":"BULL","score":1.5,"note":"Short squeeze. Act fast."}
    elif price_chg < -pt and oi_chg_pct > ot:  return {"signal":"Short Buildup",  "dir":"BEAR","score":2.0,"note":"Sustained move. Good for PE buying."}
    elif price_chg < -pt and oi_chg_pct < -ot: return {"signal":"Long Unwinding", "dir":"BEAR","score":0.5,"note":"Longs exiting."}
    else:                                        return {"signal":"—","dir":None,"score":0.0,"note":"No clear OI signal."}


# ==============================================================================
# STOCK SCORING
# ==============================================================================

def score_stock(fyers, sym, quote, df, vix):
    display    = sym.replace("NSE:","").replace("-EQ","")
    sector     = config.SECTOR_MAP.get(display, "F&O")
    ltp        = quote.get("ltp", 0)
    if ltp == 0: return None

    # ── FIX 3: CHECK IF THIS STOCK ALREADY HAS A TRADE TODAY ──────────────
    # If yes: return the LOCKED existing trade with updated SL/Target status.
    # Never generate a second trade for the same stock on the same day.
    existing = trade_tracker.get_locked_trade(display)
    if existing:
        strike_str = existing.get("strike","")
        parts      = strike_str.split()
        existing_status = existing.get("status", "ACTIVE")
        # Only fetch LTP if trade is ACTIVE — stop fetching once SL/target hit
        if existing_status == "ACTIVE" and len(parts) == 2:
            try:
                orig_strike   = int(parts[0])
                orig_opt_type = parts[1]
                orig_expiry_s = existing.get("expiry_date","")
                try:    orig_expiry = date.fromisoformat(orig_expiry_s)
                except: orig_expiry = get_nifty_expiries(1)[0]
                time.sleep(0.2)
                _, _, current_ltp, _ = get_live_option_price(
                    fyers, display, "NSE", orig_expiry, orig_strike, orig_opt_type)
                if current_ltp > 0:
                    trade_tracker.update_status(display, strike_str, current_ltp)
            except Exception as e:
                log.debug(f"  {display}: locked trade price fetch error: {e}")
        elif existing_status != "ACTIVE":
            log.debug(f"  {display}: trade {existing_status} — LTP fetch skipped")
            current_ltp = existing.get("option_ltp", 0)
        status_label = trade_tracker.get_status_label(display, strike_str) or f"Given at {existing.get('given_at','')}"
        # Return the original trade data (locked — unchanged)
        return {
            "symbol":      display,
            "sector":      sector,
            "price":       round(ltp, 2),
            "price_chg":   round(((ltp - quote.get("prev_close",ltp)) / quote.get("prev_close",ltp) * 100) if quote.get("prev_close",ltp) > 0 else 0, 2),
            "score":       existing.get("score", 5.0),
            "oi_signal":   existing.get("oi_signal","—"),
            "oi_dir":      existing.get("oi_dir",""),
            "oi_note":     existing.get("oi_note","Locked trade — original signal retained."),
            "setup":       existing.get("setup",""),
            "direction":   existing.get("direction",""),
            "in_kill_zone": existing.get("in_kill_zone", False),
            "strike":      existing.get("strike",""),
            "option_type": existing.get("option_type",""),
            "expiry":      existing.get("expiry",""),
            "expiry_date": existing.get("expiry_date",""),
            "option_ltp":  current_ltp if "current_ltp" in dir() and current_ltp > 0 else existing.get("option_ltp", 0),
            "last_ltp_update": datetime.now().isoformat() if ("current_ltp" in dir() and current_ltp > 0) else existing.get("last_ltp_update",""),
            "entry":       existing.get("entry", 0),
            "sl_price":    existing.get("sl_price", 0),
            "tgt_price":   existing.get("tgt_price", 0),
            "sl_amt":      existing.get("sl_amt", 0),
            "tgt_amt":     existing.get("tgt_amt", 0),
            "rr_ratio":    existing.get("rr_ratio","1:2"),
            "iv_rank":     existing.get("iv_rank", 0),
            "vol_surge":   existing.get("vol_surge", 1.0),
            "atr_pct":     existing.get("atr_pct", 0),
            "vwap":        existing.get("vwap", 0),
            "in_window":   existing.get("in_window", False),
            "signal_time": existing.get("given_at",""),
            "date":        existing.get("date", date.today().isoformat()),
            "status_label": status_label,
            "locked":      True,   # flag: this is a locked trade, not new
            "scanned_at":  datetime.now().isoformat(),
        }
    # ── END LOCK CHECK ─────────────────────────────────────────────────────

    prev_close = quote.get("prev_close", ltp)
    price_chg  = ((ltp - prev_close) / prev_close * 100) if prev_close > 0 else 0
    if abs(price_chg) < 0.15: return None

    today_date = date.today()
    df_today   = df[df["datetime"].dt.date == today_date].copy() if not df.empty else pd.DataFrame()
    avg_vol    = _avg_vol(df)
    today_vol  = quote.get("volume", 0)
    vol_surge  = (today_vol / avg_vol) if avg_vol > 0 else 1.0
    if vol_surge < 1.2: return None  # FIX 10: Raised from 1.0 to 1.2

    atr     = _calc_atr(df)
    atr_pct = (atr / ltp * 100) if ltp > 0 else 1.0
    if vix > 35: return None
    vwap    = _calc_vwap(df_today)

    oi_chg_pct = 2.5 if abs(price_chg) > 0.5 else 0
    oi_result  = detect_oi_signal(price_chg, oi_chg_pct)

    kz = _det_kz(df_today)
    candidates = [
        _det_sweep(df), _det_bos(df), _det_ob(df), _det_fvg(df),
        _det_eng(df), _det_pin(df), _det_st(df), _det_breakout(df, quote),
        _det_orb(df_today), _det_gap(quote.get("open", ltp), prev_close),
        _det_cpr(df), _det_vwap(df_today, vwap), _det_pdh(df), _det_ema(df),
    ]

    tech = None
    for c in candidates:
        if c.get("signal") in ("BULL","BEAR"): tech=c; break

    # FIX 10: Kill Zone only if strong price move + vol surge (reduces noise)
    if not tech and kz.get("signal") in ("BULL","BEAR") and abs(price_chg)>0.5 and vol_surge>=1.5:
        tech = kz
    # Pure momentum: only if very strong move + high vol surge (reduces fake signals)
    if not tech and abs(price_chg)>0.8 and vol_surge>=2.0:
        tech = {"signal":"BULL" if price_chg>0 else "BEAR","name":"Price Action Momentum"}
    # OI-only fallback removed — too many false signals
    if not tech: return None

    direction = tech["signal"]

    # ── FIX 1 & 10: FAKE BREAKOUT FILTER ──────────────────────────────────
    # A breakout is fake if:
    # 1. Price breaks a level but closes BACK below/above it (wick rejection)
    # 2. Volume on the breakout candle is < 1.2x average (weak conviction)
    # 3. The last 2 candles contradict the signal direction
    if not df_today.empty and len(df_today) >= 3:
        last3 = df_today.iloc[-3:]
        last_c = last3.iloc[-1]
        prev_c = last3.iloc[-2]
        body_last = abs(last_c["close"] - last_c["open"])
        total_last = last_c["high"] - last_c["low"]
        # Wick rejection: large upper wick on bullish signal = fake bull breakout
        if direction == "BULL" and total_last > 0:
            upper_wick = last_c["high"] - max(last_c["close"], last_c["open"])
            if upper_wick / total_last > 0.55 and body_last / total_last < 0.35:
                log.debug(f"  {display}: BULL signal rejected — wick rejection candle")
                return None
        # Wick rejection: large lower wick on bearish signal = fake bear breakdown
        if direction == "BEAR" and total_last > 0:
            lower_wick = min(last_c["close"], last_c["open"]) - last_c["low"]
            if lower_wick / total_last > 0.55 and body_last / total_last < 0.35:
                log.debug(f"  {display}: BEAR signal rejected — wick rejection candle")
                return None
        # Reversal check: last candle closed against signal direction
        if direction == "BULL" and last_c["close"] < last_c["open"] and prev_c["close"] < prev_c["open"]:
            log.debug(f"  {display}: BULL signal rejected — 2 consecutive bearish candles")
            return None
        if direction == "BEAR" and last_c["close"] > last_c["open"] and prev_c["close"] > prev_c["open"]:
            log.debug(f"  {display}: BEAR signal rejected — 2 consecutive bullish candles")
            return None

    # VWAP confluence: price must be on correct side of VWAP
    if vwap > 0:
        if direction == "BULL" and ltp < vwap * 0.998:
            log.debug(f"  {display}: BULL rejected — price {ltp} below VWAP {vwap}")
            return None
        if direction == "BEAR" and ltp > vwap * 1.002:
            log.debug(f"  {display}: BEAR rejected — price {ltp} above VWAP {vwap}")
            return None
    # ── END FAKE BREAKOUT FILTER ───────────────────────────────────────────

    step      = config.STOCK_STRIKE_STEP.get(display, config.STOCK_STRIKE_STEP["DEFAULT"])
    atm_s     = round(ltp / step) * step
    opt_type  = "CE" if direction == "BULL" else "PE"

    # Stocks use NSE F&O weekly expiry = every Thursday (NOT Nifty Tuesday expiry)
    expiry = get_stock_weekly_expiry()

    # Fetch ACTUAL live option price — small delay to avoid rate limits
    time.sleep(0.4)
    ask, bid, ltp_opt, sym_used = get_live_option_price(fyers, display, "NSE", expiry, atm_s, opt_type)
    if ask == 0:
        log.debug(f"  {display}: No live option price for {atm_s} {opt_type}. Skip.")
        return None

    entry    = ask
    sl_price, tgt_price, sl_amt, tgt_amt, rr = dynamic_rr(entry, None, opt_type, ltp, step, vix=float(vix) if vix else 18.0)

    # Validity check
    if ltp_opt >= tgt_price:
        log.info(f"  {display}: Option {atm_s} {opt_type} LTP {ltp_opt} >= target. Skip.")
        return None
    if ltp_opt > 0 and ltp_opt <= sl_price:
        log.info(f"  {display}: Option {atm_s} {opt_type} LTP {ltp_opt} <= SL. Skip.")
        return None

    # Score
    name = tech.get("name","")
    if "SMC" in name or "ICT" in name:                                 ts = 3.0
    elif "Crossover" in name or "Sweep" in name:                       ts = 2.8
    elif "Engulfing" in name or "Pin Bar" in name:                     ts = 2.5
    elif name in ["ORB Break","ORB Break (Bear)","Gap Up + Volume","Gap Down + Volume"]: ts = 2.5
    elif "20D" in name:                                                ts = 2.4
    elif "Kill Zone" in name:                                          ts = 2.2
    elif name in ["CPR Breakout","CPR Breakdown","VWAP Reclaim","VWAP Rejection"]: ts = 2.0
    elif name in ["PDH Break","PDL Break"]:                            ts = 1.8
    elif "Supertrend" in name:                                         ts = 1.6
    elif "EMA Stack" in name:                                          ts = 1.4
    else:                                                              ts = 1.0

    vs    = 2.0 if vol_surge>=3.0 else (1.5 if vol_surge>=2.0 else (1.0 if vol_surge>=1.5 else 0.7))
    ois   = oi_result["score"]
    ivs   = 2.0 if 1.0<atr_pct<3.0 else (1.5 if atr_pct>=3.0 else 1.0)
    kzb   = 0.5 if kz.get("in_kz") else 0
    raw_total = round(vs + ois + ts + ivs + kzb, 1)

    # Apply learned multipliers from weekly analysis
    signal_hour = datetime.now().hour
    total = learner.apply_learnings(raw_total, tech.get("name",""), direction, vol_surge, signal_hour)

    # Use learned vol_threshold (improves over time based on historical data)
    min_vol = max(1.3, learner.get_optimal_vol_threshold())

    # FIX 10: Require score >= 5.0 AND vol_surge >= learned threshold
    if total < 5.0: return None
    if vol_surge < min_vol:
        log.debug(f"  {display}: score {total} but vol_surge {vol_surge:.1f} < {min_vol:.1f}. Skip.")
        return None

    given_at   = datetime.now().strftime("%H:%M")
    strike_str = f"{atm_s} {opt_type}"

    # IV Rank adjustment — check if options are cheap or expensive to buy
    iv_rank_data = {}
    try:
        import analytics as _an
        iv_rank_data = _an.get_iv_rank_for_symbol(display)
        iv_adj = _an.iv_rank_score_adjustment(iv_rank_data, direction)
        if iv_adj != 0:
            total = round(total + iv_adj, 2)
            log.debug(f"  {display}: IV rank adj {iv_adj:+.1f} (rank={iv_rank_data.get('iv_rank',50):.0f}%) → score={total}")
    except Exception as _ive:
        log.debug(f"  {display}: IV rank lookup failed: {_ive}")

    # Compute OI context note BEFORE extra_fields (oi_signal_raw used below)
    oi_signal_raw = oi_result["signal"]
    oi_dir_raw    = oi_result["dir"]

    if direction == "BULL":
        if oi_signal_raw == "Long Buildup":
            context_note = "Long Buildup — price rising with rising OI. Good for CE buying."
        elif oi_signal_raw == "Short Covering":
            context_note = "Short Covering — shorts exiting. Bullish momentum. CE play."
        else:
            context_note = f"Technical setup ({name}) signals bullish. Consider CE entry."
    else:
        if oi_signal_raw == "Short Buildup":
            context_note = "Short Buildup — price falling with rising OI. Good for PE buying."
        elif oi_signal_raw == "Long Unwinding":
            context_note = "Long Unwinding — longs exiting. Bearish bias. PE play."
        elif oi_signal_raw == "Long Buildup":
            context_note = f"OI shows Long Buildup but {name} signals bearish reversal. Technical setup dominates."
        else:
            context_note = f"Technical setup ({name}) signals bearish. Consider PE entry."

    # Register trade — stores the full trade record for locking
    extra_fields = {
        "score":       total,
        "oi_signal":   oi_signal_raw,
        "oi_dir":      oi_dir_raw,
        "oi_note":     context_note,
        "direction":   direction,
        "in_kill_zone": kz.get("in_kz", False),
        "option_type": opt_type,
        "expiry":      fmt_exp(expiry),
        "expiry_date": expiry.isoformat(),
        "option_ltp":  round(ltp_opt,1),
        "last_ltp_update": datetime.now().isoformat(),
        "sl_amt":      sl_amt,
        "tgt_amt":     tgt_amt,
        "rr_ratio":    rr,
        "iv_rank":     round(min(atr_pct*12, 80), 1),
        "vol_surge":   round(vol_surge, 1),
        "atr_pct":     round(atr_pct, 2),
        "vwap":        round(vwap, 2),
        "in_window":   kz.get("in_kz", False),
    }
    trade_tracker.register_trade(display, direction, strike_str, entry, sl_price, tgt_price,
                                  name, given_at, expiry=fmt_exp(expiry),
                                  expiry_date=expiry.isoformat(),
                                  sector=sector, extra=extra_fields)
    status_label = trade_tracker.get_status_label(display, strike_str) or f"Given at {given_at}"

    extra = {}
    if "CPR" in name: extra = {"cpr_top": tech.get("cpr_top",0), "cpr_bot": tech.get("cpr_bot",0)}
    if "Supertrend" in name: extra["st_value"] = tech.get("st", 0)


    return {
        "symbol": display, "sector": sector, "price": round(ltp,2), "price_chg": round(price_chg,2),
        "score": total, "oi_signal": oi_signal_raw, "oi_dir": oi_dir_raw,
        "oi_note": context_note, "setup": name, "direction": direction,
        "in_kill_zone": kz.get("in_kz", False),
        "strike": f"{atm_s} {opt_type}", "option_type": opt_type,
        "expiry": fmt_exp(expiry),
        "option_ask": round(ask,1), "option_bid": round(bid,1), "option_ltp": round(ltp_opt,1),
        "entry": entry, "sl_price": sl_price, "tgt_price": tgt_price,
        "sl_amt": sl_amt, "tgt_amt": tgt_amt, "rr_ratio": rr,
        "iv_rank": round(min(atr_pct*12, 80), 1),
        "vol_surge": round(vol_surge, 1), "atr_pct": round(atr_pct, 2),
        "vwap": round(vwap, 2), "in_window": kz.get("in_kz", False),
        "signal_time": given_at, "date": date.today().isoformat(),
        "status_label": status_label,
        "scanned_at": datetime.now().isoformat(), **extra,
    }


# ==============================================================================
# NEWS
# ==============================================================================

def fetch_news():
    """Fetch news via news_fetcher module with filtering and timezone handling."""
    try:
        import news_fetcher
        articles = news_fetcher.fetch_news()
        # Convert to scanner format for compatibility
        news = []
        for a in articles:
            news.append({
                "headline": a.get("title",""),
                "source": a.get("source",""),
                "link": a.get("link",""),
                "published": a.get("published",""),
                "time": a.get("display_time",""),
                "category": a.get("category","GENERAL"),
                "impact": a.get("impact","LOW"),
                "datetime_sort": a.get("published",""),
            })
        log.info(f"Fetched {len(news)} news articles")
        return news
    except Exception as e:
        log.warning(f"News fetch error: {e}")
        return []

def _analyze(headline):
    h = headline.lower()
    bull = [kw for kw in BULLISH_KW if kw in h]
    bear = [kw for kw in BEARISH_KW if kw in h]
    impact = "BULLISH" if len(bull)>len(bear) else ("BEARISH" if len(bear)>len(bull) else ("BULLISH" if bull else ("BEARISH" if bear else "NEUTRAL")))
    aff = [k for k, kws in STOCK_KW.items() if any(kw in h for kw in kws)]
    if not aff: aff = ["NIFTY"]
    lbl = {"BANKING":"Banking","IT":"IT sector","PHARMA":"Pharma","AUTO":"Auto","NIFTY":"Nifty/Sensex"}
    return impact, ", ".join(lbl.get(a,a) for a in aff[:3]), _reason(impact, bull, bear)

def _reason(impact, bull, bear):
    if impact == "BULLISH":
        if any(k in bull for k in ["rate cut","repo cut"]): return "Rate cut = positive for earnings"
        if "fii buying" in bull: return "FII inflows = buying pressure"
        if any(k in bull for k in ["crude falls","oil drops"]): return "Lower crude = positive for India"
        return "Positive trigger for Indian market"
    if impact == "BEARISH":
        if any(k in bear for k in ["hawkish","rate hike"]): return "Hawkish Fed = FII outflows"
        if any(k in bear for k in ["crude rises","oil jumps"]): return "Higher crude = India import costs"
        if "fii selling" in bear: return "FII outflows = selling pressure"
        return "Negative trigger for Indian market"
    return "Monitoring for impact"

def _parse_time(pub):
    try:
        from email.utils import parsedate_to_datetime
        ist = parsedate_to_datetime(pub) + timedelta(hours=5, minutes=30)
        return ist.strftime("%I:%M %p"), ist.strftime("%d %b")
    except:
        n = datetime.now()
        return n.strftime("%I:%M %p"), n.strftime("%d %b")


# ==============================================================================
# MARKET TIMING
# ==============================================================================

def _market_open():
    now = datetime.now()
    if now.weekday() >= 5: return False
    t = now.hour * 60 + now.minute
    return (config.MARKET_OPEN_HOUR*60 + config.MARKET_OPEN_MIN) <= t <= (config.MARKET_CLOSE_HOUR*60 + config.MARKET_CLOSE_MIN)


# ==============================================================================
# MASTER SCAN — FIX #6: Index trades included in overview
# ==============================================================================

def scan_all(fyers):
    t0 = datetime.now()
    log.info("="*50)
    log.info(f"SCAN — {t0.strftime('%H:%M:%S')}")
    log.info("="*50)

    vix = fetch_quotes(fyers, [config.VIX_SYMBOL]).get(config.VIX_SYMBOL, {}).get("ltp", 15.0)
    log.info(f"  VIX: {vix}")
    time.sleep(1.0)  # rate limit buffer

    nifty  = scan_index(fyers, config.NIFTY_SYMBOL,  "NIFTY 50", get_nifty_expiries)
    time.sleep(2.0)  # rate limit buffer between index scans
    sensex = scan_index(fyers, config.SENSEX_SYMBOL, "SENSEX",   get_sensex_expiries)
    expiry_cal = build_expiry_calendar()
    news = fetch_news()  # fetch news early so _save_partial can use it
    time.sleep(2.0)  # buffer before stock scan

    log.info(f"  Scanning {len(config.FNO_UNIVERSE)} stocks...")
    # Fetch all stock quotes in small batches with delays built into fetch_quotes
    all_quotes = fetch_quotes(fyers, config.FNO_UNIVERSE, batch_size=25, delay=0.8)
    results = []

    for sym in config.FNO_UNIVERSE:
        q = all_quotes.get(sym, {})
        if not q or q.get("ltp",0) == 0: continue
        ltp = q.get("ltp",0); pc = q.get("prev_close",ltp)
        pchg = ((ltp-pc)/pc*100) if pc>0 else 0
        if abs(pchg) < 0.15: continue
        display = sym.replace("NSE:","").replace("-EQ","")
        log.info(f"    {display} ₹{ltp:.1f} ({pchg:+.1f}%)")
        df = fetch_candles(fyers, sym, tf=5, days=10)
        result = score_stock(fyers, sym, q, df, vix)
        if result:
            results.append(result)
            log.info(f"    ✓ {result['score']} | {result['setup']} | {result['direction']} | Entry:₹{result['entry']} | LTP:₹{result.get('option_ltp',0)} | {result['strike']}")
            # FIX 2: Save partial cache immediately so dashboard shows new trade NOW
            # without waiting for the full scan to complete
            _save_partial(results, nifty, sensex, news, vix, t0)
            # Notify server of new trade for instant dashboard push
            try:
                import server
                server._notify_new_trade(result.get('symbol', 'UNKNOWN'), result)
            except Exception as e:
                log.debug(f"Could not notify server of new trade: {e}")
        time.sleep(0.25)

    # FIX #6: Add index trades to overview stocks list
    for idx_data, idx_name in [(nifty, "NIFTY50"), (sensex, "SENSEX")]:
        t = idx_data.get("index_trade", {})
        if t and t.get("entry", 0) > 0:
            results.append({
                "symbol":      idx_name,
                "sector":      "Index",
                "price":       idx_data.get("ltp", 0),
                "price_chg":   idx_data.get("chg_pct", 0),
                "score":       8.0,
                "oi_signal":   "—",
                "oi_dir":      t.get("bias",""),
                "oi_note":     t.get("reason",""),
                "setup":       f"Index {t.get('bias','')}",
                "direction":   "BULL" if "BULL" in t.get("bias","") else "BEAR",
                "in_kill_zone": False,
                "strike":      t.get("strike",""),
                "option_type": "CE" if "CE" in t.get("strike","") else "PE",
                "expiry":      t.get("expiry",""),
                "option_ask":  t.get("entry",0),
                "option_bid":  0,
                "option_ltp":  t.get("current_ltp",0),
                "entry":       t.get("entry",0),
                "sl_price":    t.get("sl_price",0),
                "tgt_price":   t.get("tgt_price",0),
                "sl_amt":      t.get("sl_amt",0),
                "tgt_amt":     t.get("tgt_amt",0),
                "rr_ratio":    t.get("rr","1:2"),
                "vol_surge":   1.0,
                "atr_pct":     0,
                "vwap":        idx_data.get("vwap",0),
                "in_window":   False,
                "signal_time": t.get("given_at",""),
                "status_label":f"Given at {t.get('given_at','')}",
                "scanned_at":  datetime.now().isoformat(),
            })

    results.sort(key=lambda x: x["score"], reverse=True)
    today_trades = trade_tracker.get_today_trades()

    return {
        "meta": {
            "scanned_at": t0.isoformat(),
            "scan_secs":  round((datetime.now()-t0).total_seconds(), 1),
            "vix":        round(float(vix), 2),
            "vix_env":    _vix_label(vix),
            "market_open": _market_open(),
            "total":  len([r for r in results if r.get("sector") != "Index"]),
            "strong": len([r for r in results if r.get("score",0)>=7]),
            "lb":     len([r for r in results if r.get("oi_signal")=="Long Buildup"]),
            "sc":     len([r for r in results if r.get("oi_signal")=="Short Covering"]),
            "sb":     len([r for r in results if r.get("oi_signal")=="Short Buildup"]),
            "news_count": len(news),
        },
        "expiry_calendar": expiry_cal,
        "nifty": nifty, "sensex": sensex,
        "stocks": results, "news": news, "today_trades": today_trades,
    }


def _vix_label(v):
    if v < 13: return "Very Low — options cheap"
    if v < 17: return "Favorable — good for buying"
    if v < 20: return "Moderate — widen stops"
    if v < 25: return "Elevated — reduce size"
    return "High — avoid buying"


def _save_partial(results, nifty, sensex, news, vix, t0):
    """FIX 2: Save partial scan results immediately so dashboard updates in real-time."""
    try:
        sorted_r = sorted(results, key=lambda x: x.get("score", 0), reverse=True)
        today_trades = trade_tracker.get_today_trades()
        payload = {
            "meta": {
                "scanned_at": t0.isoformat(),
                "scan_secs": round((datetime.now()-t0).total_seconds(), 1),
                "vix": round(float(vix), 2),
                "vix_env": _vix_label(vix),
                "market_open": _market_open(),
                "partial": True,  # flag: scan still running
                "total":  len([r for r in sorted_r if r.get("sector") != "Index"]),
                "strong": len([r for r in sorted_r if r.get("score",0)>=7]),
                "lb":     len([r for r in sorted_r if r.get("oi_signal")=="Long Buildup"]),
                "sc":     len([r for r in sorted_r if r.get("oi_signal")=="Short Covering"]),
                "sb":     len([r for r in sorted_r if r.get("oi_signal")=="Short Buildup"]),
                "news_count": len(news),
            },
            "nifty": nifty, "sensex": sensex,
            "stocks": sorted_r, "news": news, "today_trades": today_trades,
        }
        with open(config.CACHE_FILE, "w") as f:
            json.dump(payload, f, default=str)
    except Exception as e:
        log.debug(f"Partial save error: {e}")


def save_cache(p):
    with open(config.CACHE_FILE, "w") as f:
        json.dump(p, f, indent=2, default=str)
    log.info(f"  Saved → {config.CACHE_FILE}")

def load_cache():
    if not os.path.exists(config.CACHE_FILE): return {}
    with open(config.CACHE_FILE) as f: return json.load(f)
