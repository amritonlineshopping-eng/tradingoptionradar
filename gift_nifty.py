# gift_nifty.py — Scrapes GIFT Nifty from Groww every 60 seconds
import json, os, time, logging, requests
from datetime import datetime

log = logging.getLogger("gift_nifty")
GIFT_FILE = os.path.join(os.path.dirname(__file__), "gift_nifty.json")

GROWW_URL  = "https://groww.in/indices/global-indices/sgx-nifty"
GROWW_API  = "https://groww.in/v1/api/stocks_data/v1/global_index/SGX_NIFTY"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://groww.in/",
    "Origin": "https://groww.in",
}


def fetch_gift_nifty():
    """
    Scrapes GIFT Nifty from Groww's API endpoint.
    Falls back to HTML scrape if API fails.
    """
    # Try Groww's internal API first
    try:
        resp = requests.get(GROWW_API, headers=HEADERS, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            ltp = _extract(data, ["ltp","lastPrice","price","close","last"])
            if ltp and ltp > 0:
                prev  = _extract(data, ["previousClose","prevClose","prev_close","open"]) or ltp
                chg   = round(ltp - prev, 2)
                chg_p = round((chg / prev * 100) if prev > 0 else 0, 2)
                payload = {"ltp": round(ltp,2), "chg_pts": chg, "chg_pct": chg_p,
                           "updated_at": datetime.now().isoformat(), "source": "groww_api"}
                _save(payload); return payload
    except Exception as e:
        log.debug(f"Groww API failed: {e}")

    # Fallback — try Moneycontrol
    for url in [
        "https://priceapi.moneycontrol.com/techCharts/apiV1/symbol?symbol=GIFT_NIFTY&type=index",
        "https://api.moneycontrol.com/mcapi/v1/indices/getIndexData?indexId=GIFT_NIFTY",
    ]:
        try:
            resp = requests.get(url, headers={"User-Agent": HEADERS["User-Agent"], "Referer": "https://www.moneycontrol.com/"}, timeout=6)
            if resp.status_code == 200:
                data = resp.json()
                ltp = _extract_nested(data)
                if ltp and ltp > 0:
                    payload = {"ltp": round(ltp,2), "chg_pts": 0, "chg_pct": 0,
                               "updated_at": datetime.now().isoformat(), "source": "moneycontrol"}
                    _save(payload); return payload
        except Exception as e:
            log.debug(f"MC fallback failed: {e}")

    return _load()


def _extract(data, keys):
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if v is not None:
                try: return float(str(v).replace(",",""))
                except: pass
        # search nested
        for v in data.values():
            if isinstance(v, dict):
                r = _extract(v, keys)
                if r: return r
    return 0


def _extract_nested(data):
    if isinstance(data, dict):
        if "data" in data:
            d = data["data"]
            return _extract(d if isinstance(d, dict) else {}, ["indexValue","lastPrice","ltp","close","last","c"])
        return _extract(data, ["indexValue","lastPrice","ltp","close","last","c"])
    if isinstance(data, list) and data:
        return _extract_nested(data[0])
    return 0


def _save(p):
    with open(GIFT_FILE, "w") as f: json.dump(p, f)

def _load():
    if not os.path.exists(GIFT_FILE):
        return {"ltp": 0, "chg_pts": 0, "chg_pct": 0, "updated_at": None, "source": "none"}
    try:
        with open(GIFT_FILE) as f: return json.load(f)
    except:
        return {"ltp": 0, "chg_pts": 0, "chg_pct": 0, "updated_at": None, "source": "none"}

def get_gift_nifty(): return _load()

def run_gift_nifty_loop():
    log.info("GIFT Nifty scraper started (60s, 24x7) — source: Groww")
    while True:
        try:
            d = fetch_gift_nifty()
            if d.get("ltp", 0) > 0:
                log.debug(f"GIFT Nifty: {d['ltp']} ({d.get('chg_pts',0):+.2f})")
        except Exception as e:
            log.debug(f"GIFT Nifty loop error: {e}")
        time.sleep(60)
