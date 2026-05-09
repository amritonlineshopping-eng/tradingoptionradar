# =============================================================================
# config.py — All Your Settings In One Place
# =============================================================================

import os

# ------------------------------------------------------------------------------
# YOUR FYERS CREDENTIALS
# ------------------------------------------------------------------------------
CLIENT_ID    = "V6EGKZMUJ2-100"
SECRET_KEY   = "O0US1KYW9C"
REDIRECT_URI = "http://127.0.0.1:5000"

# ------------------------------------------------------------------------------
# FILE PATHS
# ------------------------------------------------------------------------------
BASE_DIR          = os.path.dirname(os.path.abspath(__file__))
ACCESS_TOKEN_FILE = os.path.join(BASE_DIR, "access_token.txt")
CACHE_FILE        = os.path.join(BASE_DIR, "radar_cache.json")

# ------------------------------------------------------------------------------
# F&O STOCK UNIVERSE — Top 50 most liquid NSE F&O stocks
#
# WHY THESE 50 AND NOT ALL 200+?
#
# NSE has 200+ F&O eligible stocks but on any given day, 90% of all
# options trading volume is concentrated in these 50 stocks. The other
# 150+ have:
#   - Wide bid-ask spreads (you lose money just entering the trade)
#   - Very low OI on weekly strikes (hard to exit quickly)
#   - Poor intraday movement (options barely move even if stock moves)
#
# Scanning all 200 would also take 3-4 minutes per scan (too slow for
# intraday). These 50 take ~25 seconds and cover all meaningful setups.
#
# You can add or remove any stock. Format: "NSE:SYMBOLNAME-EQ"
# ------------------------------------------------------------------------------
FNO_UNIVERSE = [
    # Banking & Finance (highest options volume on NSE)
    "NSE:HDFCBANK-EQ",    "NSE:ICICIBANK-EQ",   "NSE:SBIN-EQ",
    "NSE:AXISBANK-EQ",    "NSE:KOTAKBANK-EQ",   "NSE:BAJFINANCE-EQ",
    "NSE:BAJAJFINSV-EQ",  "NSE:INDUSINDBK-EQ",  "NSE:BANDHANBNK-EQ",
    "NSE:PNB-EQ",         "NSE:FEDERALBNK-EQ",  "NSE:CANBK-EQ",

    # IT & Technology
    "NSE:INFY-EQ",        "NSE:TCS-EQ",         "NSE:WIPRO-EQ",
    "NSE:LTIM-EQ",        "NSE:TECHM-EQ",       "NSE:HCLTECH-EQ",
    "NSE:MPHASIS-EQ",     "NSE:PERSISTENT-EQ",

    # Auto
    "NSE:TATAMOTORS-EQ",  "NSE:MARUTI-EQ",      "NSE:M&M-EQ",
    "NSE:HEROMOTOCO-EQ",  "NSE:BAJAJ-AUTO-EQ",  "NSE:EICHERMOT-EQ",

    # Energy & Oil
    "NSE:RELIANCE-EQ",    "NSE:ONGC-EQ",        "NSE:BPCL-EQ",
    "NSE:IOC-EQ",         "NSE:HINDPETRO-EQ",   "NSE:NTPC-EQ",
    "NSE:POWERGRID-EQ",   "NSE:ADANIPORTS-EQ",

    # FMCG & Consumer
    "NSE:HINDUNILVR-EQ",  "NSE:ITC-EQ",         "NSE:NESTLEIND-EQ",
    "NSE:TITAN-EQ",       "NSE:TATACONSUM-EQ",  "NSE:DABUR-EQ",

    # Pharma
    "NSE:SUNPHARMA-EQ",   "NSE:DRREDDY-EQ",     "NSE:CIPLA-EQ",
    "NSE:DIVISLAB-EQ",    "NSE:AUROPHARMA-EQ",

    # Metals & Infra
    "NSE:TATASTEEL-EQ",   "NSE:JSWSTEEL-EQ",    "NSE:HINDALCO-EQ",
    "NSE:BHARTIARTL-EQ",  "NSE:ADANIENT-EQ",
]

# ------------------------------------------------------------------------------
# SECTOR MAPPING
# ------------------------------------------------------------------------------
SECTOR_MAP = {
    "HDFCBANK":   "Banking",    "ICICIBANK":  "Banking",
    "SBIN":       "Banking",    "AXISBANK":   "Banking",
    "KOTAKBANK":  "Banking",    "INDUSINDBK": "Banking",
    "BANDHANBNK": "Banking",    "PNB":        "Banking",
    "FEDERALBNK": "Banking",    "CANBK":      "Banking",
    "BAJFINANCE": "NBFC",       "BAJAJFINSV": "NBFC",
    "INFY":       "IT",         "TCS":        "IT",
    "WIPRO":      "IT",         "LTIM":       "IT",
    "TECHM":      "IT",         "HCLTECH":    "IT",
    "MPHASIS":    "IT",         "PERSISTENT": "IT",
    "TATAMOTORS": "Auto",       "MARUTI":     "Auto",
    "M&M":        "Auto",       "HEROMOTOCO": "Auto",
    "BAJAJ-AUTO": "Auto",       "EICHERMOT":  "Auto",
    "RELIANCE":   "Energy",     "ONGC":       "Oil & Gas",
    "BPCL":       "Oil & Gas",  "IOC":        "Oil & Gas",
    "HINDPETRO":  "Oil & Gas",  "NTPC":       "Power",
    "POWERGRID":  "Power",      "ADANIPORTS": "Infra",
    "HINDUNILVR": "FMCG",       "ITC":        "FMCG",
    "NESTLEIND":  "FMCG",       "TITAN":      "Consumer",
    "TATACONSUM": "FMCG",       "DABUR":      "FMCG",
    "SUNPHARMA":  "Pharma",     "DRREDDY":    "Pharma",
    "CIPLA":      "Pharma",     "DIVISLAB":   "Pharma",
    "AUROPHARMA": "Pharma",     "TATASTEEL":  "Metals",
    "JSWSTEEL":   "Metals",     "HINDALCO":   "Metals",
    "BHARTIARTL": "Telecom",    "ADANIENT":   "Conglomerate",
}

