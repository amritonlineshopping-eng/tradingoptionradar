"""
news_fetcher.py — Real-time Market News System
===============================================
Fetches fresh, relevant news for Indian F&O markets.
Sources: RSS feeds, market APIs, financial news sites.
Filters for: F&O, Nifty, Sensex, high-impact stocks, macro events.
Updates every 10 minutes with proper timezone handling.
"""

import os, json, logging, time, threading
from datetime import datetime, timedelta
from urllib.request import urlopen
from urllib.error import URLError
import feedparser

log = logging.getLogger("news")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
NEWS_CACHE = os.path.join(BASE_DIR, "market_news.json")

# Keywords that indicate HIGH-IMPACT news for Indian F&O markets
KEYWORDS_F_AND_O = ["F&O", "futures", "options", "expiry", "rollover", "open interest", "OI", "PCR"]
KEYWORDS_INDEX = ["Nifty", "NIFTY", "Sensex", "SENSEX", "BSE", "NSE", "index", "broadbased"]
KEYWORDS_VOLATILITY = ["VIX", "volatility", "crash", "rally", "circuit", "limit down", "limit up"]
KEYWORDS_MACRO = ["RBI", "inflation", "interest rate", "Fed", "ECB", "GDP", "earnings", "Q4 results", "budget"]
KEYWORDS_STOCKS = ["ITC", "HDFC", "Reliance", "Infosys", "TCS", "Wipro", "ICICIBANK", "SBIN", "BAJAJFINSV", "Maruti"]

KEYWORDS_IGNORE = ["sports", "entertainment", "movie", "cricket", "celebrity", "wedding"]

# RSS Feeds for Indian market news
FEED_URLS = [
    "https://feeds.bloomberg.com/markets/news.rss",  # Bloomberg markets
    "https://feeds.finance.yahoo.com/rss/2.0/headline",  # Yahoo finance
    "https://feeds.reuters.com/reuters/businessNews",  # Reuters business
    "https://www.cnbctv18.com/feed/",  # CNBC India
    "https://feeds.ft.com/?format=rss",  # Financial Times
    "https://economictimes.indiatimes.com/feed/",  # Economic Times India
]

# Fallback static news if feeds fail (development/testing)
FALLBACK_NEWS = [
    {
        "title": "Nifty 50 opens gap-up ahead of RBI decision",
        "source": "Economic Times",
        "link": "https://economictimes.indiatimes.com",
        "published": "2026-05-06T09:30:00+05:30",
        "impact": "HIGH",
        "category": "INDEX",
    },
    {
        "title": "F&O expiry week: Options traders brace for volatility",
        "source": "Moneycontrol",
        "link": "https://moneycontrol.com",
        "published": "2026-05-06T08:15:00+05:30",
        "impact": "MEDIUM",
        "category": "F&O",
    },
]


def _parse_timestamp(pub_date_str, source_tz="UTC"):
    """
    Parse various timestamp formats and return (datetime, iso_string, display_string).
    Preserves original timezone context for display.
    """
    if not pub_date_str:
        return datetime.now(), datetime.now().isoformat(), "Just now"

    try:
        # Try RFC 2822 format (common in RSS)
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(pub_date_str)
        iso_str = dt.isoformat()
        tz_str = dt.strftime("%Z") or source_tz
        display = dt.strftime("%I:%M %p") + f" {tz_str}"
        return dt, iso_str, display
    except:
        pass

    try:
        # ISO format
        if "T" in pub_date_str:
            dt = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))
            iso_str = dt.isoformat()
            tz_str = dt.strftime("%Z") or source_tz
            display = dt.strftime("%I:%M %p") + f" {tz_str}"
            return dt, iso_str, display
    except:
        pass

    # Fallback: assume current time
    dt = datetime.now()
    return dt, dt.isoformat(), "Just now"


def _is_relevant(title, description=""):
    """Check if news is relevant to Indian F&O markets."""
    text = (title + " " + description).lower()

    # Ignore low-relevance categories
    for ignore in KEYWORDS_IGNORE:
        if ignore.lower() in text:
            return False

    # Check for relevance
    relevant_keywords = (
        KEYWORDS_F_AND_O + KEYWORDS_INDEX + KEYWORDS_VOLATILITY + KEYWORDS_MACRO + KEYWORDS_STOCKS
    )

    for keyword in relevant_keywords:
        if keyword.lower() in text:
            return True

    return False


