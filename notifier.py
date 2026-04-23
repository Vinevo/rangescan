"""
notifier.py — Отправка сообщений в Telegram.

Улучшения v5:
  - Очередь повторной отправки при сбое сети
  - Блок funding rate в каждом сигнале
  - Блок расчёта прибыли Grid Bot
  - Статус funding в дневном отчёте
"""

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

# Очередь сообщений для повторной отправки при сбое
_retry_queue: list[dict] = []
MAX_RETRIES = 3


async def _send_raw(text: str) -> bool:
    """Базовая отправка. Возвращает True при успехе."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Не заданы TELEGRAM_TOKEN или TELEGRAM_CHAT_ID")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json={
                "chat_id":                  TELEGRAM_CHAT_ID,
                "text":                     text,
                "parse_mode":               "Markdown",
                "disable_web_page_preview": True,
            }, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return True
                body = await r.text()
                logger.error(f"Telegram {r.status}: {body[:200]}")
                return False
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")
        return False


async def _send(text: str):
    """
    Отправка с авто-ретраем.
    При неудаче добавляет в очередь — flush_retry_queue() пробует снова.
    """
    ok = await _send_raw(text)
    if not ok:
        _retry_queue.append({"text": text, "attempts": 1})
        logger.warning(f"Сообщение добавлено в очередь повторной отправки (очередь: {len(_retry_queue)})")


async def flush_retry_queue():
    """
    Пробует повторно отправить сообщения из очереди.
    Вызывается из main.py раз в несколько минут.
    """
    if not _retry_queue:
        return
    logger.info(f"🔄 Retry queue: {len(_retry_queue)} сообщений")
    still_pending = []
    for item in _retry_queue:
        ok = await _send_raw(item["text"])
        if ok:
            logger.info("✅ Отложенное сообщение отправлено")
        else:
            item["attempts"] += 1
            if item["attempts"] < MAX_RETRIES:
                still_pending.append(item)
            else:
                logger.error(f"❌ Сообщение удалено после {MAX_RETRIES} попыток")
    _retry_queue.clear()
    _retry_queue.extend(still_pending)


# ──────────────────────────────────────────────────────────────────────────────
#  УТИЛИТЫ ФОРМАТИРОВАНИЯ
# ──────────────────────────────────────────────────────────────────────────────

def _score_bar(score: int) -> str:
    return f"{'█' * score}{'░' * (10 - score)} {score}/10"


def _vol_fmt(vol: float) -> str:
    if vol >= 1_000_000_000:
        return f"${vol/1_000_000_000:.1f}B"
    if vol >= 1_000_000:
        return f"${vol/1_000_000:.1f}M"
    return f"${vol/1_000:.0f}K"


def _sr_block(sr: dict) -> str:
    lines = []
    if sr.get("sandwiched"):
        lines.append("🏆 *Зажат между уровнями — идеал для Grid!*")
    res = sr.get("resistance_above")
    if res and sr.get("has_resistance"):
        lines.append(
            f"🔴 Сопротивление: `{res['price']}` "
            f"(+{sr.get('resistance_dist_pct', 0)}%, касаний: {res['touches']})"
        )
    sup = sr.get("support_below")
    if sup and sr.get("has_support"):
        lines.append(
            f"🟢 Поддержка: `{sup['price']}` "
            f"(-{sr.get('support_dist_pct', 0)}%, касаний: {sup['touches']})"
        )
    if not lines:
        lines.append("⚪ Значимых уровней рядом нет")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
#  СИГНАЛЫ
# ──────────────────────────────────────────────────────────────────────────────

async def send_signal(symbol: str, tf: str, stats: dict):
    """Полное уведомление о новом боковике."""
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
    link        = BYBIT_URL.format(symbol=symbol)

    # Заголовок
    if score >= 8:
        badge = "🔥 *СИЛЬНЫЙ СИГНАЛ*"
    elif score >= 5:
        badge = "✅ *Хороший сигнал*"
    else:
        badge = "📊 *Слабый сигнал*"

    rsi_icon = "✅" if rsi_flat else "⚠️"
    vol_warn = "\n⚠️ *Объём растёт* — возможен скорый пробой!" if vol_growing else ""

    # Прибыль
    profit_text = format_profit_block(profit) if profit else ""

    text = (
        f"{badge}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🪙 *{symbol}* | ⏱ `{tf_label}` | 💲 `{price}`\n"
        f"📊 Скор: `{_score_bar(score)}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📉 ADX `{stats['adx']}` ✅  "
        f"📏 BB `{stats['bb_width_pct']}%` ✅  "
        f"🌊 ATR `{stats['atr_pct']}%` ✅\n"
        f"📈 RSI `{rsi_val}` {rsi_icon}  "
        f"🕯 `{stats['flat_candles']}` свечей  "
        f"🎯 пробоев: `{stats['false_breaks']}`\n"
        f"💹 Объём 24ч: `{_vol_fmt(vol24h)}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📌 *Уровни S/R:*\n{_sr_block(sr)}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{funding.get('comment', '')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🤖 *Grid Bot:*\n"
        f"   ↓ `{stats['range_low']}` → ↑ `{stats['range_high']}`\n"
        f"   📐 `{stats['range_pct']}%` | # `{stats['grid_count']}` сеток | шаг `{stats['grid_step']}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{profit_text}"
        f"{vol_warn}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔗 [Открыть на Bybit]({link})"
    )
    await _send(text)


async def send_exit_alert(symbol: str, tf: str, stats: dict, duration_h: float = 0):
    """Уведомление о выходе из боковика."""
    tf_label = TF_LABELS.get(tf, tf)
    link     = BYBIT_URL.format(symbol=symbol)

    profit = stats.get("profit", {})
    earned = profit.get("net_profit_usdt", 0)
    earned_str = f"`{earned:+.2f}$ (при $1000 депозите)`" if earned else "н/д"

    text = (
        f"⚠️ *ВЫХОД ИЗ БОКОВИКА*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🪙 *{symbol}* | ⏱ `{tf_label}` | 💲 `{stats['price']}`\n"
        f"⏳ Держался: `{duration_h}ч`\n"
        f"📊 Скор был: `{stats.get('score', 0)}/10`\n"
        f"💰 Расчётная прибыль: {earned_str}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🛑 *Закрой Grid Bot!*\n"
        f"ADX > 25 или цена пробила диапазон.\n"
        f"🔗 [Открыть на Bybit]({link})"
    )
    await _send(text)


async def send_daily_report(stats: dict, active_count: int):
    """Ежедневный отчёт в 09:00 UTC."""
    found   = stats.get("found", 0)
    exits   = stats.get("exits", 0)
    skipped = stats.get("skipped", 0)
    top     = stats.get("top", [])

    top_lines = ""
    for i, item in enumerate(top, 1):
        tf_label = TF_LABELS.get(item["tf"], item["tf"])
        top_lines += f"   {i}. `{item['symbol']}` [{tf_label}] — {item['score']}/10\n"
    if not top_lines:
        top_lines = "   Нет сигналов\n"

    retry_info = f"📭 Очередь retry: `{len(_retry_queue)}`\n" if _retry_queue else ""

    text = (
        f"📅 *Дневной отчёт*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🆕 Новых сигналов: `{found}`\n"
        f"📤 Выходов: `{exits}`\n"
        f"👁 Активных сейчас: `{active_count}`\n"
        f"⏭ Пропущено (API): `{skipped}`\n"
        f"{retry_info}"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🏆 *Топ-5 за сутки:*\n{top_lines}"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🤖 Bybit Flat Scanner v5"
    )
    await _send(text)
