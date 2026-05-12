"""
notifier.py — Bybit Flat Scanner v9

Форматы сообщений:
  🟢 GRID  — зелёная шапка, настройки сетки, расчёт прибыли
  ⚡ BREAKOUT ожидание — оранжевая шапка, уровни пробоя
  💥 BREAKOUT случился — красная/зелёная шапка, направление, точка входа
  ⚠️ ВЫХОД из боковика — серая шапка
  📅 Дневной отчёт
"""

import os
import time
import logging
import aiohttp

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TF_LABELS        = {"30": "30м", "60": "1ч", "240": "4ч", "D": "1д"}
BYBIT_URL        = "https://www.bybit.com/trade/usdt/{symbol}"

_retry_queue: list[dict] = []
MAX_RETRIES = 3


# ──────────────────────────────────────────────────────────────────────────────
#  БАЗОВАЯ ОТПРАВКА
# ──────────────────────────────────────────────────────────────────────────────

async def _send_raw(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID, "text": text,
                "parse_mode": "Markdown", "disable_web_page_preview": True},
                timeout=aiohttp.ClientTimeout(total=10)) as r:
                return r.status == 200
    except Exception as e:
        logger.warning(f"Telegram: {e}")
        return False


async def _send(text: str):
    if not await _send_raw(text):
        _retry_queue.append({"text": text, "attempts": 1})


async def flush_retry_queue():
    if not _retry_queue:
        return
    pending = []
    for item in _retry_queue:
        if await _send_raw(item["text"]):
            logger.info("✅ Retry sent")
        else:
            item["attempts"] += 1
            if item["attempts"] < MAX_RETRIES:
                pending.append(item)
    _retry_queue.clear()
    _retry_queue.extend(pending)


# ──────────────────────────────────────────────────────────────────────────────
#  УТИЛИТЫ
# ──────────────────────────────────────────────────────────────────────────────

def _vol(v: float) -> str:
    if v >= 1e9: return f"${v/1e9:.1f}B"
    if v >= 1e6: return f"${v/1e6:.1f}M"
    return f"${v/1e3:.0f}K"


def _bar(score: int, total: int = 10) -> str:
    filled = round(score / total * 8)
    return "█" * filled + "░" * (8 - filled)


def _smart_compact(smart: dict) -> str:
    """Компактный блок Smart Money — 3 строки максимум."""
    if not smart:
        return ""

    ms      = smart.get("market_structure", "range")
    rs      = smart.get("range_score", 0)
    br      = smart.get("breakout_risk", 0)
    pos     = smart.get("price_position", {})
    fvg     = smart.get("fvg", {})
    sweep   = smart.get("sweep", {})
    disp    = smart.get("displacement", {})
    imp     = smart.get("impulse", {})
    vol_acc = smart.get("volume_acc", {})
    sess    = smart.get("session", {})
    pd_z    = smart.get("premium_discount", {})
    wo      = smart.get("weekly_open", {})
    blocked = smart.get("grid_blocked", [])

    ms_map  = {"uptrend": "📈 Аптренд", "downtrend": "📉 Даунтренд", "range": "↔️ Флет"}
    pos_map = {"top": "🔝 Верх", "middle": "➡️ Середина", "bottom": "⬇️ Низ"}
    pd_map  = {"premium": "💎 Premium", "discount": "🏷 Discount", "equilibrium": "⚖️ Равновесие"}

    line1 = (f"{ms_map.get(ms,'↔️ Флет')} · "
             f"{pd_map.get(pd_z.get('zone','equilibrium'),'⚖️')} ({pd_z.get('pct',50):.0f}%) · "
             f"{pos_map.get(pos.get('position','middle'),'➡️')} ({pos.get('position_pct',50):.0f}%)")

    # Ключевые факторы риска
    risks = []
    if fvg.get("danger_above") and fvg.get("nearest_bear"):
        risks.append(f"🔴 FVG↑ {fvg['nearest_bear']['mid']}")
    if fvg.get("danger_below") and fvg.get("nearest_bull"):
        risks.append(f"🟢 FVG↓ {fvg['nearest_bull']['mid']}")
    if not risks:
        risks.append("✅ FVG чисто")
    if sweep.get("has_sweep"):
        sw = "🟢 Бычий" if sweep["sweep_type"] == "bull_sweep" else "🔴 Медвежий"
        risks.append(f"💧 {sw} sweep {sweep['candles_ago']}св.")
    if disp.get("has_displacement") and disp.get("is_recent"):
        risks.append(f"⚡ Displ. ×{disp['size_mult']}")
    if imp.get("has_impulse"):
        risks.append(f"💥 Импульс {imp['last_impulse_ago']}св.")
    if vol_acc.get("is_accumulation"):
        risks.append(f"📦 Накопл. ×{vol_acc['vol_ratio']}")
    if wo.get("is_far"):
        risks.append(f"📅 WO далеко {wo['dist_pct']:+.1f}%")
    if sess.get("is_overlap"):
        risks.append("⚠️ Overlap сессия")

    line2 = " · ".join(risks[:4])   # не больше 4 факторов

    return (
        f"🧠 *Smart Money*\n"
        f"`{_bar(rs)}` Range {rs}/10   `{_bar(br)}` Risk {br}/10\n"
        f"{line1}\n"
        f"{line2}"
    )


