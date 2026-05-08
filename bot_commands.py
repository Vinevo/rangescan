import asyncio
import logging
import os
import time
import aiohttp

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TF_LABELS        = {"30": "30м", "60": "1ч", "240": "4ч", "D": "1д"}

_paused         = False
_last_update_id = 0
_active_flats_ref: dict | None = None
_daily_stats_ref:  dict | None = None
_bot_start_time = time.time()


def set_refs(active_flats: dict, daily_stats: dict):
    global _active_flats_ref, _daily_stats_ref
    _active_flats_ref = active_flats
    _daily_stats_ref  = daily_stats


def is_paused() -> bool:
    return _paused


async def _send(chat_id, text: str):
    if not TELEGRAM_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(url, json={"chat_id": chat_id, "text": text,
                                    "parse_mode": "Markdown",
                                    "disable_web_page_preview": True},
                         timeout=aiohttp.ClientTimeout(total=10))
    except Exception as e:
        logger.debug(f"cmd send: {e}")


async def _get_updates() -> list[dict]:
    global _last_update_id
    if not TELEGRAM_TOKEN:
        return []
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params={"offset": _last_update_id + 1,
                                          "timeout": 5, "limit": 10},
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json()
                return data.get("result", [])
    except Exception:
        return []


def _uptime() -> str:
    secs = int(time.time() - _bot_start_time)
    h, m = divmod(secs // 60, 60)
    return f"{h}ч {m}м"


async def _cmd_status(chat_id):
    active = len(_active_flats_ref) if _active_flats_ref else 0
    found  = _daily_stats_ref.get("found", 0) if _daily_stats_ref else 0
    exits  = _daily_stats_ref.get("exits", 0) if _daily_stats_ref else 0
    status = "⏸ *Пауза*" if _paused else "✅ *Работает*"
    await _send(chat_id,
        f"🤖 *Статус бота*\n━━━━━━━━━━━━━━━━━━\n"
        f"Состояние: {status}\n⏱ Аптайм: `{_uptime()}`\n"
        f"👁 Активных боковиков: `{active}`\n━━━━━━━━━━━━━━━━━━\n"
        f"📊 *За сегодня:*\n🆕 Новых: `{found}`\n📤 Выходов: `{exits}`\n"
        f"━━━━━━━━━━━━━━━━━━\nКоманды: /active /pause /resume /help")


async def _cmd_active(chat_id):
    if not _active_flats_ref:
        await _send(chat_id, "👁 Активных боковиков нет.")
        return
    lines = [f"👁 *Активные боковики* ({len(_active_flats_ref)}):\n"]
    now   = time.time()
    items = sorted(_active_flats_ref.items(),
                   key=lambda x: x[1].get("score", 0), reverse=True)
    for key, stats in items[:15]:
        sym, tf  = key.rsplit("_", 1)
        age_h    = round((now - stats.get("since", now)) / 3600, 1)
        score    = stats.get("score", 0)
        mode     = stats.get("mode", "grid")
        mode_icon = "🤖" if mode == "grid" else "⚡"
        f_icon   = "🟢" if stats.get("funding", {}).get("is_safe", True) else "🔴"
        lines.append(f"{mode_icon} `{sym}` [{TF_LABELS.get(tf,tf)}] "
                     f"скор {score}/10 | {age_h}ч {f_icon}")
    if len(_active_flats_ref) > 15:
        lines.append(f"_...ещё {len(_active_flats_ref)-15}_")
    await _send(chat_id, "\n".join(lines))


async def _cmd_pause(chat_id):
    global _paused
    _paused = True
    await _send(chat_id, "⏸ *Сканирование приостановлено.*\n/resume — возобновить")
    logger.info("⏸ Пауза через Telegram")


async def _cmd_resume(chat_id):
    global _paused
    _paused = False
    await _send(chat_id, "▶️ *Сканирование возобновлено.*")
    logger.info("▶️ Возобновлено через Telegram")


async def _cmd_help(chat_id):
    await _send(chat_id,
        "🤖 *Bybit Flat Scanner — Команды*\n━━━━━━━━━━━━━━━━━━\n"
        "/status  — состояние и статистика\n"
        "/active  — активные боковики\n"
        "/pause   — остановить сигналы\n"
        "/resume  — возобновить\n"
        "/help    — справка")


async def _handle(update: dict):
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat_id = str(msg["chat"]["id"])
    text    = msg.get("text", "").strip()
    if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
        return
    cmd = text.split()[0].lower().split("@")[0] if text else ""
    if cmd == "/status":      await _cmd_status(chat_id)
    elif cmd == "/active":    await _cmd_active(chat_id)
    elif cmd == "/pause":     await _cmd_pause(chat_id)
    elif cmd == "/resume":    await _cmd_resume(chat_id)
    elif cmd in ("/help", "/start"): await _cmd_help(chat_id)
    elif text.startswith("/"): await _send(chat_id, "❓ /help — список команд")


async def poll_commands():
    global _last_update_id
    logger.info("🎮 Telegram command polling запущен")
    while True:
        try:
            updates = await _get_updates()
            for upd in updates:
                _last_update_id = max(_last_update_id, upd.get("update_id", 0))
                await _handle(upd)
        except Exception as e:
            logger.debug(f"poll: {e}")
        await asyncio.sleep(3)
