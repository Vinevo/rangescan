import logging
logger = logging.getLogger(__name__)

BYBIT_MAKER_FEE      = 0.0001
GRID_FEE             = BYBIT_MAKER_FEE * 2
TF_MINUTES           = {"30": 30, "60": 60, "240": 240, "D": 1440}
OSCILLATIONS_PER_CANDLE = 0.4
GRID_MIN_STEP_PCT    = 0.003


def calc_profit(stats: dict, tf: str, funding_daily_pct: float = 0.0,
                deposit_usdt: float = 1000.0, duration_h: float = 24.0) -> dict:
    try:
        price      = stats["price"]
        range_low  = stats["range_low"]
        range_high = stats["range_high"]
        grid_count = stats["grid_count"]
        grid_step  = stats["grid_step"]
        step_pct   = grid_step / price
        profit_per = step_pct - GRID_FEE
        if profit_per <= 0:
            return _zero("Шаг сетки меньше комиссии")
        tf_min      = TF_MINUTES.get(tf, 60)
        candles     = duration_h * 60 / tf_min
        oscillations = candles * OSCILLATIONS_PER_CANDLE
        gross_pct   = profit_per * oscillations
        fund_pct    = funding_daily_pct / 100 * (duration_h / 24)
        net_pct     = gross_pct - abs(fund_pct)
        gross_usdt  = round(deposit_usdt * gross_pct, 4)
        net_usdt    = round(deposit_usdt * net_pct, 4)
        fund_usdt   = round(deposit_usdt * abs(fund_pct), 4)
        min_dep     = round(1.0 / max(net_pct, 0.0001), 2)
        apy_pct     = round(net_pct / duration_h * 24 * 365 * 100, 1) if duration_h > 0 else 0.0
        return {"deposit": deposit_usdt, "duration_h": duration_h,
                "step_pct": round(step_pct * 100, 4), "oscillations": round(oscillations, 1),
                "gross_profit_pct": round(gross_pct * 100, 3), "gross_profit_usdt": gross_usdt,
                "funding_cost_usdt": fund_usdt, "net_profit_pct": round(net_pct * 100, 3),
                "net_profit_usdt": net_usdt, "min_deposit": min_dep,
                "apy_pct": apy_pct, "is_profitable": net_usdt > 0, "error": None}
    except Exception as e:
        return _zero(str(e))


def _zero(reason):
    return {"deposit": 0, "duration_h": 0, "step_pct": 0, "oscillations": 0,
            "gross_profit_pct": 0, "gross_profit_usdt": 0, "funding_cost_usdt": 0,
            "net_profit_pct": 0, "net_profit_usdt": 0, "min_deposit": 0,
            "apy_pct": 0, "is_profitable": False, "error": reason}


def format_profit_block(p: dict) -> str:
    if p.get("error") and p["net_profit_usdt"] == 0:
        return f"⚠️ Расчёт прибыли: {p['error']}"
    icon = "✅" if p["is_profitable"] else "🔴"
    sign = "+" if p["net_profit_usdt"] >= 0 else ""
    lines = [
        f"💰 *Расчёт прибыли* (депозит ${p['deposit']:.0f}, {p['duration_h']:.0f}ч):",
        f"   Шаг сетки: `{p['step_pct']:.3f}%`",
        f"   Колебаний за период: `~{p['oscillations']:.0f}`",
        f"   Валовая прибыль: `+${p['gross_profit_usdt']:.2f}` ({p['gross_profit_pct']:+.3f}%)",
    ]
    if p["funding_cost_usdt"] > 0:
        lines.append(f"   Funding: `-${p['funding_cost_usdt']:.2f}`")
    lines += [
        f"   {icon} Чистая: `{sign}${p['net_profit_usdt']:.2f}` ({p['net_profit_pct']:+.3f}%)",
        f"   📈 APY: `{p['apy_pct']:.1f}%`",
    ]
    if not p["is_profitable"]:
        lines.append("   ⛔ Funding перекрывает прибыль")
    return "\n".join(lines)