def _grid_count_label(grid_count: int, deposit: float = 1000.0,
                      range_low: float = 0, range_high: float = 0,
                      price: float = 0) -> str:
    """
    Поясняет логику количества сеток.
    Рекомендует оптимальное кол-во под депозит.
    """
    if price <= 0 or range_high <= range_low:
        return str(grid_count)

    span      = range_high - range_low
    step_usdt = span / grid_count * (deposit / price)  # примерный размер 1 ордера

    if step_usdt < 5:
        note = " ⚠️ мало"   # ордер слишком маленький
    elif step_usdt > 500:
        note = " ⚠️ много"  # слишком крупные ордера
    else:
        note = " ✅"

    return f"{grid_count}{note}"


# ──────────────────────────────────────────────────────────────────────────────
#  GRID СИГНАЛ
# ──────────────────────────────────────────────────────────────────────────────

async def send_signal(symbol: str, tf: str, stats: dict):
    tf_label = TF_LABELS.get(tf, tf)
    price    = stats["price"]
    score    = stats["score"]
    mode     = stats.get("mode", "grid")
    link     = BYBIT_URL.format(symbol=symbol)
    vol24h   = stats.get("volume24h", 0)
    funding  = stats.get("funding", {})
    smart    = stats.get("smart", {})

    if mode == "breakout":
        await _send_breakout_watch(symbol, tf, tf_label, price, score,
                                   vol24h, funding, smart, stats, link)
    else:
        await _send_grid(symbol, tf, tf_label, price, score,
                         vol24h, funding, smart, stats, link)