def _categorize_news(title, description=""):
    """Categorize news by impact and type."""
    text = (title + " " + description).lower()

    category = "GENERAL"
    impact = "LOW"

    # Category detection
    if any(k.lower() in text for k in KEYWORDS_F_AND_O):
        category = "F&O"
    elif any(k.lower() in text for k in KEYWORDS_INDEX):
        category = "INDEX"
    elif any(k.lower() in text for k in KEYWORDS_MACRO):
        category = "MACRO"
    elif any(k.lower() in text for k in KEYWORDS_VOLATILITY):
        category = "VOLATILITY"
    else:
        for stock in KEYWORDS_STOCKS:
            if stock.lower() in text:
                category = "STOCK"
                break

    # Impact detection
    high_impact_words = ["crash", "rally", "surge", "plunge", "circuit", "RBI", "Fed", "earnings"]
    if any(w in text for w in high_impact_words):
        impact = "HIGH"
    elif category in ("INDEX", "F&O", "MACRO"):
        impact = "MEDIUM"
    else:
        impact = "LOW"

    return category, impact


def fetch_news_from_rss():
    """Fetch and parse RSS feeds, filter for relevance."""
    articles = []

    for feed_url in FEED_URLS:
        try:
            log.info(f"Fetching {feed_url[:50]}...")
            feed = feedparser.parse(feed_url)

            if not feed.entries:
                continue

            for entry in feed.entries[:15]:  # Limit to 15 per feed
                title = entry.get("title", "")
                desc = entry.get("summary", "")
                link = entry.get("link", "")
                pub = entry.get("published", "")

                # Check relevance
                if not _is_relevant(title, desc):
                    continue

                # Parse timestamp
                dt, iso_str, display_time = _parse_timestamp(pub)

                # Categorize
                category, impact = _categorize_news(title, desc)

                articles.append(
                    {
                        "title": title[:120],  # Limit length
                        "description": desc[:250],
                        "source": feed.feed.get("title", "News Feed"),
                        "link": link,
                        "published": iso_str,
                        "display_time": display_time,
                        "category": category,
                        "impact": impact,
                        "fetched_at": datetime.now().isoformat(),
                    }
                )
        except Exception as e:
            log.warning(f"Feed fetch error {feed_url[:40]}: {e}")
            continue

    # Sort by freshness
    articles.sort(key=lambda x: x["published"], reverse=True)

    # Deduplicate
    seen_titles = set()
    unique = []
    for a in articles:
        if a["title"] not in seen_titles:
            seen_titles.add(a["title"])
            unique.append(a)

    return unique[:30]  # Return top 30 unique articles


def get_cached_news():
    """Load cached news from file."""
    if not os.path.exists(NEWS_CACHE):
        return []
    try:
        with open(NEWS_CACHE) as f:
            data = json.load(f)
        return data.get("articles", [])
    except:
        return []


def _save_news_cache(articles):
    """Save news to cache file."""
    data = {
        "articles": articles,
        "updated_at": datetime.now().isoformat(),
        "count": len(articles),
    }
    try:
        with open(NEWS_CACHE, "w") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        log.error(f"Cache save error: {e}")


def fetch_news():
    """
    Main fetch function: tries real feeds, falls back to cache + fallback.
    Called every 10 minutes.
    """
    try:
        articles = fetch_news_from_rss()
        if articles:
            _save_news_cache(articles)
            log.info(f"Fetched {len(articles)} news articles")
            return articles
    except Exception as e:
        log.error(f"News fetch error: {e}")

    # Fallback to cache
    cached = get_cached_news()
    if cached:
        log.info(f"Using cached news ({len(cached)} articles)")
        return cached

    # Last resort: hardcoded fallback
    log.warning("Using fallback news (stale)")
    return FALLBACK_NEWS


def run_news_loop():
    """Background thread: refresh news every 10 minutes."""
    log.info("News fetcher started (10-min refresh).")
    next_fetch = datetime.now()

    while True:
        try:
            now = datetime.now()

            # Fetch every 10 minutes
            if now >= next_fetch:
                articles = fetch_news()
                _save_news_cache(articles)
                next_fetch = now + timedelta(minutes=10)
                log.debug(f"News refresh complete. Next in 10min.")

        except Exception as e:
            log.error(f"News loop error: {e}")

        time.sleep(60)  # Check every minute
