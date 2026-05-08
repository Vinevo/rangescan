import logging
import time
from pybit.unified_trading import HTTP

logger = logging.getLogger(__name__)

FUNDING_DANGER_THRESHOLD = 0.001
FUNDING_WARN_THRESHOLD   = 0.0005

_cache: dict = {}
CACHE_TTL = 3600

session = HTTP(testnet=False)


def get_funding_rate(symbol: str) -> float | None:
    now = time.time()
    cached = _cache.get(symbol)
    if cached and now - cached["ts"] < CACHE_TTL:
        return cached["rate"]
    try:
        resp = session.get_tickers(category="linear", symbol=symbol)
        items = resp.get("result", {}).get("list", [])
        if not items:
            return None
        rate = float(items[0].get("fundingRate", 0))
        _cache[symbol] = {"rate": rate, "ts": now}
        return rate
    except Exception as e:
        logger.debug(f"funding {symbol}: {e}")
        return None


def analyse_funding(symbol: str) -> dict:
    rate = get_funding_rate(symbol)
    if rate is None:
        return {"rate": 0.0, "rate_pct": "н/д", "daily_pct": 0.0,
                "is_safe": True, "is_warning": False,
                "direction": "neutral", "comment": "⚪ Funding: нет данных"}
    abs_rate  = abs(rate)
    daily_pct = round(rate * 3 * 100, 4)
    direction = "long_squeeze" if rate > 0 else ("short_squeeze" if rate < 0 else "neutral")
    is_safe    = abs_rate < FUNDING_DANGER_THRESHOLD
    is_warning = abs_rate >= FUNDING_WARN_THRESHOLD and abs_rate < FUNDING_DANGER_THRESHOLD
    rate_str  = f"{rate * 100:+.4f}%"
    daily_str = f"{daily_pct:+.3f}%/сутки"
    if not is_safe:
        comment = f"🔴 Funding {rate_str} ({daily_str}) — опасен для Grid"
    elif is_warning:
        comment = f"🟡 Funding {rate_str} ({daily_str}) — повышен"
    else:
        comment = f"🟢 Funding {rate_str} ({daily_str}) — нейтральный"
    return {"rate": rate, "rate_pct": rate_str, "daily_pct": daily_pct,
            "is_safe": is_safe, "is_warning": is_warning,
            "direction": direction, "comment": comment}
