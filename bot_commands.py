"""
bot_commands.py — Telegram команды с inline-кнопками.

Команды:
    /start   — приветствие + меню кнопок
    /menu    — показать меню кнопок
    /status  — статус бота
    /active  — активные боковики (Grid и Breakout отдельно)
    /stats   — статистика сигналов за всё время
    /pause   — пауза
    /resume  — возобновить
    /help    — справка

Inline кнопки:
    [📊 Статус]  [👁 Активные]  [📈 Статистика]
    [⏸ Пауза / ▶️ Продолжить]  [❓ Помощь]
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

_paused         = False
_last_update_id = 0
_active_flats_ref: dict | None = None
_daily_stats_ref:  dict | None = None
_bot_start_time = time.time()

# Полная статистика за всё время работы
_all_time_stats = {
    "grid_signals":     0,   # всего Grid сигналов
    "breakout_signals": 0,   # всего Breakout сигналов
    "confirmed_breakouts": 0, # пробоев подтверждено
    "exits_total":      0,   # всего выходов
    "best_signal":      None, # лучший сигнал {"symbol","tf","score","mode"}
    "started_at":       time.time(),
}


def set_refs(active_flats: dict, daily_stats: dict):
    global _active_flats_ref, _daily_stats_ref
    _active_flats_ref = active_flats
    _daily_stats_ref  = daily_stats


def is_paused() -> bool:
    return _paused


def record_signal(symbol: str, tf: str, score: int, mode: str):
    """Вызывается из scanner.py при каждом новом сигнале."""
    if mode == "grid":
        _all_time_stats["grid_signals"] += 1
    else:
        _all_time_stats["breakout_signals"] += 1

    best = _all_time_stats["best_signal"]
    if best is None or score > best["score"]:
        _all_time_stats["best_signal"] = {
            "symbol": symbol, "tf": tf,
            "score": score, "mode": mode
        }


def record_exit():
    _all_time_stats["exits_total"] += 1


def record_breakout():
    _all_time_stats["confirmed_breakouts"] += 1


# ──────────────────────────────────────────────────────────────────────────────
#  HTTP УТИЛИТЫ
# ──────────────────────────────────────────────────────────────────────────────

async def _post(url: str, payload: dict):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=payload,
                              timeout=aiohttp.ClientTimeout(total=10)) as r:
                return await r.json()
    except Exception as e:
        logger.debug(f"_post: {e}")
        return {}


async def _send(chat_id, text: str, reply_markup: dict = None):
    """Отправка сообщения с опциональной клавиатурой."""
    if not TELEGRAM_TOKEN:
        return
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id":                  chat_id,
        "text":                     text,
        "parse_mode":               "Markdown",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    await _post(url, payload)


async def _edit_message(chat_id, message_id: int, text: str, reply_markup: dict = None):
    """Редактирование существующего сообщения (для callback)."""
    if not TELEGRAM_TOKEN:
        return
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText"
    payload = {
        "chat_id":                  chat_id,
        "message_id":               message_id,
        "text":                     text,
        "parse_mode":               "Markdown",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    await _post(url, payload)


async def _answer_callback(callback_id: str, text: str = ""):
    """Подтверждение нажатия кнопки (убирает loading)."""
    if not TELEGRAM_TOKEN:
        return
    await _post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
        {"callback_query_id": callback_id, "text": text}
    )


async def _get_updates() -> list[dict]:
    global _last_update_id
    if not TELEGRAM_TOKEN:
        return []
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params={
                "offset": _last_update_id + 1,
                "timeout": 5, "limit": 10},
                timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json()
                return data.get("result", [])
    except Exception:
        return []


# ──────────────────────────────────────────────────────────────────────────────
#  INLINE КЛАВИАТУРА
# ──────────────────────────────────────────────────────────────────────────────

def _main_keyboard() -> dict:
    """Основная inline-клавиатура с командами."""
    pause_btn = "▶️ Продолжить" if _paused else "⏸ Пауза"
    pause_cb  = "cmd_resume"    if _paused else "cmd_pause"

    return {
        "inline_keyboard": [
            [
                {"text": "📊 Статус",     "callback_data": "cmd_status"},
                {"text": "👁 Активные",   "callback_data": "cmd_active"},
            ],
            [
                {"text": "📈 Статистика", "callback_data": "cmd_stats"},
                {"text": pause_btn,        "callback_data": pause_cb},
            ],
            [
                {"text": "❓ Помощь",     "callback_data": "cmd_help"},
            ],
        ]
    }


# ──────────────────────────────────────────────────────────────────────────────
#  УТИЛИТЫ ФОРМАТИРОВАНИЯ
# ──────────────────────────────────────────────────────────────────────────────

def _uptime() -> str:
    secs = int(time.time() - _bot_start_time)
    h, m = divmod(secs // 60, 60)
    d, h = divmod(h, 24)
    if d > 0:
        return f"{d}д {h}ч {m}м"
    return f"{h}ч {m}м"


def _runtime() -> str:
    secs = int(time.time() - _all_time_stats["started_at"])
    h, m = divmod(secs // 60, 60)
    d, h = divmod(h, 24)
    if d > 0:
        return f"{d}д {h}ч"
    return f"{h}ч {m}м"


# ──────────────────────────────────────────────────────────────────────────────
#  КОМАНДЫ
# ──────────────────────────────────────────────────────────────────────────────

async def _cmd_start(chat_id):
    text = (
        "🤖 *Bybit Flat Scanner v9*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Сканирую рынок каждые 5 минут.\n"
        "Ищу боковики для Grid Bot и готовящиеся пробои.\n\n"
        "Используй кнопки ниже 👇"
    )
    await _send(chat_id, text, reply_markup=_main_keyboard())


async def _cmd_menu(chat_id):
    await _send(chat_id, "📋 *Меню команд:*", reply_markup=_main_keyboard())


async def _cmd_status(chat_id):
    active  = len(_active_flats_ref) if _active_flats_ref else 0
    found   = _daily_stats_ref.get("found", 0)   if _daily_stats_ref else 0
    exits   = _daily_stats_ref.get("exits", 0)   if _daily_stats_ref else 0
    skipped = _daily_stats_ref.get("skipped", 0) if _daily_stats_ref else 0
    status  = "⏸ *Пауза*" if _paused else "✅ *Работает*"

    # Считаем Grid vs Breakout среди активных
    grids     = sum(1 for v in (_active_flats_ref or {}).values() if v.get("mode","grid") == "grid")
    breakouts = active - grids

    text = (
        f"🤖 *Статус бота*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Состояние: {status}\n"
        f"⏱ Аптайм: `{_uptime()}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👁 *Активных:* `{active}` "
        f"(🤖 Grid: `{grids}` · ⚡ Breakout: `{breakouts}`)\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 *За сегодня:*\n"
        f"🆕 Сигналов: `{found}` · 📤 Выходов: `{exits}`\n"
        f"⏭ Пропущено API: `{skipped}`"
    )
    await _send(chat_id, text, reply_markup=_main_keyboard())


async def _cmd_active(chat_id):
    if not _active_flats_ref or len(_active_flats_ref) == 0:
        await _send(chat_id, "👁 Активных боковиков нет.", reply_markup=_main_keyboard())
        return

    now   = time.time()
    items = sorted(_active_flats_ref.items(),
                   key=lambda x: x[1].get("score", 0), reverse=True)

    grids     = [(k,v) for k,v in items if v.get("mode","grid") == "grid"]
    breakouts = [(k,v) for k,v in items if v.get("mode","grid") != "grid"]

    lines = [f"👁 *Активные боковики* ({len(_active_flats_ref)})\n"]

    if grids:
        lines.append(f"🤖 *Grid Bot* ({len(grids)}):")
        for key, stats in grids[:8]:
            sym, tf  = key.rsplit("_", 1)
            age_h    = round((now - stats.get("since", now)) / 3600, 1)
            score    = stats.get("score", 0)
            rng_pct  = stats.get("range_pct", 0)
            f_icon   = "🟢" if stats.get("funding", {}).get("is_safe", True) else "🔴"
            profit   = stats.get("profit", {})
            apy      = profit.get("apy_pct", 0)
            lines.append(
                f"  `{sym}` [{TF_LABELS.get(tf,tf)}] "
                f"{score}/10 · {rng_pct}% · {age_h}ч {f_icon} APY~{apy:.0f}%"
            )

    if breakouts:
        if grids: lines.append("")
        lines.append(f"⚡ *Ждём пробой* ({len(breakouts)}):")
        for key, stats in breakouts[:8]:
            sym, tf  = key.rsplit("_", 1)
            age_h    = round((now - stats.get("since", now)) / 3600, 1)
            score    = stats.get("score", 0)
            smart    = stats.get("smart", {})
            br       = smart.get("breakout_risk", 0)
            rng      = smart.get("range_info", {})
            rh       = rng.get("range_high", stats.get("range_high", 0))
            rl       = rng.get("range_low",  stats.get("range_low",  0))
            sweep    = smart.get("sweep", {})
            sw_hint  = (" ↑" if sweep["direction"]=="up" else " ↓") if sweep.get("has_sweep") else ""
            lines.append(
                f"  `{sym}` [{TF_LABELS.get(tf,tf)}] "
                f"{score}/10 · Risk {br}/10{sw_hint} · {age_h}ч\n"
                f"    🟢`{rl}` — 🔴`{rh}`"
            )

    if len(_active_flats_ref) > 16:
        lines.append(f"\n_...ещё {len(_active_flats_ref)-16}_")

    await _send(chat_id, "\n".join(lines), reply_markup=_main_keyboard())


async def _cmd_stats(chat_id):
    st   = _all_time_stats
    tot  = st["grid_signals"] + st["breakout_signals"]
    best = st["best_signal"]
    best_line = ""
    if best:
        mode_icon = "🤖" if best["mode"] == "grid" else "⚡"
        best_line = (
            f"\n🏆 *Лучший сигнал:*\n"
            f"  {mode_icon} `{best['symbol']}` [{TF_LABELS.get(best['tf'], best['tf'])}] "
            f"скор `{best['score']}/10`"
        )

    # Процент Grid vs Breakout
    grid_pct = round(st["grid_signals"] / tot * 100) if tot > 0 else 0
    bo_pct   = 100 - grid_pct

    # Соотношение подтверждённых пробоев
    bo_total = st["breakout_signals"]
    conf_pct = round(st["confirmed_breakouts"] / bo_total * 100) if bo_total > 0 else 0

    text = (
        f"📈 *Статистика сканера*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Работает: `{_runtime()}`\n"
        f"📊 Всего сигналов: `{tot}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🤖 Grid сигналов: `{st['grid_signals']}` ({grid_pct}%)\n"
        f"⚡ Breakout сигналов: `{st['breakout_signals']}` ({bo_pct}%)\n"
        f"💥 Пробоев подтверждено: `{st['confirmed_breakouts']}` ({conf_pct}% от Breakout)\n"
        f"📤 Выходов из боковика: `{st['exits_total']}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👁 Активных сейчас: `{len(_active_flats_ref) if _active_flats_ref else 0}`"
        f"{best_line}"
    )
    await _send(chat_id, text, reply_markup=_main_keyboard())


async def _cmd_pause(chat_id):
    global _paused
    _paused = True
    await _send(chat_id,
        "⏸ *Сканирование приостановлено.*\n"
        "Новые сигналы не отправляются.",
        reply_markup=_main_keyboard()
    )
    logger.info("⏸ Пауза через Telegram")


async def _cmd_resume(chat_id):
    global _paused
    _paused = False
    await _send(chat_id,
        "▶️ *Сканирование возобновлено.*\n"
        "Следующий скан через ≤5 минут.",
        reply_markup=_main_keyboard()
    )
    logger.info("▶️ Возобновлено через Telegram")


async def _cmd_help(chat_id):
    await _send(chat_id,
        "🤖 *Bybit Flat Scanner v9 — Справка*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Бот сканирует все USDT пары Bybit каждые *5 минут*.\n\n"
        "*Типы сигналов:*\n"
        "🤖 *Grid* — безопасный боковик, запускай сетку\n"
        "⚡ *Breakout* — ждать пробой уровня\n"
        "💥 *Пробой случился* — подтверждённый пробой с точкой входа\n"
        "⚠️ *Выход* — боковик сломан, закрывай Grid Bot\n\n"
        "*Фильтры:*\n"
        "• ADX < 20 + BB < 4% + ATR < 3%\n"
        "• Smart Money: структура, FVG, Sweep, Displacement\n"
        "• Funding rate фильтр\n"
        "• Мультитаймфрейм подтверждение\n\n"
        "*Команды:*\n"
        "/status · /active · /stats · /pause · /resume",
        reply_markup=_main_keyboard()
    )


# ──────────────────────────────────────────────────────────────────────────────
#  ОБРАБОТКА CALLBACK (нажатие кнопок)
# ──────────────────────────────────────────────────────────────────────────────

async def _handle_callback(callback: dict):
    """Обрабатывает нажатие inline-кнопки."""
    cb_id   = callback.get("id", "")
    chat_id = callback["message"]["chat"]["id"]
    data    = callback.get("data", "")

    # Подтверждаем нажатие (убирает loading spinner)
    await _answer_callback(cb_id)

    # Проверяем доступ
    if TELEGRAM_CHAT_ID and str(chat_id) != str(TELEGRAM_CHAT_ID):
        return

    if data == "cmd_status":  await _cmd_status(chat_id)
    elif data == "cmd_active": await _cmd_active(chat_id)
    elif data == "cmd_stats":  await _cmd_stats(chat_id)
    elif data == "cmd_pause":  await _cmd_pause(chat_id)
    elif data == "cmd_resume": await _cmd_resume(chat_id)
    elif data == "cmd_help":   await _cmd_help(chat_id)


# ──────────────────────────────────────────────────────────────────────────────
#  ОБРАБОТКА СООБЩЕНИЙ
# ──────────────────────────────────────────────────────────────────────────────

async def _handle_message(update: dict):
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat_id = str(msg["chat"]["id"])
    text    = msg.get("text", "").strip()

    if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
        return

    cmd = text.split()[0].lower().split("@")[0] if text else ""

    if cmd in ("/start",):     await _cmd_start(chat_id)
    elif cmd == "/menu":       await _cmd_menu(chat_id)
    elif cmd == "/status":     await _cmd_status(chat_id)
    elif cmd == "/active":     await _cmd_active(chat_id)
    elif cmd == "/stats":      await _cmd_stats(chat_id)
    elif cmd == "/pause":      await _cmd_pause(chat_id)
    elif cmd == "/resume":     await _cmd_resume(chat_id)
    elif cmd in ("/help",):    await _cmd_help(chat_id)
    elif text.startswith("/"): await _send(chat_id,
        "❓ Неизвестная команда.\n/menu — открыть меню",
        reply_markup=_main_keyboard()
    )


# ──────────────────────────────────────────────────────────────────────────────
#  РЕГИСТРАЦИЯ КОМАНД В TELEGRAM
# ──────────────────────────────────────────────────────────────────────────────

async def _set_bot_commands():
    """Регистрирует список команд в Telegram (показывается в меню '/')."""
    if not TELEGRAM_TOKEN:
        return
    commands = [
        {"command": "menu",   "description": "📋 Открыть меню"},
        {"command": "status", "description": "📊 Статус бота"},
        {"command": "active", "description": "👁 Активные боковики"},
        {"command": "stats",  "description": "📈 Статистика"},
        {"command": "pause",  "description": "⏸ Приостановить"},
        {"command": "resume", "description": "▶️ Возобновить"},
        {"command": "help",   "description": "❓ Справка"},
    ]
    await _post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setMyCommands",
        {"commands": commands}
    )
    logger.info("✅ Команды бота зарегистрированы в Telegram")


# ──────────────────────────────────────────────────────────────────────────────
#  POLLING
# ──────────────────────────────────────────────────────────────────────────────

async def poll_commands():
    global _last_update_id
    logger.info("🎮 Telegram command polling запущен")

    # Регистрируем команды в Telegram при старте
    await _set_bot_commands()

    while True:
        try:
            updates = await _get_updates()
            for upd in updates:
                _last_update_id = max(_last_update_id, upd.get("update_id", 0))

                # Обрабатываем нажатие кнопки
                if "callback_query" in upd:
                    await _handle_callback(upd["callback_query"])
                else:
                    await _handle_message(upd)

        except Exception as e:
            logger.debug(f"poll: {e}")

        await asyncio.sleep(3)
