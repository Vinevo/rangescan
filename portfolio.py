"""
portfolio.py — Личный портфель активных сделок.

Хранит сделки которые пользователь взял в работу.
Отдельно от active_flats (которые отслеживает сканер).

Структура сделки:
{
    "key":        "POLUSDT_30",
    "symbol":     "POLUSDT",
    "tf":         "30",
    "mode":       "grid" | "breakout",
    "entry_price": 0.10187,
    "range_low":  0.10132,
    "range_high": 0.10358,
    "stop_loss":  0.09828,     # −3% от range_low
    "stop_high":  0.10876,     # +5% от range_high (только для grid)
    "deposit":    50.0,        # введён пользователем
    "grid_count": 10,
    "score":      8,
    "since":      1234567890,
    "status":     "active" | "closed" | "breakout_triggered",
    "profit_est": 0.0,         # расчётная прибыль (обновляется)
    "closed_at":  None,
    "close_reason": None,
}
"""

import json
import logging
import os
import time

logger = logging.getLogger(__name__)

PORTFOLIO_FILE = "portfolio.json"
TF_MINUTES     = {"30": 30, "60": 60, "240": 240, "D": 1440}
TF_LABELS      = {"30": "30м", "60": "1ч", "240": "4ч", "D": "1д"}

# Параметры стоп-лоссов
STOP_LOW_PCT   = 0.03   # нижний стоп = range_low × (1 - 0.03)
STOP_HIGH_PCT  = 0.05   # верхний стоп = range_high × (1 + 0.05)

_portfolio: dict = {}   # { key: trade_dict }


# ──────────────────────────────────────────────────────────────────────────────
#  ПЕРСИСТЕНТНОСТЬ
# ──────────────────────────────────────────────────────────────────────────────

def load_portfolio():
    global _portfolio
    if not os.path.exists(PORTFOLIO_FILE):
        _portfolio = {}
        return
    try:
        with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
            _portfolio = json.load(f)
        # Чистим закрытые старше 7 дней
        cutoff = time.time() - 7 * 24 * 3600
        _portfolio = {
            k: v for k, v in _portfolio.items()
            if v.get("status") == "active" or v.get("closed_at", 0) > cutoff
        }
        logger.info(f"💼 Портфель загружен: {len(_portfolio)} сделок")
    except Exception as e:
        logger.error(f"load_portfolio: {e}")
        _portfolio = {}


def save_portfolio():
    try:
        tmp = PORTFOLIO_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_portfolio, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, PORTFOLIO_FILE)
    except Exception as e:
        logger.error(f"save_portfolio: {e}")


# ──────────────────────────────────────────────────────────────────────────────
#  УПРАВЛЕНИЕ СДЕЛКАМИ
# ──────────────────────────────────────────────────────────────────────────────

def add_trade(stats: dict, symbol: str, tf: str,
              deposit: float = 100.0) -> dict:
    """
    Добавляет сделку в портфель.
    Рассчитывает стоп-лоссы автоматически.
    """
    key        = f"{symbol}_{tf}"
    mode       = stats.get("mode", "grid")
    rl         = stats.get("range_low",  0)
    rh         = stats.get("range_high", 0)
    price      = stats.get("price", 0)
    smart      = stats.get("smart", {})
    rng_info   = smart.get("range_info", {})

    # Используем range из smart_analysis если он более точный
    if rng_info.get("is_valid"):
        rl = rng_info.get("range_low", rl)
        rh = rng_info.get("range_high", rh)

    stop_loss  = round(rl * (1 - STOP_LOW_PCT), 8)  if rl > 0 else 0
    stop_high  = round(rh * (1 + STOP_HIGH_PCT), 8) if rh > 0 else 0

    trade = {
        "key":           key,
        "symbol":        symbol,
        "tf":            tf,
        "mode":          mode,
        "entry_price":   price,
        "range_low":     rl,
        "range_high":    rh,
        "stop_loss":     stop_loss,
        "stop_high":     stop_high,
        "deposit":       deposit,
        "grid_count":    stats.get("grid_count", 5),
        "grid_step":     stats.get("grid_step", 0),
        "range_pct":     stats.get("range_pct", 0),
        "score":         stats.get("score", 0),
        "since":         time.time(),
        "status":        "active",
        "profit_est":    0.0,
        "closed_at":     None,
        "close_reason":  None,
        "funding_daily": stats.get("funding", {}).get("daily_pct", 0.0),
    }

    _portfolio[key] = trade
    save_portfolio()
    logger.info(f"💼 Добавлена сделка: {symbol} [{TF_LABELS.get(tf,tf)}] "
                f"стоп↓{stop_loss} стоп↑{stop_high}")
    return trade


