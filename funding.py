"""
funding.py — Фильтр по funding rate.

Funding на Bybit перп-фьючерсах платится каждые 8 часов.
Если funding слишком отрицательный — лонг-позиции в grid боте
постоянно платят шорт-держателям → grid работает в минус.

Логика:
  - funding > +0.1%  за 8ч → рынок перегрет лонгами, опасен шорт
  - funding < -0.1%  за 8ч → рынок перегрет шортами, опасен лонг
  - |funding| < 0.05% → нейтрально, Grid Bot безопасен
"""

import logging
import time
from pybit.unified_trading import HTTP

logger = logging.getLogger(__name__)

# Порог: |funding за 8ч| выше этого — опасно для grid
FUNDING_DANGER_THRESHOLD = 0.001    # 0.1% за 8ч
FUNDING_WARN_THRESHOLD   = 0.0005   # 0.05% за 8ч — предупреждение

# Кэш: { symbol: {"rate": float, "ts": float} }
_cache: dict = {}
CACHE_TTL = 3600   # Обновляем раз в час (funding меняется каждые 8ч)

session = HTTP(testnet=False)


def get_funding_rate(symbol: str) -> float | None:
    """
    Получает текущий predicted funding rate для символа.
    Возвращает float (напр. 0.0001 = 0.01%) или None при ошибке.
    Кэшируется на CACHE_TTL секунд.
    """
    now = time.time()
    cached = _cache.get(symbol)
    if cached and now - cached["ts"] < CACHE_TTL:
        return cached["rate"]

    try:
        resp = session.get_tickers(category="linear", symbol=symbol)
        items = resp.get("result", {}).get("list", [])
        if not items:
            return None

        rate_str = items[0].get("fundingRate", None)
        if rate_str is None:
            return None

        rate = float(rate_str)
        _cache[symbol] = {"rate": rate, "ts": now}
        return rate

    except Exception as e:
        logger.debug(f"funding {symbol}: {e}")
        return None


def analyse_funding(symbol: str) -> dict:
    """
    Возвращает полный анализ funding для сигнала.

    Возвращает dict:
        rate          float   — текущий funding (за 8ч)
        rate_pct      str     — "0.0100%" для отображения
        daily_pct     float   — funding в сутки (×3)
        is_safe       bool    — безопасно ли запускать grid
        is_warning    bool    — есть предупреждение но не критично
        direction     str     — "neutral" / "long_squeeze" / "short_squeeze"
        comment       str     — текстовый вывод для Telegram
    """
    rate = get_funding_rate(symbol)

    if rate is None:
        return {
            "rate": 0.0, "rate_pct": "н/д", "daily_pct": 0.0,
            "is_safe": True, "is_warning": False,
            "direction": "neutral", "comment": "⚪ Funding: нет данных"
        }

    abs_rate  = abs(rate)
    daily_pct = round(rate * 3 * 100, 4)   # 3 выплаты в сутки

    if rate > 0:
        direction = "long_squeeze"   # лонги перегреты, шорты получают
    elif rate < 0:
        direction = "short_squeeze"  # шорты перегреты, лонги получают
    else:
        direction = "neutral"

    is_safe    = abs_rate < FUNDING_DANGER_THRESHOLD
    is_warning = abs_rate >= FUNDING_WARN_THRESHOLD and abs_rate < FUNDING_DANGER_THRESHOLD

    # Формируем comment для Telegram
    rate_str = f"{rate * 100:+.4f}%"
    daily_str = f"{daily_pct:+.3f}%/сутки"

    if not is_safe:
        if direction == "long_squeeze":
            comment = f"🔴 Funding {rate_str} ({daily_str}) — лонги переплачивают, Grid опасен"
        else:
            comment = f"🔴 Funding {rate_str} ({daily_str}) — шорты переплачивают, Grid опасен"
    elif is_warning:
        comment = f"🟡 Funding {rate_str} ({daily_str}) — повышен, следи"
    else:
        comment = f"🟢 Funding {rate_str} ({daily_str}) — нейтральный"

    return {
        "rate":      rate,
        "rate_pct":  rate_str,
        "daily_pct": daily_pct,
        "is_safe":   is_safe,
        "is_warning": is_warning,
        "direction": direction,
        "comment":   comment,
    }


def clear_cache():
    """Очистить кэш (например при рестарте)."""
    _cache.clear()