# ------------------------------------------------------------------------------
# INDEX SYMBOLS
# ------------------------------------------------------------------------------
NIFTY_SYMBOL  = "NSE:NIFTY50-INDEX"
SENSEX_SYMBOL = "BSE:SENSEX-INDEX"
VIX_SYMBOL    = "NSE:INDIAVIX-INDEX"

# ------------------------------------------------------------------------------
# STRIKE STEP SIZES per stock
# ------------------------------------------------------------------------------
NIFTY_STRIKE_STEP  = 50
SENSEX_STRIKE_STEP = 100
STOCK_STRIKE_STEP  = {
    "RELIANCE":   20,   "HDFCBANK":   20,   "ICICIBANK":  20,
    "SBIN":       5,    "AXISBANK":   20,   "KOTAKBANK":  20,
    "BAJFINANCE": 100,  "BAJAJFINSV": 50,   "INDUSINDBK": 20,
    "INFY":       20,   "TCS":        50,   "WIPRO":      5,
    "LTIM":       50,   "TECHM":      20,   "HCLTECH":    20,
    "TATAMOTORS": 5,    "MARUTI":     100,  "M&M":        20,
    "HEROMOTOCO": 50,   "EICHERMOT":  50,   "BAJAJ-AUTO": 50,
    "ONGC":       5,    "BPCL":       5,    "NTPC":       5,
    "HINDUNILVR": 20,   "ITC":        5,    "TITAN":      50,
    "SUNPHARMA":  20,   "DRREDDY":    50,   "CIPLA":      10,
    "TATASTEEL":  5,    "JSWSTEEL":   10,   "HINDALCO":   10,
    "BHARTIARTL": 20,   "ADANIENT":   50,   "ADANIPORTS": 20,
    "NESTLEIND":  50,   "POWERGRID":  5,    "IOC":        5,
    "HINDPETRO":  5,    "TATACONSUM": 10,   "DABUR":      5,
    "DIVISLAB":   50,   "AUROPHARMA": 10,   "JSWSTEEL":   10,
    "FEDERALBNK": 5,    "CANBK":      5,    "PNB":        5,
    "BANDHANBNK": 5,    "MPHASIS":    50,   "PERSISTENT": 50,
    "DEFAULT":    10,
}

# ------------------------------------------------------------------------------
# SCORING THRESHOLDS
# ------------------------------------------------------------------------------
MIN_VOLUME_SURGE    = 1.5
MIN_ATR_PCT         = 1.0
IV_RANK_MIN         = 30
IV_RANK_MAX         = 75
MIN_SCORE_FOR_RADAR = 5.0
OI_CHANGE_THRESHOLD = 2.0
PRICE_CHG_THRESHOLD = 0.3

# ------------------------------------------------------------------------------
# TIMING
# ------------------------------------------------------------------------------
SCAN_INTERVAL_SECONDS = 300
ACTIVE_TRADE_MONITOR_SECONDS = 30
INDEX_CHAIN_REFRESH_SECONDS = 900
NEWS_REFRESH_SECONDS = 1800
MARKET_OPEN_HOUR,  MARKET_OPEN_MIN  = 9,  15
MARKET_CLOSE_HOUR, MARKET_CLOSE_MIN = 15, 30

# ------------------------------------------------------------------------------
# NEWS FEEDS
# ------------------------------------------------------------------------------
NEWS_FEEDS = [
    {"source": "ET Markets",    "url": "https://economictimes.indiatimes.com/markets/rss.cms",       "region": "india"},
    {"source": "Moneycontrol",  "url": "https://www.moneycontrol.com/rss/marketreports.xml",         "region": "india"},
    {"source": "LiveMint",      "url": "https://www.livemint.com/rss/markets",                       "region": "india"},
    {"source": "CNBCTV18",      "url": "https://www.cnbctv18.com/commonfeeds/v1/eng/rss/market.xml", "region": "india"},
    {"source": "Reuters India", "url": "https://feeds.reuters.com/reuters/INbusinessNews",           "region": "global"},
    {"source": "Bloomberg",     "url": "https://feeds.bloomberg.com/markets/news.rss",               "region": "global"},
]
MAX_NEWS_ITEMS = 12

# ------------------------------------------------------------------------------
# SERVER
# ------------------------------------------------------------------------------
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8000