async def _send_grid(symbol, tf, tf_label, price, score, vol24h, funding, smart, stats, link):
    rsi_val  = stats.get("rsi", 50)
    rsi_flat = stats.get("rsi_flat", False)
    sr       = stats.get("sr", {})
    profit   = stats.get("profit", {})

    rsi_icon = "✅" if rsi_flat else "⚠️"

    # S/R одной строкой
    sr_line = ""
    if sr.get("sandwiched"):
        sr_line = "🏆 Зажат между уровнями"
    elif sr.get("has_support") and sr.get("has_resistance"):
        sr_line = f"🟢 {sr['support_below']['price']}  🔴 {sr['resistance_above']['price']}"
    elif sr.get("has_support"):
        sr_line = f"🟢 Поддержка {sr['support_below']['price']}"
    elif sr.get("has_resistance"):
        sr_line = f"🔴 Сопротивление {sr['resistance_above']['price']}"

    # Сетки
    gc_label = _grid_count_label(
        stats["grid_count"], 1000.0,
        stats["range_low"], stats["range_high"], price)

    # Стоп-лоссы
    rl = stats["range_low"]
    rh = stats["range_high"]
    stop_low  = round(rl * 0.97, 8)
    stop_high = round(rh * 1.05, 8)
    max_loss  = round((price - stop_low) / price * 100, 2)

    stop_block = (
        f"\n🛡 *Защита Grid Bot*\n"
        f"┌ Нижний стоп: `{stop_low}` (−3% от границы)\n"
        f"├ Верхний стоп: `{stop_high}` (+5%) или выключить\n"
        f"├ Макс. убыток при $1000: `~${round(1000 * max_loss/100, 1)}`\n"
        f"└ ⚠️ Плечо: только *1x*!"
    )

    # Прибыль
    p = profit
    profit_line = ""
    if p and not p.get("error"):
        sign = "+" if p["net_profit_usdt"] >= 0 else ""
        icon = "✅" if p["is_profitable"] else "🔴"
        profit_line = (
            f"\n💰 *Прибыль* ($1000 · {p['duration_h']:.0f}ч)\n"
            f"┌ Валовая: `+${p['gross_profit_usdt']:.2f}`\n"
            f"├ Funding: `-${p['funding_cost_usdt']:.2f}`\n"
            f"└ {icon} Чистая: `{sign}${p['net_profit_usdt']:.2f}` · APY `{p['apy_pct']:.0f}%`"
        )

    vol_warn = "\n⚠️ *Объём растёт* — возможен пробой!" if stats.get("vol_growing") else ""
    sr_block = f"\n📌 {sr_line}" if sr_line else ""

    # Ключ для callback кнопки
    key = f"{symbol}_{tf}"

    text = (
        f"🤖 *GRID · {symbol}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⏱ `{tf_label}` · 💲 `{price}` · 📊 `{score}/10`\n"
        f"💹 {_vol(vol24h)} · {funding.get('comment','')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📉 ADX `{stats['adx']}` ✅  "
        f"📏 BB `{stats['bb_width_pct']}%` ✅  "
        f"🌊 ATR `{stats['atr_pct']}%` ✅\n"
        f"📈 RSI `{rsi_val}` {rsi_icon}  "
        f"🕯 `{stats['flat_candles']}` св.  "
        f"🎯 пробоев: `{stats['false_breaks']}`"
        f"{sr_block}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{_smart_compact(smart)}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ *Grid настройки*\n"
        f"↓ `{stats['range_low']}` ↔ `{stats['range_high']}` ↑\n"
        f"📐 `{stats['range_pct']}%` · # `{gc_label}` сеток · шаг `{stats['grid_step']}`"
        f"{stop_block}"
        f"{profit_line}"
        f"{vol_warn}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔗 [Открыть на Bybit]({link})"
    )

    # Inline кнопки под сигналом
    markup = {
        "inline_keyboard": [[
            {"text": "✅ Взять в работу", "callback_data": f"add_trade:{key}"},
            {"text": "🔗 Bybit",          "url": link},
        ]]
    }
    await _send_with_markup(text, markup)


