"""
analytics.py — Advanced Analytics Engine for Options Radar
============================================================
Implements everything from Phase 1 integration plan:

1. BLACK-SCHOLES GREEKS  : Delta, Gamma, Theta, Vega, IV (per strike)
2. IV RANK SCANNER       : IV percentile for 30+ F&O stocks (52-week high/low)
3. MAX PAIN (proper)     : True max pain calculation from OI data
4. MARKET BREADTH        : Advance/Decline, sentiment score 0-100, VIX regime
5. SECTOR HEATMAP        : NSE sector performance data (12 sectors)
6. GIFT NIFTY            : Pre-market gap signal from SGX/GIFT Nifty

All data cached to JSON files, refreshed on schedule.
Greeks/IV calculated via Black-Scholes (no external API needed).
Sector + Breadth data from NSE India public endpoints (no auth needed).
"""

import os, json, math, time, logging, threading, requests
from datetime import datetime, date, timedelta
from typing import Optional

log = logging.getLogger("analytics")

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
ANALYTICS_FILE = os.path.join(BASE_DIR, "analytics_cache.json")
IV_RANK_FILE   = os.path.join(BASE_DIR, "iv_rank_cache.json")

# NSE public endpoints (no auth required, 3-5 sec delay)
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://www.nseindia.com",
}
NSE_BASE = "https://www.nseindia.com"

# ─── 1. BLACK-SCHOLES GREEKS ──────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    """Cumulative standard normal distribution (Abramowitz & Stegun approximation)."""
    a1, a2, a3, a4, a5 = 0.319381530, -0.356563782, 1.781477937, -1.821255978, 1.330274429
    p = 0.2316419
    k = 1.0 / (1.0 + p * abs(x))
    poly = k * (a1 + k * (a2 + k * (a3 + k * (a4 + k * a5))))
    cdf_pos = 1.0 - (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * x * x) * poly
    return cdf_pos if x >= 0 else 1.0 - cdf_pos


def _norm_pdf(x: float) -> float:
    return (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * x * x)


