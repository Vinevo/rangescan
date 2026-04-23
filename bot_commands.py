"""
bot_commands.py — Обработчик команд Telegram бота.

Команды:
    /status   — статус бота: работает ли, сколько активных боковиков
    /active   — список всех активных боковиков прямо сейчас
    /pause    — приостановить сканирование (сигналы не отправляются)
    /resume   — возобновить сканирование
    /help     — список команд

Работает через long polling (getUpdates).
Запускается в отдельном asyncio task в main.py.
"""

import asyncio
import logging
import os
import time
import aiohttp

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TF_LABELS        = {"30": "30м", "60": "1ч", "240": "4ч", "D": "1д"}

# Глобальный флаг паузы — читается в scanner.py
_paused     = False
_last_update_id = 0

# Ссылки на данные сканера (устанавливаются из main.py)
_active_flats_ref: dict | None = None
_daily_stats_ref:  dict | None = None
_bot_start_time = time.time()


def set_refs(active_flats: dict, daily_stats: dict):
    """Передаём ссылки на данные сканера."""
    global _active_flats_ref, _daily_stats_ref
    _active_flats_ref = active_flats
    _daily_stats_ref  = daily_stats


def is_paused() -> bool:
    return _paused


async def _send(chat_id: str | int, text: str):
    if not TELEGRAM_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(url, json={
                "chat_id":    chat_id,
                "text":       text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }, timeout=aiohttp.ClientTimeout(total=10))
    except Exception as e:
        logger.debug(f"_send cmd: {e}")


async def _get_updates() -> list[dict]:
    global _last_update_id
    if not TELEGRAM_TOKEN:
        return []
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params={
                "offset":  _last_update_id + 1,
                "timeout": 5,
                "limit":   10,
            }, timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json()
                return data.get("result", [])
    except Exception:
        return []


def _uptime_str() -> str:
    secs = int(time.time() - _bot_start_time)
    h, m = divmod(secs // 60, 60)
    return f"{h}ч {m}м"


async def _cmd_status(chat_id):
    active = len(_active_flats_ref) if _active_flats_ref else 0
    found  = _daily_stats_ref.get("found", 0) if _daily_stats_ref else 0
    exits  = _daily_stats_ref.get("exits", 0) if _daily_stats_ref else 0
    status = "⏸ *Пауза*" if _paused else "✅ *Работает*"

    text = (
        f"🤖 *Статус бота*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Состояние: {status}\n"
        f"⏱ Аптайм: `{_uptime_str()}`\n"
        f"👁 Активных боковиков: `{active}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 *За сегодня:*\n"
        f"   🆕 Новых сигналов: `{found}`\n"
        f"   📤 Выходов: `{exits}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Команды: /active /pause /resume /help"
    )
    await _send(chat_id, text)


async def _cmd_active(chat_id):
    if not _active_flats_ref:
        await _send(chat_id, "👁 Активных боковиков нет.")
        return

    if not _active_flats_ref:
        await _send(chat_id, "👁 Активных боковиков нет.")
        return

    lines = [f"👁 *Активные боковики* ({len(_active_flats_ref)}):\n"]
    now   = time.time()

    # Сортируем по скору
    items = sorted(
        _active_flats_ref.items(),
        key=lambda x: x[1].get("score", 0),
        reverse=True
    )

    for key, stats in items[:15]:   # Максимум 15 чтобы не спамить
        sym, tf = key.rsplit("_", 1)
        tf_label  = TF_LABELS.get(tf, tf)
        score     = stats.get("score", 0)
        since     = stats.get("since", now)
        age_h     = round((now - since) / 3600, 1)
        range_pct = stats.get("range_pct", 0)
        funding   = stats.get("funding", {})
        f_icon    = "🟢" if funding.get("is_safe", True) else "🔴"

        lines.append(
            f"`{sym}` [{tf_label}] "
            f"скор {score}/10 | "
            f"{range_pct}% диап | "
            f"{age_h}ч назад {f_icon}"
        )

    if len(_active_flats_ref) > 15:
        lines.append(f"_...ещё {len(_active_flats_ref) - 15}_")

    await _send(chat_id, "\n".join(lines))


async def _cmd_pause(chat_id):
    global _paused
    _paused = True
    await _send(chat_id,
        "⏸ *Сканирование приостановлено.*\n"
        "Новые сигналы отправляться не будут.\n"
        "Для возобновления: /resume"
    )
    logger.info("⏸ Бот поставлен на паузу через Telegram")


async def _cmd_resume(chat_id):
    global _paused
    _paused = False
    await _send(chat_id,
        "▶️ *Сканирование возобновлено.*\n"
        "Следующий скан через ≤5 минут."
    )
    logger.info("▶️ Бот возобновлён через Telegram")


async def _cmd_help(chat_id):
    text = (
        "🤖 *Bybit Flat Scanner — Команды*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "/status  — состояние бота и статистика\n"
        "/active  — все активные боковики прямо сейчас\n"
        "/pause   — приостановить сигналы\n"
        "/resume  — возобновить сигналы\n"
        "/help    — эта справка\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "_Команды работают только от твоего chat\\_id_"
    )
    await _send(chat_id, text)


async def _handle_update(update: dict):
    """Обрабатывает одно обновление от Telegram."""
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return

    chat_id = str(msg["chat"]["id"])
    text    = msg.get("text", "").strip()

    # Принимаем команды только от владельца
    if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
        logger.warning(f"Команда от чужого chat_id={chat_id}, игнорируем")
        return

    cmd = text.split()[0].lower().split("@")[0] if text else ""

    if cmd == "/status":
        await _cmd_status(chat_id)
    elif cmd == "/active":
        await _cmd_active(chat_id)
    elif cmd == "/pause":
        await _cmd_pause(chat_id)
    elif cmd == "/resume":
        await _cmd_resume(chat_id)
    elif cmd in ("/help", "/start"):
        await _cmd_help(chat_id)
    else:
        if text.startswith("/"):
            await _send(chat_id, "❓ Неизвестная команда. /help — список команд.")


async def poll_commands():
    """
    Бесконечный цикл polling обновлений от Telegram.
    Запускать как asyncio.create_task(poll_commands()).
    """
    global _last_update_id
    logger.info("🎮 Telegram command polling запущен")

    while True:
        try:
            updates = await _get_updates()
            for upd in updates:
                _last_update_id = max(_last_update_id, upd.get("update_id", 0))
                await _handle_update(upd)
        except Exception as e:
            logger.debug(f"poll_commands error: {e}")

        await asyncio.sleep(3)   # Проверяем каждые 3 секунды
