# =============================================================================
# config.example.py — Copy this to config.py and fill in your credentials
# =============================================================================
# cp config.example.py config.py

import os

# ------------------------------------------------------------------------------
# YOUR FYERS CREDENTIALS  (from https://myapi.fyers.in)
# ------------------------------------------------------------------------------
CLIENT_ID    = "YOUR_CLIENT_ID"
SECRET_KEY   = "YOUR_SECRET_KEY"
REDIRECT_URI = "http://127.0.0.1:5000"

# ------------------------------------------------------------------------------
# TRADINGVIEW CREDENTIALS  (your TradingView login)
# ------------------------------------------------------------------------------
TV_USERNAME = "your@email.com"
TV_PASSWORD = "yourpassword"

# ------------------------------------------------------------------------------
# FILE PATHS
# ------------------------------------------------------------------------------
BASE_DIR          = os.path.dirname(os.path.abspath(__file__))
ACCESS_TOKEN_FILE = os.path.join(BASE_DIR, "access_token.txt")
CACHE_FILE        = os.path.join(BASE_DIR, "radar_cache.json")

# ------------------------------------------------------------------------------
# F&O STOCK UNIVERSE — Top 50 most liquid NSE F&O stocks
# ------------------------------------------------------------------------------
FNO_UNIVERSE = [
    "NSE:HDFCBANK-EQ",    "NSE:ICICIBANK-EQ",   "NSE:SBIN-EQ",
    "NSE:AXISBANK-EQ",    "NSE:KOTAKBANK-EQ",   "NSE:BAJFINANCE-EQ",
    "NSE:BAJAJFINSV-EQ",  "NSE:INDUSINDBK-EQ",  "NSE:BANDHANBNK-EQ",
    "NSE:PNB-EQ",         "NSE:FEDERALBNK-EQ",  "NSE:CANBK-EQ",
    "NSE:INFY-EQ",        "NSE:TCS-EQ",         "NSE:WIPRO-EQ",
    "NSE:LTIM-EQ",        "NSE:TECHM-EQ",       "NSE:HCLTECH-EQ",
    "NSE:MPHASIS-EQ",     "NSE:PERSISTENT-EQ",
    "NSE:TATAMOTORS-EQ",  "NSE:MARUTI-EQ",      "NSE:M&M-EQ",
    "NSE:HEROMOTOCO-EQ",  "NSE:BAJAJ-AUTO-EQ",  "NSE:EICHERMOT-EQ",
    "NSE:RELIANCE-EQ",    "NSE:ONGC-EQ",        "NSE:BPCL-EQ",
    "NSE:IOC-EQ",         "NSE:HINDPETRO-EQ",   "NSE:NTPC-EQ",
    "NSE:POWERGRID-EQ",   "NSE:ADANIPORTS-EQ",
    "NSE:HINDUNILVR-EQ",  "NSE:ITC-EQ",         "NSE:NESTLEIND-EQ",
    "NSE:TITAN-EQ",       "NSE:TATACONSUM-EQ",  "NSE:DABUR-EQ",
    "NSE:SUNPHARMA-EQ",   "NSE:DRREDDY-EQ",     "NSE:CIPLA-EQ",
    "NSE:DIVISLAB-EQ",    "NSE:AUROPHARMA-EQ",
    "NSE:TATASTEEL-EQ",   "NSE:JSWSTEEL-EQ",    "NSE:HINDALCO-EQ",
    "NSE:BHARTIARTL-EQ",  "NSE:ADANIENT-EQ",
]

# Rest of config values — see config.example.py for full list
# (sector map, strike steps, thresholds, timing, news feeds, server settings)
