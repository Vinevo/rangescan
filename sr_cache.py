"""
sr_cache.py — Кэш уровней поддержки/сопротивления.

S/R уровни пересчитываются дорого (50+ свечей, pivot алгоритм).
Они не меняются быстро — кэшируем на 1 час.

Структура кэша: { "BTCUSDT_60": {"levels": [...], "ts": float} }
"""

import time
import logging

logger = logging.getLogger(__name__)

_cache: dict = {}
CACHE_TTL = 3600   # 1 час


def get_cached(symbol: str, tf: str) -> list[dict] | None:
    """Возвращает кэшированные уровни или None если кэш устарел."""
    key = f"{symbol}_{tf}"
    entry = _cache.get(key)
    if entry and time.time() - entry["ts"] < CACHE_TTL:
        return entry["levels"]
    return None


def set_cached(symbol: str, tf: str, levels: list[dict]) -> None:
    """Сохраняет уровни в кэш."""
    key = f"{symbol}_{tf}"
    _cache[key] = {"levels": levels, "ts": time.time()}


def cache_size() -> int:
    return len(_cache)


def clear_stale() -> int:
    """Удаляет устаревшие записи. Возвращает кол-во удалённых."""
    now     = time.time()
    stale   = [k for k, v in _cache.items() if now - v["ts"] > CACHE_TTL * 2]
    for k in stale:
        del _cache[k]
    return len(stale)
