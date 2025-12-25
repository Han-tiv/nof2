import time
import requests
from config import OI_BASE_URL as BASE

# =========================
# 🔗 URL mapping
# =========================
URLS = {
    "OPEN_INTEREST": BASE + "/fapi/v1/openInterest?symbol={symbol}",
    "FUNDING_RATE": BASE + "/fapi/v1/premiumIndex?symbol={symbol}",
    "TICKER_24HR": BASE + "/fapi/v1/ticker/24hr?symbol={symbol}",
}

# =========================
# 🔐 Simple in-memory cache
# =========================
_cached = {
    "oi": {},
    "funding": {},
    "24hr": {},
}

def _cache_get(group, key, ttl):
    item = _cached[group].get(key)
    if not item:
        return None
    if time.time() - item["ts"] > ttl:
        return None
    return item["value"]

def _cache_set(group, key, value):
    _cached[group][key] = {
        "value": value,
        "ts": time.time()
    }

# =========================
# 📌 API wrappers
# =========================
def get_open_interest(symbol):
    cached = _cache_get("oi", symbol, ttl=60)
    if cached is not None:
        return cached

    try:
        r = requests.get(
            URLS["OPEN_INTEREST"].format(symbol=symbol),
            timeout=5
        ).json()
        value = float(r.get("openInterest"))
    except Exception:
        value = None

    _cache_set("oi", symbol, value)
    return value

def get_funding_rate(symbol):
    cached = _cache_get("funding", symbol, ttl=60)
    if cached is not None:
        return cached

    try:
        r = requests.get(
            URLS["FUNDING_RATE"].format(symbol=symbol),
            timeout=5
        ).json()
        value = float(r.get("lastFundingRate"))
    except Exception:
        value = None

    _cache_set("funding", symbol, value)
    return value

def get_24hr_change(symbol):
    cached = _cache_get("24hr", symbol, ttl=60)
    if cached is not None:
        return cached

    try:
        j = requests.get(
            URLS["TICKER_24HR"].format(symbol=symbol),
            timeout=5
        ).json()
        result = {
            "priceChange": float(j.get("priceChange", 0)),
            "priceChangePercent": float(j.get("priceChangePercent", 0)),
            "lastPrice": float(j.get("lastPrice", 0)),
            "highPrice": float(j.get("highPrice", 0)),
            "lowPrice": float(j.get("lowPrice", 0)),
            "volume": float(j.get("volume", 0)),
            "quoteVolume": float(j.get("quoteVolume", 0)),
        }
    except Exception:
        result = None

    _cache_set("24hr", symbol, result)
    return result