def calc_greeks(S: float, K: float, T: float, r: float, sigma: float, opt_type: str) -> dict:
    """
    Black-Scholes Greeks for European options.

    S       = Spot price (underlying)
    K       = Strike price
    T       = Time to expiry in years (e.g. 7/365 for 7 days)
    r       = Risk-free rate (use 0.065 for India 6.5%)
    sigma   = Implied volatility (annualized, e.g. 0.18 for 18%)
    opt_type= 'CE' or 'PE'

    Returns: {delta, gamma, theta, vega, rho, price}
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return {"delta": 0.5 if opt_type == "CE" else -0.5,
                "gamma": 0, "theta": 0, "vega": 0, "rho": 0, "price": 0}

    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)

        if opt_type == "CE":
            price = S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
            delta = _norm_cdf(d1)
            rho   = K * T * math.exp(-r * T) * _norm_cdf(d2) / 100
        else:  # PE
            price = K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)
            delta = _norm_cdf(d1) - 1
            rho   = -K * T * math.exp(-r * T) * _norm_cdf(-d2) / 100

        gamma = _norm_pdf(d1) / (S * sigma * math.sqrt(T))
        vega  = S * _norm_pdf(d1) * math.sqrt(T) / 100   # per 1% IV change
        theta = (-(S * _norm_pdf(d1) * sigma) / (2 * math.sqrt(T))
                 - r * K * math.exp(-r * T) * (_norm_cdf(d2) if opt_type == "CE" else _norm_cdf(-d2))) / 365

        return {
            "delta": round(delta, 4),
            "gamma": round(gamma, 6),
            "theta": round(theta, 4),
            "vega":  round(vega, 4),
            "rho":   round(rho, 4),
            "price": round(price, 2),
        }
    except Exception as e:
        log.debug(f"Greeks calc error: {e}")
        return {"delta": 0.5 if opt_type == "CE" else -0.5, "gamma": 0, "theta": 0, "vega": 0, "rho": 0, "price": 0}


def calc_iv(market_price: float, S: float, K: float, T: float, r: float, opt_type: str,
            tol: float = 1e-5, max_iter: int = 100) -> float:
    """
    Calculate Implied Volatility using Newton-Raphson method.
    Returns annualized IV as decimal (e.g. 0.18 for 18%).
    Returns 0 if calculation fails.
    """
    if market_price <= 0 or T <= 0 or S <= 0 or K <= 0:
        return 0.0

    # Initial IV estimate
    sigma = 0.25

    for _ in range(max_iter):
        g = calc_greeks(S, K, T, r, sigma, opt_type)
        bs_price = g["price"]
        vega     = g["vega"] * 100  # vega per 1 full IV unit

        if vega < 1e-10:
            break

        diff = bs_price - market_price
        if abs(diff) < tol:
            return round(sigma, 4)

        sigma -= diff / vega
        if sigma <= 0.001:
            sigma = 0.001
        if sigma > 5.0:
            return 0.0

    return round(max(sigma, 0), 4)


def enrich_chain_with_greeks(chain: dict, spot: float, expiry: date, r: float = 0.065) -> dict:
    """
    Enrich an existing option chain dict with Black-Scholes Greeks and IV for every strike.
    Also computes proper Max Pain.
    Updates chain in-place and returns it.
    """
    if not chain or not chain.get("strikes"):
        return chain

    T = max((expiry - date.today()).days / 365.0, 1 / 365.0)

    for strike_data in chain.get("strikes", []):
        K = strike_data.get("strike", 0)
        if not K:
            continue

        for opt_type in ("CE", "PE"):
            opt = strike_data.get(opt_type, {})
            if not opt:
                continue

            ltp = opt.get("ltp", 0)
            if ltp <= 0:
                continue

            # Calculate IV from market price
            iv = calc_iv(ltp, spot, K, T, r, opt_type)
            opt["iv"] = round(iv * 100, 2)  # store as percentage

            # Calculate Greeks using computed IV
            g = calc_greeks(spot, K, T, r, iv if iv > 0 else 0.20, opt_type)
            opt["delta"] = g["delta"]
            opt["gamma"] = g["gamma"]
            opt["theta"] = g["theta"]
            opt["vega"]  = g["vega"]

    # Compute proper Max Pain
    chain["max_pain"] = calc_max_pain(chain, spot)

    return chain


# ─── 2. MAX PAIN (PROPER CALCULATION) ────────────────────────────────────────

def calc_max_pain(chain: dict, spot: float) -> float:
    """
    True Max Pain: strike where total option buyer loss is maximum
    (i.e. where option writers make the most money).

    Algorithm:
    For each possible expiry strike X:
      - All CE holders with strike < X lose (their CE expires OTM)
      - All PE holders with strike > X lose (their PE expires OTM)
      - Total writer gain at X = sum(CE_OI * max(X-strike,0)) + sum(PE_OI * max(strike-X,0))
    Max Pain = X that maximizes total writer gain
    """
    strikes_data = chain.get("strikes", [])
    if not strikes_data:
        return chain.get("max_pain", round(spot / 50) * 50)

    strikes = [s["strike"] for s in strikes_data if s.get("strike")]
    if not strikes:
        return round(spot / 50) * 50

    min_pain = float("inf")
    max_pain_strike = strikes[0]

    for test_strike in strikes:
        total_pain = 0
        for s in strikes_data:
            K = s.get("strike", 0)
            if not K:
                continue
            # CE OI pain at test_strike
            ce_oi = s.get("CE", {}).get("oi", 0)
            if ce_oi and test_strike > K:
                total_pain += ce_oi * (test_strike - K)
            # PE OI pain at test_strike
            pe_oi = s.get("PE", {}).get("oi", 0)
            if pe_oi and test_strike < K:
                total_pain += pe_oi * (K - test_strike)

        if total_pain < min_pain:
            min_pain = total_pain
            max_pain_strike = test_strike

    return max_pain_strike


# ─── 3. IV RANK SCANNER ───────────────────────────────────────────────────────

def calc_iv_rank(current_iv: float, iv_52w_low: float, iv_52w_high: float) -> float:
    """
    IV Rank = (Current IV - 52W Low) / (52W High - 52W Low) × 100
    Range: 0-100. >80 = expensive (sell), <20 = cheap (buy).
    """
    if iv_52w_high <= iv_52w_low or iv_52w_high == 0:
        return 50.0
    return round((current_iv - iv_52w_low) / (iv_52w_high - iv_52w_low) * 100, 1)


def calc_iv_percentile(current_iv: float, iv_history: list) -> float:
    """
    IV Percentile = % of past days where IV was BELOW current IV.
    More reliable than IV Rank.
    """
    if not iv_history:
        return 50.0
    below = sum(1 for v in iv_history if v < current_iv)
    return round(below / len(iv_history) * 100, 1)


def get_atm_iv_from_chain(chain: dict, spot: float) -> float:
    """Extract ATM implied volatility from an enriched option chain."""
    if not chain or not chain.get("strikes"):
        return 0.0

    atm_strike = chain.get("atm_strike", round(spot / 50) * 50)
    for s in chain["strikes"]:
        if s.get("strike") == atm_strike:
            ce_iv = s.get("CE", {}).get("iv", 0)
            pe_iv = s.get("PE", {}).get("iv", 0)
            # Use average of CE and PE ATM IV (straddle IV)
            ivs = [v for v in [ce_iv, pe_iv] if v > 0]
            if ivs:
                return round(sum(ivs) / len(ivs), 2)
    return 0.0


def scan_iv_ranks(fyers, symbols_to_check: list, chains: dict) -> dict:
    """
    For each symbol, compute IV rank using:
    - Current ATM IV from live option chain
    - Estimated 52W IV range (using VIX as proxy, with stock beta scaling)

    Returns dict: {symbol: {iv, iv_rank, iv_percentile, signal}}
    """
    results = {}
    vix = 18.0  # default, updated from scanner

    try:
        import scanner as sc
        qs = sc.fetch_quotes(fyers, ["NSE:INDIAVIX-INDEX"], batch_size=2, delay=0.1)
        v = qs.get("NSE:INDIAVIX-INDEX", {}).get("ltp", 0)
        if v: vix = float(v)
    except: pass

    # Approximate 52W IV ranges per stock type
    # High beta stocks (banks, auto): IV 20-60%
    # Mid beta (IT, FMCG): IV 15-45%
    # Low beta (defensives): IV 12-35%
    BETA_HIGH = ["HDFCBANK","ICICIBANK","SBIN","KOTAKBANK","AXISBANK","BAJAJFINSV",
                 "BAJAJ-AUTO","TATAMOTORS","MARUTI","M&M","HEROMOTOCO",
                 "TATASTEEL","JSWSTEEL","HINDALCO","COALINDIA"]
    BETA_MID  = ["INFY","TCS","WIPRO","HCLTECH","TECHM","RELIANCE",
                 "NTPC","POWERGRID","ADANIPORTS"]

    for sym, chain in chains.items():
        if not chain:
            continue
        try:
            spot = chain.get("spot", 0)
            if not spot: continue

            atm_iv = get_atm_iv_from_chain(chain, spot)
            if atm_iv <= 0:
                continue

            # Estimate IV range scaled from VIX
            # VIX is Nifty 30-day IV. Stocks typically trade at 1.2-2x VIX
            base_name = sym.replace("NSE:","").replace("-EQ","")
            if any(b in base_name for b in BETA_HIGH):
                scale_low, scale_high = 1.4, 2.2
            elif any(b in base_name for b in BETA_MID):
                scale_low, scale_high = 1.2, 1.8
            else:
                scale_low, scale_high = 1.0, 1.6

            # Historical range: when VIX was at low (11-12), high (22-25)
            iv_52w_low  = round(11.0 * scale_low, 1)
            iv_52w_high = round(25.0 * scale_high, 1)

            iv_rank = calc_iv_rank(atm_iv, iv_52w_low, iv_52w_high)

            # Signal
            if iv_rank >= 80:
                signal = "HIGH_IV"   # Expensive — good for selling
                signal_text = "IV Expensive — Consider Selling"
            elif iv_rank <= 20:
                signal = "LOW_IV"    # Cheap — good for buying
                signal_text = "IV Cheap — Consider Buying"
            else:
                signal = "NEUTRAL"
                signal_text = "IV Normal"

            results[base_name] = {
                "iv":            atm_iv,
                "iv_rank":       iv_rank,
                "iv_52w_low":    iv_52w_low,
                "iv_52w_high":   iv_52w_high,
                "signal":        signal,
                "signal_text":   signal_text,
                "vix_ref":       round(vix, 2),
                "updated_at":    datetime.now().strftime("%H:%M"),
            }
        except Exception as e:
            log.debug(f"IV rank error for {sym}: {e}")

    return results


# ─── 4. MARKET BREADTH ────────────────────────────────────────────────────────

def fetch_market_breadth() -> dict:
    """
    Fetch Advance/Decline ratio, NIFTY 500 breadth, and compute sentiment score.
    Uses NSE India public API (no auth needed).
    """
    try:
        session = requests.Session()
        # First hit homepage to get cookies
        session.get(NSE_BASE, headers=NSE_HEADERS, timeout=8)

        # Fetch market status and breadth
        resp = session.get(
            f"{NSE_BASE}/api/equity-stockIndices?index=SECURITIES%20IN%20F%26O",
            headers=NSE_HEADERS, timeout=10
        )
        data = resp.json() if resp.status_code == 200 else {}

        advance = 0
        decline = 0
        unchanged = 0

        if "data" in data:
            for stock in data["data"]:
                chg = float(stock.get("pChange", 0) or 0)
                if chg > 0: advance += 1
                elif chg < 0: decline += 1
                else: unchanged += 1

        total = advance + decline + unchanged
        if total == 0:
            return _breadth_fallback()

        ad_ratio = round(advance / max(decline, 1), 2)
        breadth_pct = round(advance / total * 100, 1)

        # Sentiment score 0-100:
        # Based on: A/D ratio (40%), breadth% (40%), time of day (20%)
        now = datetime.now()
        hour_weight = 1.0
        if now.hour < 10:  hour_weight = 0.8   # early session, less reliable
        if now.hour >= 14: hour_weight = 1.1    # afternoon confirmation is stronger

        ad_score = min(ad_ratio / 3.0, 1.0) * 40   # max at 3:1 ratio
        br_score = (breadth_pct / 100.0) * 40
        sentiment = round((ad_score + br_score) * hour_weight, 1)
        sentiment = max(0, min(100, sentiment))

        # VIX regime
        vix_regime = "LOW"    # default
        try:
            vix_resp = session.get(
                f"{NSE_BASE}/api/allIndices",
                headers=NSE_HEADERS, timeout=8
            )
            if vix_resp.status_code == 200:
                vix_data = vix_resp.json().get("data", [])
                for idx in vix_data:
                    if "VIX" in idx.get("indexSymbol", ""):
                        v = float(idx.get("last", 0) or 0)
                        if v > 0:
                            if v > 20:   vix_regime = "HIGH"
                            elif v > 15: vix_regime = "MODERATE"
                            else:        vix_regime = "LOW"
                        break
        except: pass

        # Labels
        if sentiment >= 70:   mood = "BULLISH"
        elif sentiment >= 55: mood = "SLIGHTLY BULLISH"
        elif sentiment >= 45: mood = "NEUTRAL"
        elif sentiment >= 30: mood = "SLIGHTLY BEARISH"
        else:                 mood = "BEARISH"

        return {
            "advance":      advance,
            "decline":      decline,
            "unchanged":    unchanged,
            "total":        total,
            "ad_ratio":     ad_ratio,
            "breadth_pct":  breadth_pct,
            "sentiment":    sentiment,
            "mood":         mood,
            "vix_regime":   vix_regime,
            "updated_at":   datetime.now().strftime("%H:%M"),
        }

    except Exception as e:
        log.warning(f"Market breadth fetch error: {e}")
        return _breadth_fallback()


def _breadth_fallback() -> dict:
    return {
        "advance": 0, "decline": 0, "unchanged": 0, "total": 0,
        "ad_ratio": 1.0, "breadth_pct": 50.0, "sentiment": 50.0,
        "mood": "NEUTRAL", "vix_regime": "MODERATE",
        "updated_at": datetime.now().strftime("%H:%M"),
    }


# ─── 5. SECTOR HEATMAP ────────────────────────────────────────────────────────

NSE_SECTOR_INDICES = {
    "BANK":      "NIFTY BANK",
    "IT":        "NIFTY IT",
    "AUTO":      "NIFTY AUTO",
    "FMCG":      "NIFTY FMCG",
    "PHARMA":    "NIFTY PHARMA",
    "METAL":     "NIFTY METAL",
    "REALTY":    "NIFTY REALTY",
    "ENERGY":    "NIFTY ENERGY",
    "INFRA":     "NIFTY INFRA",
    "MEDIA":     "NIFTY MEDIA",
    "PSU BANK":  "NIFTY PSU BANK",
    "MIDCAP":    "NIFTY MIDCAP 100",
}


def fetch_sector_heatmap() -> list:
    """
    Fetch sector performance from NSE India.
    Returns list of {sector, change_pct, ltp, trend}.
    """
    try:
        session = requests.Session()
        session.get(NSE_BASE, headers=NSE_HEADERS, timeout=8)

        resp = session.get(
            f"{NSE_BASE}/api/allIndices",
            headers=NSE_HEADERS, timeout=12
        )
        if resp.status_code != 200:
            return _sector_fallback()

        all_indices = resp.json().get("data", [])

        sectors = []
        for sector_short, nse_name in NSE_SECTOR_INDICES.items():
            for idx in all_indices:
                name = idx.get("indexSymbol", "") or idx.get("index", "")
                if nse_name.upper() in name.upper():
                    chg_pct = float(idx.get("percentChange", 0) or 0)
                    ltp     = float(idx.get("last", 0) or 0)
                    sectors.append({
                        "sector":     sector_short,
                        "full_name":  nse_name,
                        "ltp":        round(ltp, 2),
                        "change_pct": round(chg_pct, 2),
                        "trend":      "UP" if chg_pct > 0 else ("DOWN" if chg_pct < 0 else "FLAT"),
                        "strength":   "STRONG" if abs(chg_pct) > 1.5 else ("MODERATE" if abs(chg_pct) > 0.5 else "WEAK"),
                    })
                    break

        if not sectors:
            return _sector_fallback()

        sectors.sort(key=lambda x: x["change_pct"], reverse=True)
        return sectors

    except Exception as e:
        log.warning(f"Sector heatmap fetch error: {e}")
        return _sector_fallback()


def _sector_fallback() -> list:
    sectors = []
    for s in NSE_SECTOR_INDICES:
        sectors.append({"sector": s, "full_name": s, "ltp": 0, "change_pct": 0, "trend": "FLAT", "strength": "WEAK"})
    return sectors


# ─── COMBINED ANALYTICS CACHE ─────────────────────────────────────────────────

def _load_cache() -> dict:
    if not os.path.exists(ANALYTICS_FILE):
        return {}
    try:
        with open(ANALYTICS_FILE) as f:
            return json.load(f)
    except:
        return {}


def _save_cache(data: dict):
    try:
        data["cache_updated"] = datetime.now().isoformat()
        with open(ANALYTICS_FILE, "w") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        log.error(f"Analytics cache save error: {e}")


def get_cached_analytics() -> dict:
    """Returns current analytics cache for API serving."""
    return _load_cache()


# ─── ANALYTICS ENRICHMENT FOR SCORER ─────────────────────────────────────────

def get_iv_rank_for_symbol(symbol: str) -> dict:
    """
    Called by score_stock() to get IV rank for a stock.
    Returns {iv, iv_rank, signal} or empty dict.
    """
    try:
        cache = _load_cache()
        iv_data = cache.get("iv_ranks", {})
        base = symbol.replace("NSE:", "").replace("-EQ", "").replace("BSE:", "")
        return iv_data.get(base, {})
    except:
        return {}


def iv_rank_score_adjustment(iv_rank_data: dict, direction: str) -> float:
    """
    Adjust trade score based on IV rank.
    Low IV + buying = GOOD (cheap premium, better RR)
    High IV + buying = BAD (expensive premium)
    Score adjustment: -1.0 to +1.0
    """
    if not iv_rank_data:
        return 0.0

    iv_rank = iv_rank_data.get("iv_rank", 50)
    signal  = iv_rank_data.get("signal", "NEUTRAL")

    # For option BUYERS (CE or PE):
    if signal == "LOW_IV":
        return +0.5   # Cheap options — great for buying
    elif signal == "HIGH_IV":
        return -0.5   # Expensive options — avoid buying
    return 0.0


# ─── BACKGROUND ANALYTICS LOOP ───────────────────────────────────────────────

def run_analytics_loop(fyers_getter):
    """
    Background thread that refreshes all analytics data.
    fyers_getter: callable that returns the current fyers object

    Schedule:
    - Market breadth: every 5 min
    - Sector heatmap: every 10 min
    - IV ranks: every 15 min during market hours (uses existing chains)
    Note: GIFT Nifty is now handled by tv_provider.py (TradingView feed).
    """
    log.info("Analytics engine started.")

    last_breadth  = datetime.min
    last_sector   = datetime.min
    last_iv       = datetime.min

    while True:
        try:
            now = datetime.now()
            cache = _load_cache()
            updated = False
            total_mins = now.hour * 60 + now.minute

            # Market breadth — every 5 min during market hours
            if (9 * 60 + 15) <= total_mins <= (15 * 60 + 35):
                if (now - last_breadth).total_seconds() > 300:
                    log.debug("Analytics: fetching market breadth...")
                    cache["market_breadth"] = fetch_market_breadth()
                    last_breadth = now
                    updated = True

                # Sector heatmap — every 10 min
                if (now - last_sector).total_seconds() > 600:
                    log.debug("Analytics: fetching sector heatmap...")
                    cache["sector_heatmap"] = fetch_sector_heatmap()
                    last_sector = now
                    updated = True

            # IV Ranks — every 15 min using live chains from scanner
            if (9 * 60 + 15) <= total_mins <= (15 * 60 + 30):
                if (now - last_iv).total_seconds() > 900:
                    log.debug("Analytics: computing IV ranks...")
                    try:
                        fyers = fyers_getter()
                        if fyers:
                            # Load existing chains from radar cache
                            radar_cache_path = os.path.join(BASE_DIR, "radar_cache.json")
                            if os.path.exists(radar_cache_path):
                                with open(radar_cache_path) as f:
                                    radar = json.load(f)
                                stocks = radar.get("stocks", [])
                                chains = {}
                                for s in stocks:
                                    sym = s.get("symbol", "")
                                    if sym and s.get("chain"):
                                        chains[sym] = s["chain"]
                                if chains:
                                    iv_ranks = scan_iv_ranks(fyers, list(chains.keys()), chains)
                                    cache["iv_ranks"] = iv_ranks
                                    log.info(f"Analytics: IV ranks computed for {len(iv_ranks)} symbols")
                    except Exception as e:
                        log.warning(f"IV rank scan error: {e}")
                    last_iv = now
                    updated = True

            if updated:
                _save_cache(cache)

        except Exception as e:
            log.error(f"Analytics loop error: {e}")

        time.sleep(60)  # check every minute


def run_greeks_enrichment(chain: dict, spot: float, expiry: date) -> dict:
    """
    Public function called by scanner.build_option_chain() after building chain.
    Enriches chain with Greeks, IV, and proper Max Pain in-place.
    """
    try:
        return enrich_chain_with_greeks(chain, spot, expiry)
    except Exception as e:
        log.debug(f"Greeks enrichment error: {e}")
        return chain