async def _send_with_markup(text: str, markup: dict):
    """Отправка сообщения с inline-кнопками."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID, "text": text,
                "parse_mode": "Markdown", "disable_web_page_preview": True,
                "reply_markup": markup},
                timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return True
                body = await r.text()
                logger.warning(f"Telegram markup {r.status}: {body[:200]}")
                return False
    except Exception as e:
        logger.warning(f"Telegram markup: {e}")
        return False


# ──────────────────────────────────────────────────────────────────────────────
#  BREAKOUT ОЖИДАНИЕ
# ──────────────────────────────────────────────────────────────────────────────

async def _send_breakout_watch(symbol, tf, tf_label, price, score, vol24h, funding, smart, stats, link):
    rng = smart.get("range_info", {})
    rh  = rng.get("range_high", stats.get("range_high", 0))
    rl  = rng.get("range_low",  stats.get("range_low",  0))

    sweep = smart.get("sweep", {})
    sweep_hint = ""
    if sweep.get("has_sweep"):
        if sweep["direction"] == "up":
            sweep_hint = "\n💧 Бычий sweep → *вероятнее пробой вверх*"
        else:
            sweep_hint = "\n💧 Медвежий sweep → *вероятнее пробой вниз*"

    key = f"{symbol}_{tf}"

    text = (
        f"⚡ *BREAKOUT · {symbol}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⏱ `{tf_label}` · 💲 `{price}` · 📊 `{score}/10`\n"
        f"💹 {_vol(vol24h)} · {funding.get('comment','')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📉 ADX `{stats['adx']}` ✅  "
        f"📏 BB `{stats['bb_width_pct']}%` ✅  "
        f"🌊 ATR `{stats['atr_pct']}%` ✅\n"
        f"🕯 `{stats['flat_candles']}` св.  🎯 пробоев: `{stats['false_breaks']}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{_smart_compact(smart)}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 *Уровни пробоя*\n"
        f"🟢 Лонг если пробьёт: `{rh}`\n"
        f"🔴 Шорт если пробьёт: `{rl}`"
        f"{sweep_hint}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ Входить только после *закрытия свечи* за уровнем\n"
        f"🔗 [Открыть на Bybit]({link})"
    )

    markup = {
        "inline_keyboard": [[
            {"text": "👁 Следить за пробоем", "callback_data": f"add_trade:{key}"},
            {"text": "🔗 Bybit", "url": link},
        ]]
    }
    await _send_with_markup(text, markup)


# ──────────────────────────────────────────────────────────────────────────────
#  BREAKOUT СЛУЧИЛСЯ  ← НОВОЕ
# ──────────────────────────────────────────────────────────────────────────────

async def send_breakout_alert(symbol: str, tf: str, stats: dict,
                               direction: str, breakout_price: float,
                               prev_range_high: float, prev_range_low: float):
    """
    Уведомление когда цена реально пробила диапазон.

    direction: "up" (бычий пробой) или "down" (медвежий пробой)
    breakout_price: текущая цена после пробоя
    """
    tf_label = TF_LABELS.get(tf, tf)
    link     = BYBIT_URL.format(symbol=symbol)
    price    = stats.get("price", breakout_price)

    if direction == "up":
        header    = f"🚀 *ПРОБОЙ ВВЕРХ · {symbol}*"
        action    = "🟢 *Лонг* — цена закрылась выше диапазона"
        entry     = f"Вход: `{breakout_price}` (текущая цена)"
        stop      = f"Стоп-лосс: `{round(prev_range_high * 0.99, 8)}` (под уровнем)"
        target    = f"Цель: `{round(prev_range_high + (prev_range_high - prev_range_low), 8)}` (+{round((prev_range_high - prev_range_low) / prev_range_high * 100, 2)}%)"
        move_pct  = round((breakout_price - prev_range_high) / prev_range_high * 100, 2)
        move_icon = "📈"
    else:
        header    = f"📉 *ПРОБОЙ ВНИЗ · {symbol}*"
        action    = "🔴 *Шорт* — цена закрылась ниже диапазона"
        entry     = f"Вход: `{breakout_price}` (текущая цена)"
        stop      = f"Стоп-лосс: `{round(prev_range_low * 1.01, 8)}` (над уровнем)"
        target    = f"Цель: `{round(prev_range_low - (prev_range_high - prev_range_low), 8)}` (-{round((prev_range_high - prev_range_low) / prev_range_low * 100, 2)}%)"
        move_pct  = round((prev_range_low - breakout_price) / prev_range_low * 100, 2)
        move_icon = "📉"

    # Сколько держался боковик
    since      = stats.get("since", 0)
    import time
    duration_h = round((time.time() - since) / 3600, 1) if since else 0
    tf_hours   = {"30": 0.5, "60": 1.0, "240": 4.0, "D": 24.0}
    flat_h     = stats.get("flat_candles", 0) * tf_hours.get(tf, 1.0)

    text = (
        f"{header}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⏱ `{tf_label}` · 💲 `{breakout_price}`\n"
        f"{move_icon} Движение от диапазона: `{move_pct:+.2f}%`\n"
        f"⏳ Боковик держался: `{flat_h:.0f}ч`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📐 *Был диапазон:* `{prev_range_low}` — `{prev_range_high}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{action}\n\n"
        f"📍 {entry}\n"
        f"🛡 {stop}\n"
        f"🎯 {target}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ Это автоматический расчёт — проверь объём и контекст\n"
        f"🔗 [Открыть на Bybit]({link})"
    )
    await _send(text)


# ──────────────────────────────────────────────────────────────────────────────
#  ВЫХОД ИЗ БОКОВИКА
# ──────────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────────
#  ВЫХОД ИЗ БОКОВИКА
# ──────────────────────────────────────────────────────────────────────────────

async def send_exit_alert(symbol: str, tf: str, stats: dict, duration_h: float = 0):
    """⚠️ Стандартный алерт о выходе из боковика."""
    tf_label = TF_LABELS.get(tf, tf)
    link     = BYBIT_URL.format(symbol=symbol)
    profit   = stats.get("profit", {})
    earned   = profit.get("net_profit_usdt", 0)
    sign     = "+" if earned >= 0 else ""
    earned_s = f"`{sign}${earned:.2f}` (при $1000)" if earned else "н/д"

    text = (
        f"⚠️ *ВЫХОД ИЗ БОКОВИКА · {symbol}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⏱ `{tf_label}` · 💲 `{stats['price']}`\n"
        f"⏳ Держался: `{duration_h}ч` · Скор: `{stats.get('score',0)}/10`\n"
        f"💰 Расч. прибыль: {earned_s}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🛑 *Закрой Grid Bot!*\n"
        f"🔗 [Открыть на Bybit]({link})"
    )
    await _send(text)


async def send_portfolio_stop_alert(trade: dict, status: str, current_price: float):
    """🚨 Персональный алерт когда цена близко к стопу или пробила его."""
    symbol   = trade["symbol"]
    tf       = trade["tf"]
    tf_label = TF_LABELS.get(tf, tf)
    link     = BYBIT_URL.format(symbol=symbol)
    deposit  = trade.get("deposit", 0)
    profit   = trade.get("profit_est", 0)
    sign     = "+" if profit >= 0 else ""

    if status == "stop_low":
        header = f"🚨 *СТОП СРАБОТАЛ · {symbol}*"
        body   = (
            f"Цена пробила нижний стоп!\n"
            f"Стоп: `{trade['stop_loss']}` · Цена: `{current_price}`\n"
            f"🛑 *Немедленно закрой Grid Bot!*"
        )
    elif status == "stop_high":
        header = f"🚀 *ВЕРХНИЙ СТОП · {symbol}*"
        body   = (
            f"Цена пробила верхний стоп!\n"
            f"Стоп: `{trade['stop_high']}` · Цена: `{current_price}`\n"
            f"✅ Закрой бота — зафиксируй прибыль"
        )
    elif status == "near_stop_low":
        header = f"⚠️ *БЛИЗКО К СТОПУ · {symbol}*"
        body   = (
            f"Цена опасно близко к нижнему стопу!\n"
            f"Стоп: `{trade['stop_loss']}` · Цена: `{current_price}`\n"
            f"Расстояние: `{round(abs(current_price - trade['stop_loss']) / current_price * 100, 2)}%`\n"
            f"👀 Следи за ситуацией"
        )
    else:
        return

    elapsed_h = round((time.time() - trade["since"]) / 3600, 1)

    text = (
        f"{header}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⏱ `{tf_label}` · 💲 `{current_price}`\n"
        f"⏳ В работе: `{elapsed_h}ч` · Депозит: `${deposit}`\n"
        f"💰 Расч. прибыль: `{sign}${profit:.2f}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{body}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📐 Диапазон: `{trade['range_low']}` — `{trade['range_high']}`\n"
        f"🔗 [Открыть на Bybit]({link})"
    )
    await _send(text)


async def send_portfolio_exit_alert(trade: dict, duration_h: float, reason: str):
    """⚠️ Алерт о выходе из боковика по сделке из портфеля."""
    symbol   = trade["symbol"]
    tf_label = TF_LABELS.get(trade["tf"], trade["tf"])
    link     = BYBIT_URL.format(symbol=symbol)
    profit   = trade.get("profit_est", 0)
    sign     = "+" if profit >= 0 else ""

    text = (
        f"⚠️ *ТВОЯ СДЕЛКА · {symbol}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⏱ `{tf_label}` · Депозит: `${trade.get('deposit',0)}`\n"
        f"⏳ Держалась: `{duration_h}ч`\n"
        f"💰 Расч. прибыль: `{sign}${profit:.2f}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🛑 *{reason}*\n"
        f"Закрой Grid Bot на Bybit!\n"
        f"🔗 [Открыть на Bybit]({link})"
    )
    await _send(text)
    tf_label = TF_LABELS.get(tf, tf)
    link     = BYBIT_URL.format(symbol=symbol)
    profit   = stats.get("profit", {})
    earned   = profit.get("net_profit_usdt", 0)
    sign     = "+" if earned >= 0 else ""
    earned_s = f"`{sign}${earned:.2f}` (при $1000)" if earned else "н/д"

    text = (
        f"⚠️ *ВЫХОД ИЗ БОКОВИКА · {symbol}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⏱ `{tf_label}` · 💲 `{stats['price']}`\n"
        f"⏳ Держался: `{duration_h}ч` · Скор: `{stats.get('score',0)}/10`\n"
        f"💰 Расчётная прибыль: {earned_s}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🛑 *Закрой Grid Bot!*\n"
        f"🔗 [Открыть на Bybit]({link})"
    )
    await _send(text)


# ──────────────────────────────────────────────────────────────────────────────
#  ДНЕВНОЙ ОТЧЁТ
# ──────────────────────────────────────────────────────────────────────────────

async def send_daily_report(stats: dict, active_count: int):
    found   = stats.get("found", 0)
    exits   = stats.get("exits", 0)
    breakouts = stats.get("breakouts", 0)
    skipped = stats.get("skipped", 0)
    top     = stats.get("top", [])

    top_lines = ""
    for i, item in enumerate(top, 1):
        mode_icon = "🤖" if item.get("mode","grid") == "grid" else "⚡"
        tf_label  = TF_LABELS.get(item["tf"], item["tf"])
        top_lines += f"  {i}. {mode_icon} `{item['symbol']}` [{tf_label}] — {item['score']}/10\n"
    if not top_lines:
        top_lines = "  Нет сигналов\n"

    retry_info = f"📭 Retry: `{len(_retry_queue)}`\n" if _retry_queue else ""

    text = (
        f"📅 *Дневной отчёт*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🤖 Grid: `{found}` · ⚡ Breakout: `{breakouts}`\n"
        f"📤 Выходов: `{exits}` · 👁 Активных: `{active_count}`\n"
        f"⏭ Пропущено API: `{skipped}`\n"
        f"{retry_info}"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🏆 *Топ-5 за сутки:*\n{top_lines}"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🤖 Bybit Flat Scanner v9"
    )
    await _send(text)
