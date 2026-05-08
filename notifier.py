import os
import asyncio
import logging
import aiohttp
from profit import format_profit_block

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TF_LABELS        = {"30": "30м", "60": "1ч", "240": "4ч", "D": "1д"}
BYBIT_URL        = "https://www.bybit.com/trade/usdt/{symbol}"

_retry_queue: list[dict] = []
MAX_RETRIES = 3


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
        logger.warning(f"Telegram error: {e}")
        return False


async def _send(text: str):
    if not await _send_raw(text):
        _retry_queue.append({"text": text, "attempts": 1})


async def flush_retry_queue():
    if not _retry_queue: return
    pending = []
    for item in _retry_queue:
        if await _send_raw(item["text"]):
            logger.info("✅ Retry message sent")
        else:
            item["attempts"] += 1
            if item["attempts"] < MAX_RETRIES:
                pending.append(item)
    _retry_queue.clear()
    _retry_queue.extend(pending)


def _score_bar(score: int) -> str:
    return f"{'█'*score}{'░'*(10-score)} {score}/10"


def _vol_fmt(vol: float) -> str:
    if vol >= 1e9: return f"${vol/1e9:.1f}B"
    if vol >= 1e6: return f"${vol/1e6:.1f}M"
    return f"${vol/1e3:.0f}K"


def _sr_block(sr: dict) -> str:
    lines = []
    if sr.get("sandwiched"):
        lines.append("🏆 *Зажат между уровнями — идеал для Grid!*")
    res = sr.get("resistance_above")
    if res and sr.get("has_resistance"):
        lines.append(f"🔴 Сопротивление: `{res['price']}` (+{sr.get('resistance_dist_pct',0)}%, {res['touches']} кас.)")
    sup = sr.get("support_below")
    if sup and sr.get("has_support"):
        lines.append(f"🟢 Поддержка: `{sup['price']}` (-{sr.get('support_dist_pct',0)}%, {sup['touches']} кас.)")
    if not lines:
        lines.append("⚪ Значимых уровней рядом нет")
    return "\n".join(lines)


