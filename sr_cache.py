import time

_cache: dict = {}
CACHE_TTL = 3600


def get_cached(symbol: str, tf: str) -> list[dict] | None:
    key = f"{symbol}_{tf}"
    entry = _cache.get(key)
    if entry and time.time() - entry["ts"] < CACHE_TTL:
        return entry["levels"]
    return None


def set_cached(symbol: str, tf: str, levels: list[dict]) -> None:
    _cache[f"{symbol}_{tf}"] = {"levels": levels, "ts": time.time()}


def cache_size() -> int:
    return len(_cache)


def clear_stale() -> int:
    now   = time.time()
    stale = [k for k, v in _cache.items() if now - v["ts"] > CACHE_TTL * 2]
    for k in stale: del _cache[k]
    return len(stale)