def remove_trade(key: str) -> bool:
    if key in _portfolio:
        del _portfolio[key]
        save_portfolio()
        return True
    return False


def close_trade(key: str, reason: str):
    if key in _portfolio:
        _portfolio[key]["status"]       = "closed"
        _portfolio[key]["closed_at"]    = time.time()
        _portfolio[key]["close_reason"] = reason
        save_portfolio()


def get_active_trades() -> list[dict]:
    return [t for t in _portfolio.values() if t.get("status") == "active"]


def get_all_trades() -> list[dict]:
    return list(_portfolio.values())


def get_trade(key: str) -> dict | None:
    return _portfolio.get(key)


def is_in_portfolio(key: str) -> bool:
    t = _portfolio.get(key)
    return t is not None and t.get("status") == "active"


# ──────────────────────────────────────────────────────────────────────────────
#  РАСЧЁТ ПРИБЫЛИ
# ──────────────────────────────────────────────────────────────────────────────

def estimate_profit(trade: dict) -> float:
    """
    Расчётная прибыль Grid бота с момента входа.
    Консервативная оценка: 0.4 колебания за свечу.
    """
    try:
        now          = time.time()
        elapsed_h    = (now - trade["since"]) / 3600
        tf_min       = TF_MINUTES.get(trade["tf"], 60)
        candles      = elapsed_h * 60 / tf_min
        oscillations = candles * 0.4

        rl  = trade["range_low"]
        rh  = trade["range_high"]
        gc  = trade["grid_count"]
        dep = trade["deposit"]

        if gc <= 0 or rl <= 0 or rh <= 0:
            return 0.0

        step_pct    = (rh - rl) / gc / ((rl + rh) / 2)
        fee         = 0.0002   # maker × 2
        profit_per  = max(step_pct - fee, 0)
        gross       = profit_per * oscillations * dep
        fund_cost   = abs(trade.get("funding_daily", 0) / 100) * (elapsed_h / 24) * dep
        net         = gross - fund_cost

        trade["profit_est"] = round(net, 4)
        return trade["profit_est"]
    except Exception:
        return 0.0


# ──────────────────────────────────────────────────────────────────────────────
#  МОНИТОРИНГ — проверка стопов
# ──────────────────────────────────────────────────────────────────────────────

def check_trade_status(trade: dict, current_price: float) -> str | None:
    """
    Проверяет не сработал ли стоп по сделке.

    Возвращает:
        "stop_low"   — цена ниже стоп-лосса
        "stop_high"  — цена выше верхнего стопа
        "in_range"   — всё нормально
        "near_stop"  — цена опасно близко к стопу (в пределах 1%)
    """
    if trade.get("status") != "active":
        return None

    sl = trade.get("stop_loss", 0)
    sh = trade.get("stop_high", 0)
    rl = trade.get("range_low", 0)
    rh = trade.get("range_high", 0)

    if sl > 0 and current_price <= sl:
        return "stop_low"
    if sh > 0 and current_price >= sh:
        return "stop_high"

    # Предупреждение если цена близко к нижнему стопу (в пределах 1.5%)
    if sl > 0 and (current_price - sl) / current_price < 0.015:
        return "near_stop_low"
    if sh > 0 and (sh - current_price) / current_price < 0.015:
        return "near_stop_high"

    return "in_range"


def update_deposit(key: str, deposit: float):
    """Обновляет депозит по сделке."""
    if key in _portfolio:
        _portfolio[key]["deposit"] = deposit
        save_portfolio()