def _smart_block(smart: dict) -> str:
    if not smart: return ""

    ms      = smart.get("market_structure", "range")
    rs      = smart.get("range_score", 0)
    br      = smart.get("breakout_risk", 0)
    rec     = smart.get("recommendation", "grid")
    pos     = smart.get("price_position", {})
    imp     = smart.get("impulse", {})
    vol_acc = smart.get("volume_acc", {})
    rng     = smart.get("range_info", {})
    fvg     = smart.get("fvg", {})
    session = smart.get("session", {})
    disp    = smart.get("displacement", {})
    pd_z    = smart.get("premium_discount", {})
    sweep   = smart.get("sweep", {})
    wo      = smart.get("weekly_open", {})
    blocked = smart.get("grid_blocked", [])

    ms_icons = {"uptrend":"📈 Аптренд","downtrend":"📉 Даунтренд","range":"↔️ Флет"}
    pos_icons = {"top":"🔝 Верх","middle":"➡️ Середина","bottom":"⬇️ Низ"}
    pd_icons  = {"premium":"💎 Premium","discount":"🏷 Discount","equilibrium":"⚖️ Равновесие"}
    rec_labels = {"grid":"🤖 GRID BOT — запускай","breakout":"⚡ ЖДАТЬ ПРОБОЙ","wait":"⏸ ЖДАТЬ"}

    lines = [
        f"🧠 *Smart Money:*",
        f"   {ms_icons.get(ms,ms)} | {pd_icons.get(pd_z.get('zone','equilibrium'),'⚖️')} ({pd_z.get('pct',50):.0f}%)",
        f"   Позиция: {pos_icons.get(pos.get('position','middle'),'➡️')} ({pos.get('position_pct',50):.0f}%)",
        f"   🟢 Range: `{rs}/10`  🔴 Breakout Risk: `{br}/10`",
    ]

    # Сессия
    sc = session.get("comment","")
    if sc and session.get("current_session") not in ("any",""):
        lines.append(f"   🕐 {sc}")

    # Weekly Open
    if wo.get("comment"):
        lines.append(f"   📅 {wo['comment']}")

    # FVG
    if fvg.get("danger_above") and fvg.get("nearest_bear"):
        nb = fvg["nearest_bear"]
        lines.append(f"   🔴 FVG сверху: `{nb['mid']}` ({nb['size_pct']}%, {nb['age']} св.)")
    if fvg.get("danger_below") and fvg.get("nearest_bull"):
        nb = fvg["nearest_bull"]
        lines.append(f"   🟢 FVG снизу: `{nb['mid']}` ({nb['size_pct']}%, {nb['age']} св.)")
    if not fvg.get("danger_above") and not fvg.get("danger_below"):
        lines.append(f"   ✅ FVG: нет опасных гэпов")

    # Sweep
    if sweep.get("has_sweep"):
        sw_l  = "🟢 Бычий sweep" if sweep["sweep_type"]=="bull_sweep" else "🔴 Медвежий sweep"
        sw_d  = "↑ ожидаем рост" if sweep["direction"]=="up" else "↓ ожидаем падение"
        fresh = " ⚠️ СВЕЖИЙ!" if sweep["is_fresh"] else ""
        lines.append(f"   💧 {sw_l}: `{sweep['swept_level']}` {sweep['candles_ago']} св. | {sw_d}{fresh}")

    # Displacement
    if disp.get("has_displacement"):
        dd = "🟢 бычий" if disp["direction"]=="bull" else "🔴 медвежий"
        lines.append(f"   ⚡ Displacement {dd} ×{disp['size_mult']} — {disp['candles_ago']} св. назад")

    # Импульс
    if imp.get("has_impulse"):
        lines.append(f"   💥 Импульс: {imp['last_impulse_ago']} св. назад (×{imp['max_candle_mult']})")

    # Накопление
    if vol_acc.get("is_accumulation"):
        lines.append(f"   📦 Накопление: объём ×{vol_acc['vol_ratio']} при {vol_acc['price_range_pct']}%")

    # Диапазон
    if rng.get("is_valid"):
        lines.append(f"   📐 `{rng['range_low']}–{rng['range_high']}` ({rng['width_pct']}%, ↑{rng['top_touches']} ↓{rng['bot_touches']})")

    # Блокировки
    if blocked:
        lines.append(f"   🚫 *Стоп:* {' | '.join(blocked)}")

    lines.append(f"   👉 *{rec_labels.get(rec, rec)}*")
    return "\n".join(lines)


