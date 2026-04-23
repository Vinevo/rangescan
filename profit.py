"""
profit.py — Расчёт потенциальной прибыли Grid Bot.

Формула прибыли grid бота на боковике:
    profit = grid_count × grid_step_pct × deposit × (duration_h / candle_h)
    где grid_step_pct = (range_high - range_low) / grid_count / price

Учитывает:
    - Комиссию Bybit (maker 0.01%, taker 0.06%)
    - Funding rate за период
    - Минимальный депозит на сетку

Bybit Grid Bot: каждый раз когда цена проходит один шаг сетки
туда-обратно — фиксируется прибыль = шаг сетки.
За N часов боковика при среднем ATR цена проходит step примерно
(duration_h × 60 / candle_min × atr_ratio / step_ratio) раз.
"""

import logging

logger = logging.getLogger(__name__)

# Комиссии Bybit (linear perp, maker/taker)
BYBIT_MAKER_FEE = 0.0001   # 0.01%
BYBIT_TAKER_FEE = 0.0006   # 0.06%
# Grid бот использует лимитные ордера (maker) для большинства сделок
GRID_FEE = BYBIT_MAKER_FEE * 2   # вход + выход

# Таймфреймы → минут на свечу
TF_MINUTES = {"30": 30, "60": 60, "240": 240, "D": 1440}

# Сколько раз цена в среднем проходит один шаг за свечу
# (эмпирический коэффициент для flat рынка, консервативная оценка)
OSCILLATIONS_PER_CANDLE = 0.4


def calc_profit(
    stats: dict,
    tf: str,
    funding_daily_pct: float = 0.0,
    deposit_usdt: float = 1000.0,
    duration_h: float = 24.0,
) -> dict:
    """
    Рассчитывает ожидаемую прибыль grid бота.

    Параметры:
        stats           — dict из analyse_flat()
        tf              — таймфрейм ("30", "60", "240", "D")
        funding_daily_pct — funding в % за сутки (может быть отрицательным)
        deposit_usdt    — размер депозита в USDT
        duration_h      — ожидаемая длительность боковика в часах

    Возвращает dict с расчётами.
    """
    try:
        price       = stats["price"]
        range_low   = stats["range_low"]
        range_high  = stats["range_high"]
        grid_count  = stats["grid_count"]
        grid_step   = stats["grid_step"]

        # Шаг сетки в %
        step_pct    = grid_step / price

        # Прибыль за одно полное колебание (туда-обратно) по одной сетке
        profit_per_oscillation = step_pct - GRID_FEE

        if profit_per_oscillation <= 0:
            # Комиссия съедает шаг — grid нецелесообразен
            return _zero_result(deposit_usdt, "Шаг сетки меньше комиссии")

        # Свечей за период
        tf_min      = TF_MINUTES.get(tf, 60)
        candles     = duration_h * 60 / tf_min

        # Среднее кол-во полных колебаний цены за период
        oscillations = candles * OSCILLATIONS_PER_CANDLE

        # Прибыль в % от депозита за период (до funding)
        gross_profit_pct = profit_per_oscillation * oscillations

        # Вычитаем funding (в % за период)
        funding_period_pct = funding_daily_pct / 100 * (duration_h / 24)
        net_profit_pct     = gross_profit_pct - abs(funding_period_pct)

        # В USDT
        gross_profit_usdt = round(deposit_usdt * gross_profit_pct, 4)
        net_profit_usdt   = round(deposit_usdt * net_profit_pct, 4)
        funding_cost_usdt = round(deposit_usdt * abs(funding_period_pct), 4)

        # Минимальный депозит чтобы было хоть $1 прибыли
        min_deposit = round(1.0 / max(net_profit_pct, 0.0001), 2)

        # APY (годовой эквивалент)
        if duration_h > 0:
            apy_pct = round(net_profit_pct / duration_h * 24 * 365 * 100, 1)
        else:
            apy_pct = 0.0

        is_profitable = net_profit_usdt > 0

        return {
            "deposit":            deposit_usdt,
            "duration_h":         duration_h,
            "step_pct":           round(step_pct * 100, 4),
            "oscillations":       round(oscillations, 1),
            "gross_profit_pct":   round(gross_profit_pct * 100, 3),
            "gross_profit_usdt":  gross_profit_usdt,
            "funding_cost_usdt":  funding_cost_usdt,
            "net_profit_pct":     round(net_profit_pct * 100, 3),
            "net_profit_usdt":    net_profit_usdt,
            "min_deposit":        min_deposit,
            "apy_pct":            apy_pct,
            "is_profitable":      is_profitable,
            "error":              None,
        }

    except Exception as e:
        logger.error(f"calc_profit error: {e}")
        return _zero_result(deposit_usdt, str(e))


def _zero_result(deposit: float, reason: str) -> dict:
    return {
        "deposit":            deposit,
        "duration_h":         0,
        "step_pct":           0,
        "oscillations":       0,
        "gross_profit_pct":   0,
        "gross_profit_usdt":  0,
        "funding_cost_usdt":  0,
        "net_profit_pct":     0,
        "net_profit_usdt":    0,
        "min_deposit":        0,
        "apy_pct":            0,
        "is_profitable":      False,
        "error":              reason,
    }


def format_profit_block(p: dict) -> str:
    """Форматирует блок расчёта прибыли для Telegram сообщения."""
    if p.get("error") and p["net_profit_usdt"] == 0:
        return f"⚠️ Расчёт прибыли: {p['error']}"

    profitable_icon = "✅" if p["is_profitable"] else "🔴"
    sign = "+" if p["net_profit_usdt"] >= 0 else ""

    lines = [
        f"💰 *Расчёт прибыли* (депозит ${p['deposit']:.0f}, {p['duration_h']:.0f}ч):",
        f"   Шаг сетки: `{p['step_pct']:.3f}%`",
        f"   Колебаний за период: `~{p['oscillations']:.0f}`",
        f"   Валовая прибыль: `+${p['gross_profit_usdt']:.2f}` ({p['gross_profit_pct']:+.3f}%)",
    ]

    if p["funding_cost_usdt"] > 0:
        lines.append(f"   Funding издержки: `-${p['funding_cost_usdt']:.2f}`")

    lines += [
        f"   {profitable_icon} Чистая прибыль: `{sign}${p['net_profit_usdt']:.2f}` ({p['net_profit_pct']:+.3f}%)",
        f"   📈 APY эквивалент: `{p['apy_pct']:.1f}%`",
    ]

    if not p["is_profitable"]:
        lines.append(f"   ⛔ Funding перекрывает прибыль — Grid не рекомендован")
    elif p["min_deposit"] > 100:
        lines.append(f"   💡 Мин. депозит для $1 прибыли: `${p['min_deposit']:.0f}`")

    return "\n".join(lines)