async def send_signal(symbol: str, tf: str, stats: dict):
    tf_label    = TF_LABELS.get(tf, tf)
    price       = stats["price"]
    score       = stats["score"]
    rsi_val     = stats.get("rsi", 50)
    rsi_flat    = stats.get("rsi_flat", False)
    vol_growing = stats.get("vol_growing", False)
    vol24h      = stats.get("volume24h", 0)
    sr          = stats.get("sr", {})
    funding     = stats.get("funding", {})
    profit      = stats.get("profit", {})
    smart       = stats.get("smart", {})
    mode        = stats.get("mode", "grid")
    link        = BYBIT_URL.format(symbol=symbol)

    if mode == "breakout":
        badge = "⚡ *СИГНАЛ: ЖДАТЬ ПРОБОЙ*"
    elif score >= 8:
        badge = "🔥 *GRID: СИЛЬНЫЙ СИГНАЛ*"
    elif score >= 5:
        badge = "✅ *GRID: Хороший сигнал*"
    else:
        badge = "📊 *GRID: Слабый сигнал*"

    rsi_icon = "✅" if rsi_flat else "⚠️"
    vol_warn = "\n⚠️ *Объём растёт* — возможен пробой!" if vol_growing else ""
    profit_text = format_profit_block(profit) if profit and mode == "grid" else ""
    smart_text  = _smart_block(smart)

    if mode == "grid":
        grid_block = (
            f"━━━━━━━━━━━━━━━━━━\n🤖 *Grid Bot:*\n"
            f"   ↓ `{stats['range_low']}` → ↑ `{stats['range_high']}`\n"
            f"   📐 `{stats['range_pct']}%` | #{stats['grid_count']} сеток | шаг `{stats['grid_step']}`\n"
            f"━━━━━━━━━━━━━━━━━━\n{profit_text}"
        )
    else:
        rng = smart.get("range_info", {})
        grid_block = (
            f"━━━━━━━━━━━━━━━━━━\n⚡ *Ждать пробой:*\n"
            f"   Верх: `{rng.get('range_high','?')}`\n"
            f"   Низ:  `{rng.get('range_low','?')}`\n"
            f"   Войти после закрытия свечи ЗА границей\n"
        )

    text = (
        f"{badge}\n━━━━━━━━━━━━━━━━━━\n"
        f"🪙 *{symbol}* | ⏱ `{tf_label}` | 💲 `{price}`\n"
        f"📊 Скор: `{_score_bar(score)}`\n━━━━━━━━━━━━━━━━━━\n"
        f"📉 ADX `{stats['adx']}` ✅  📏 BB `{stats['bb_width_pct']}%` ✅  🌊 ATR `{stats['atr_pct']}%` ✅\n"
        f"📈 RSI `{rsi_val}` {rsi_icon}  🕯 `{stats['flat_candles']}` св.  🎯 пробоев: `{stats['false_breaks']}`\n"
        f"💹 Объём 24ч: `{_vol_fmt(vol24h)}`\n━━━━━━━━━━━━━━━━━━\n"
        f"📌 *Уровни S/R:*\n{_sr_block(sr)}\n━━━━━━━━━━━━━━━━━━\n"
        f"{smart_text}\n━━━━━━━━━━━━━━━━━━\n"
        f"{funding.get('comment','')}\n━━━━━━━━━━━━━━━━━━\n"
        f"{grid_block}"
        f"{vol_warn}\n━━━━━━━━━━━━━━━━━━\n"
        f"🔗 [Открыть на Bybit]({link})"
    )
    await _send(text)


async def send_exit_alert(symbol: str, tf: str, stats: dict, duration_h: float = 0):
    tf_label = TF_LABELS.get(tf, tf)
    link     = BYBIT_URL.format(symbol=symbol)
    profit   = stats.get("profit", {})
    earned   = profit.get("net_profit_usdt", 0)
    earned_s = f"`{earned:+.2f}$ (при $1000)`" if earned else "н/д"
    await _send(
        f"⚠️ *ВЫХОД ИЗ БОКОВИКА*\n━━━━━━━━━━━━━━━━━━\n"
        f"🪙 *{symbol}* | ⏱ `{tf_label}` | 💲 `{stats['price']}`\n"
        f"⏳ Держался: `{duration_h}ч` | Скор был: `{stats.get('score',0)}/10`\n"
        f"💰 Расчётная прибыль: {earned_s}\n━━━━━━━━━━━━━━━━━━\n"
        f"🛑 *Закрой Grid Bot!*\n🔗 [Открыть на Bybit]({link})"
    )


async def send_daily_report(stats: dict, active_count: int):
    found   = stats.get("found", 0)
    exits   = stats.get("exits", 0)
    skipped = stats.get("skipped", 0)
    top     = stats.get("top", [])
    top_lines = ""
    for i, item in enumerate(top, 1):
        top_lines += f"   {i}. `{item['symbol']}` [{TF_LABELS.get(item['tf'],item['tf'])}] — {item['score']}/10\n"
    if not top_lines: top_lines = "   Нет сигналов\n"
    retry_info = f"📭 Retry очередь: `{len(_retry_queue)}`\n" if _retry_queue else ""
    await _send(
        f"📅 *Дневной отчёт*\n━━━━━━━━━━━━━━━━━━\n"
        f"🆕 Новых: `{found}` | 📤 Выходов: `{exits}`\n"
        f"👁 Активных: `{active_count}` | ⏭ Пропущено: `{skipped}`\n"
        f"{retry_info}━━━━━━━━━━━━━━━━━━\n"
        f"🏆 *Топ-5:*\n{top_lines}"
        f"━━━━━━━━━━━━━━━━━━\n🤖 Bybit Flat Scanner v9"
    )
