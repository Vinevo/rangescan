"""
bot_commands.py — Telegram команды v9 с портфелем.

Команды:
    /start      — приветствие + меню
    /menu       — меню кнопок
    /status     — статус бота
    /active     — активные боковики
    /portfolio  — мой портфель сделок
    /stats      — статистика
    /pause      — пауза
    /resume     — возобновить
    /help       — справка

Callback:
    add_trade:SYMBOL_TF  — взять сигнал в работу
    portfolio_close:KEY  — закрыть сделку из портфеля
    portfolio_refresh    — обновить портфель
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

_all_time_stats = {
    "grid_signals":        0,
    "breakout_signals":    0,
    "confirmed_breakouts": 0,
    "exits_total":         0,
    "best_signal":         None,
    "started_at":          time.time(),
}

# Ожидаем ввод депозита от пользователя: { chat_id: "KEY" }
_awaiting_deposit: dict = {}


def set_refs(active_flats: dict, daily_stats: dict):
    global _active_flats_ref, _daily_stats_ref
    _active_flats_ref = active_flats
    _daily_stats_ref  = daily_stats


def is_paused() -> bool:
    return _paused


def record_signal(symbol: str, tf: str, score: int, mode: str):
    if mode == "grid":
        _all_time_stats["grid_signals"] += 1
    else:
        _all_time_stats["breakout_signals"] += 1
    best = _all_time_stats["best_signal"]
    if best is None or score > best["score"]:
        _all_time_stats["best_signal"] = {
            "symbol": symbol, "tf": tf, "score": score, "mode": mode}


def record_exit():
    _all_time_stats["exits_total"] += 1


def record_breakout():
    _all_time_stats["confirmed_breakouts"] += 1


# ──────────────────────────────────────────────────────────────────────────────
#  HTTP
# ──────────────────────────────────────────────────────────────────────────────

async def _post(url: str, payload: dict) -> dict:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=payload,
                              timeout=aiohttp.ClientTimeout(total=10)) as r:
                return await r.json()
    except Exception as e:
        logger.debug(f"_post: {e}")
        return {}


async def _send(chat_id, text: str, markup: dict = None):
    if not TELEGRAM_TOKEN:
        return
    payload = {
        "chat_id": chat_id, "text": text,
        "parse_mode": "Markdown", "disable_web_page_preview": True,
    }
    if markup:
        payload["reply_markup"] = markup
    await _post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", payload)


async def _edit(chat_id, msg_id: int, text: str, markup: dict = None):
    if not TELEGRAM_TOKEN:
        return
    payload = {
        "chat_id": chat_id, "message_id": msg_id,
        "text": text, "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    if markup:
        payload["reply_markup"] = markup
    await _post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText", payload)


async def _answer_cb(cb_id: str, text: str = ""):
    if not TELEGRAM_TOKEN:
        return
    await _post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
        {"callback_query_id": cb_id, "text": text, "show_alert": False}
    )


async def _get_updates() -> list[dict]:
    global _last_update_id
    if not TELEGRAM_TOKEN:
        return []
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"offset": _last_update_id + 1, "timeout": 5, "limit": 10},
                timeout=aiohttp.ClientTimeout(total=10)) as r:
                return (await r.json()).get("result", [])
    except Exception:
        return []


# ──────────────────────────────────────────────────────────────────────────────
#  КЛАВИАТУРЫ
# ──────────────────────────────────────────────────────────────────────────────

def _main_kb() -> dict:
    pause_text = "▶️ Продолжить" if _paused else "⏸ Пауза"
    pause_cb   = "cmd_resume"    if _paused else "cmd_pause"
    return {"inline_keyboard": [
        [{"text": "📊 Статус",     "callback_data": "cmd_status"},
         {"text": "👁 Активные",   "callback_data": "cmd_active"}],
        [{"text": "💼 Портфель",   "callback_data": "cmd_portfolio"},
         {"text": "📈 Статистика", "callback_data": "cmd_stats"}],
        [{"text": pause_text,      "callback_data": pause_cb},
         {"text": "❓ Помощь",     "callback_data": "cmd_help"}],
    ]}


def _portfolio_kb(trades: list) -> dict:
    """Клавиатура для портфеля — кнопка закрытия под каждой сделкой."""
    rows = []
    for t in trades[:8]:
        sym  = t["symbol"]
        tf   = TF_LABELS.get(t["tf"], t["tf"])
        mode = "🤖" if t["mode"] == "grid" else "⚡"
        rows.append([
            {"text": f"{mode} {sym} [{tf}] — закрыть ✖️",
             "callback_data": f"portfolio_close:{t['key']}"}
        ])
    rows.append([{"text": "🔄 Обновить", "callback_data": "portfolio_refresh"},
                 {"text": "◀️ Меню",    "callback_data": "cmd_menu"}])
    return {"inline_keyboard": rows}


# ──────────────────────────────────────────────────────────────────────────────
#  УТИЛИТЫ
# ──────────────────────────────────────────────────────────────────────────────

def _uptime() -> str:
    s = int(time.time() - _bot_start_time)
    h, m = divmod(s // 60, 60)
    d, h = divmod(h, 24)
    return f"{d}д {h}ч {m}м" if d > 0 else f"{h}ч {m}м"


def _runtime() -> str:
    s = int(time.time() - _all_time_stats["started_at"])
    h, _ = divmod(s // 60, 60)
    d, h = divmod(h, 24)
    return f"{d}д {h}ч" if d > 0 else f"{h}ч"


# ──────────────────────────────────────────────────────────────────────────────
#  КОМАНДЫ
# ──────────────────────────────────────────────────────────────────────────────

async def _cmd_start(chat_id):
    await _send(chat_id,
        "🤖 *Bybit Flat Scanner v9*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Сканирую рынок каждые 5 минут.\n"
        "Ищу боковики для Grid Bot и пробои.\n\n"
        "Используй кнопки ниже 👇",
        markup=_main_kb())


async def _cmd_menu(chat_id):
    await _send(chat_id, "📋 *Меню:*", markup=_main_kb())


async def _cmd_status(chat_id):
    active    = len(_active_flats_ref) if _active_flats_ref else 0
    found     = _daily_stats_ref.get("found",   0) if _daily_stats_ref else 0
    exits     = _daily_stats_ref.get("exits",   0) if _daily_stats_ref else 0
    skipped   = _daily_stats_ref.get("skipped", 0) if _daily_stats_ref else 0
    status    = "⏸ *Пауза*" if _paused else "✅ *Работает*"
    grids     = sum(1 for v in (_active_flats_ref or {}).values()
                    if v.get("mode","grid") == "grid")
    breakouts = active - grids

    # Портфель
    from portfolio import get_active_trades
    pt = get_active_trades()

    await _send(chat_id,
        f"🤖 *Статус бота*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Состояние: {status} · ⏱ `{_uptime()}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👁 Активных: `{active}` "
        f"(🤖 `{grids}` · ⚡ `{breakouts}`)\n"
        f"💼 В портфеле: `{len(pt)}` сделок\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 *Сегодня:* сигналов `{found}` · выходов `{exits}` · пропущено `{skipped}`",
        markup=_main_kb())


async def _cmd_active(chat_id):
    if not _active_flats_ref or len(_active_flats_ref) == 0:
        await _send(chat_id, "👁 Активных боковиков нет.", markup=_main_kb())
        return

    now   = time.time()
    items = sorted(_active_flats_ref.items(),
                   key=lambda x: x[1].get("score", 0), reverse=True)
    grids     = [(k,v) for k,v in items if v.get("mode","grid") == "grid"]
    breakouts = [(k,v) for k,v in items if v.get("mode","grid") != "grid"]

    lines = [f"👁 *Активные* ({len(_active_flats_ref)})\n"]

    if grids:
        lines.append(f"🤖 *Grid* ({len(grids)}):")
        for key, s in grids[:8]:
            sym, tf = key.rsplit("_", 1)
            age_h   = round((now - s.get("since", now)) / 3600, 1)
            apy     = s.get("profit", {}).get("apy_pct", 0)
            fi      = "🟢" if s.get("funding", {}).get("is_safe", True) else "🔴"
            lines.append(f"  `{sym}` [{TF_LABELS.get(tf,tf)}] "
                         f"{s.get('score',0)}/10 · {s.get('range_pct',0)}% · {age_h}ч {fi}")

    if breakouts:
        if grids: lines.append("")
        lines.append(f"⚡ *Breakout* ({len(breakouts)}):")
        for key, s in breakouts[:8]:
            sym, tf = key.rsplit("_", 1)
            age_h   = round((now - s.get("since", now)) / 3600, 1)
            smart   = s.get("smart", {})
            br      = smart.get("breakout_risk", 0)
            rng     = smart.get("range_info", {})
            rh = rng.get("range_high", s.get("range_high", 0))
            rl = rng.get("range_low",  s.get("range_low",  0))
            sweep   = smart.get("sweep", {})
            sw      = (" ↑" if sweep["direction"]=="up" else " ↓") if sweep.get("has_sweep") else ""
            lines.append(f"  `{sym}` [{TF_LABELS.get(tf,tf)}] "
                         f"{s.get('score',0)}/10 · Risk {br}/10{sw} · {age_h}ч\n"
                         f"    🟢`{rl}` — 🔴`{rh}`")

    await _send(chat_id, "\n".join(lines), markup=_main_kb())


async def _cmd_portfolio(chat_id):
    from portfolio import get_active_trades, estimate_profit

    trades = get_active_trades()
    if not trades:
        await _send(chat_id,
            "💼 *Портфель пуст*\n\n"
            "Нажми *✅ Взять в работу* в любом Grid или Breakout сигнале.",
            markup=_main_kb())
        return

    now        = time.time()
    total_dep  = sum(t.get("deposit", 0) for t in trades)
    total_prof = sum(estimate_profit(t) for t in trades)
    sign_total = "+" if total_prof >= 0 else ""

    lines = [
        f"💼 *Мой портфель* ({len(trades)} сделок)\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💰 Суммарный депозит: `${total_dep:.0f}`\n"
        f"📈 Расч. прибыль: `{sign_total}${total_prof:.2f}`\n"
        f"━━━━━━━━━━━━━━━━━━"
    ]

    for t in sorted(trades, key=lambda x: x["since"], reverse=True):
        sym      = t["symbol"]
        tf_label = TF_LABELS.get(t["tf"], t["tf"])
        mode     = "🤖" if t["mode"] == "grid" else "⚡"
        age_h    = round((now - t["since"]) / 3600, 1)
        profit   = estimate_profit(t)
        sign     = "+" if profit >= 0 else ""
        prof_icon = "✅" if profit >= 0 else "🔴"

        # Статус цены относительно диапазона
        cur_price = t.get("entry_price", 0)  # актуальная цена неизвестна без API
        rl, rh   = t["range_low"], t["range_high"]
        sl, sh   = t.get("stop_loss", 0), t.get("stop_high", 0)

        lines.append(
            f"\n{mode} *{sym}* [{tf_label}] · скор {t['score']}/10\n"
            f"  ⏱ В работе: `{age_h}ч` · Депозит: `${t['deposit']:.0f}`\n"
            f"  📐 `{rl}` — `{rh}` ({t['range_pct']}%)\n"
            f"  🛡 Стоп↓: `{sl}` · Стоп↑: `{sh}`\n"
            f"  {prof_icon} Прибыль: `{sign}${profit:.2f}`"
        )

    lines.append(f"\n━━━━━━━━━━━━━━━━━━")
    lines.append("Нажми на сделку чтобы закрыть 👇")

    await _send(chat_id, "\n".join(lines),
                markup=_portfolio_kb(trades))


async def _cmd_stats(chat_id):
    st   = _all_time_stats
    tot  = st["grid_signals"] + st["breakout_signals"]
    best = st["best_signal"]
    best_line = ""
    if best:
        mode_icon = "🤖" if best["mode"] == "grid" else "⚡"
        best_line = (
            f"\n🏆 *Лучший сигнал:*\n"
            f"  {mode_icon} `{best['symbol']}` "
            f"[{TF_LABELS.get(best['tf'],best['tf'])}] скор `{best['score']}/10`"
        )

    gp   = round(st["grid_signals"]     / tot * 100) if tot > 0 else 0
    bp   = 100 - gp
    bcp  = round(st["confirmed_breakouts"] / max(st["breakout_signals"],1) * 100)

    from portfolio import get_active_trades, get_all_trades, estimate_profit
    all_t  = get_all_trades()
    act_t  = [t for t in all_t if t["status"] == "active"]
    clo_t  = [t for t in all_t if t["status"] == "closed"]
    tot_p  = sum(estimate_profit(t) for t in act_t)
    sign   = "+" if tot_p >= 0 else ""

    await _send(chat_id,
        f"📈 *Статистика*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Работает: `{_runtime()}`\n"
        f"📊 Всего сигналов: `{tot}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🤖 Grid: `{st['grid_signals']}` ({gp}%)\n"
        f"⚡ Breakout: `{st['breakout_signals']}` ({bp}%)\n"
        f"💥 Пробоев подтверждено: `{st['confirmed_breakouts']}` ({bcp}%)\n"
        f"📤 Выходов: `{st['exits_total']}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💼 *Портфель:*\n"
        f"  Активных: `{len(act_t)}` · Закрытых: `{len(clo_t)}`\n"
        f"  Текущая прибыль: `{sign}${tot_p:.2f}`"
        f"{best_line}",
        markup=_main_kb())


async def _cmd_pause(chat_id):
    global _paused
    _paused = True
    await _send(chat_id, "⏸ *Приостановлено.*", markup=_main_kb())
    logger.info("⏸ Пауза")


async def _cmd_resume(chat_id):
    global _paused
    _paused = False
    await _send(chat_id, "▶️ *Возобновлено.*", markup=_main_kb())
    logger.info("▶️ Возобновлено")


async def _cmd_help(chat_id):
    await _send(chat_id,
        "🤖 *Bybit Flat Scanner v9*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "*Сигналы:*\n"
        "🤖 Grid — безопасный боковик\n"
        "⚡ Breakout — ждать пробой\n"
        "💥 Пробой — подтверждённый пробой\n"
        "⚠️ Выход — закрой Grid Bot\n\n"
        "*Портфель:*\n"
        "Нажми ✅ в сигнале → сделка в портфеле\n"
        "Бот следит за стопами и шлёт алерты\n\n"
        "*Стоп-лоссы:*\n"
        "↓ −3% от нижней границы\n"
        "↑ +5% от верхней (или выключить)\n"
        "⚠️ Плечо всегда 1x!\n",
        markup=_main_kb())


# ──────────────────────────────────────────────────────────────────────────────
#  CALLBACK — нажатие кнопок
# ──────────────────────────────────────────────────────────────────────────────

async def _handle_callback(cb: dict):
    cb_id   = cb.get("id", "")
    chat_id = str(cb["message"]["chat"]["id"])
    data    = cb.get("data", "")

    if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
        await _answer_cb(cb_id)
        return

    await _answer_cb(cb_id)

    # Взять сигнал в работу
    if data.startswith("add_trade:"):
        key = data.split(":", 1)[1]
        await _handle_add_trade(chat_id, key)

    # Закрыть сделку из портфеля
    elif data.startswith("portfolio_close:"):
        key = data.split(":", 1)[1]
        await _handle_close_trade(chat_id, key)

    # Обновить портфель
    elif data == "portfolio_refresh":
        await _cmd_portfolio(chat_id)

    # Основные команды
    elif data == "cmd_status":    await _cmd_status(chat_id)
    elif data == "cmd_active":    await _cmd_active(chat_id)
    elif data == "cmd_portfolio": await _cmd_portfolio(chat_id)
    elif data == "cmd_stats":     await _cmd_stats(chat_id)
    elif data == "cmd_pause":     await _cmd_pause(chat_id)
    elif data == "cmd_resume":    await _cmd_resume(chat_id)
    elif data == "cmd_help":      await _cmd_help(chat_id)
    elif data == "cmd_menu":      await _cmd_menu(chat_id)

    # Подтверждение депозита
    elif data.startswith("deposit_confirm:"):
        _, key, amount = data.split(":", 2)
        await _confirm_deposit(chat_id, key, float(amount))


async def _handle_add_trade(chat_id: str, key: str):
    """Добавляет сделку в портфель или предлагает ввести депозит."""
    from portfolio import is_in_portfolio, add_trade, get_trade

    if is_in_portfolio(key):
        await _send(chat_id,
            f"✅ *{key.replace('_',' ')} уже в портфеле*",
            markup=_main_kb())
        return

    # Ищем stats в active_flats
    stats = (_active_flats_ref or {}).get(key)
    if not stats:
        await _send(chat_id,
            f"⚠️ Сигнал `{key}` не найден в активных.\n"
            f"Возможно боковик уже закончился.",
            markup=_main_kb())
        return

    # Предлагаем выбрать депозит
    sym, tf = key.rsplit("_", 1)
    tf_label = TF_LABELS.get(tf, tf)
    await _send(chat_id,
        f"💼 *{sym}* [{tf_label}] — выбери сумму депозита:",
        markup={"inline_keyboard": [
            [{"text": "$50",   "callback_data": f"deposit_confirm:{key}:50"},
             {"text": "$100",  "callback_data": f"deposit_confirm:{key}:100"}],
            [{"text": "$200",  "callback_data": f"deposit_confirm:{key}:200"},
             {"text": "$500",  "callback_data": f"deposit_confirm:{key}:500"}],
            [{"text": "❌ Отмена", "callback_data": "cmd_menu"}],
        ]})


async def _confirm_deposit(chat_id: str, key: str, deposit: float):
    """Подтверждает добавление сделки с выбранным депозитом."""
    from portfolio import add_trade

    stats = (_active_flats_ref or {}).get(key)
    if not stats:
        await _send(chat_id, "⚠️ Сигнал уже неактуален.", markup=_main_kb())
        return

    sym, tf  = key.rsplit("_", 1)
    tf_label = TF_LABELS.get(tf, tf)
    trade    = add_trade(stats, sym, tf, deposit)

    await _send(chat_id,
        f"✅ *{sym}* [{tf_label}] добавлен в портфель!\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💰 Депозит: `${deposit:.0f}`\n"
        f"📐 Диапазон: `{trade['range_low']}` — `{trade['range_high']}`\n"
        f"🛡 Нижний стоп: `{trade['stop_loss']}`\n"
        f"🛡 Верхний стоп: `{trade['stop_high']}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Буду следить и пришлю алерт если цена подойдёт к стопу.",
        markup=_main_kb())


async def _handle_close_trade(chat_id: str, key: str):
    """Закрывает сделку из портфеля."""
    from portfolio import close_trade, get_trade, estimate_profit

    trade = get_trade(key)
    if not trade:
        await _send(chat_id, "⚠️ Сделка не найдена.", markup=_main_kb())
        return

    profit = estimate_profit(trade)
    sign   = "+" if profit >= 0 else ""
    age_h  = round((time.time() - trade["since"]) / 3600, 1)

    close_trade(key, "Закрыто вручную")

    sym, tf = key.rsplit("_", 1)
    await _send(chat_id,
        f"✖️ *{sym}* [{TF_LABELS.get(tf,tf)}] закрыта\n"
        f"⏱ Держалась: `{age_h}ч`\n"
        f"💰 Расч. прибыль: `{sign}${profit:.2f}`",
        markup=_main_kb())


# ──────────────────────────────────────────────────────────────────────────────
#  МОНИТОРИНГ ПОРТФЕЛЯ — вызывается из scan_market
# ──────────────────────────────────────────────────────────────────────────────

async def check_portfolio_stops(current_prices: dict):
    """
    Проверяет стопы по всем сделкам портфеля.
    current_prices: { "BTCUSDT": 65000.0, ... }
    Вызывается из scanner.py после каждого скана.
    """
    from portfolio import get_active_trades, check_trade_status, close_trade, estimate_profit
    from notifier import send_portfolio_stop_alert, send_portfolio_exit_alert

    for trade in get_active_trades():
        sym    = trade["symbol"]
        price  = current_prices.get(sym)
        if price is None:
            continue

        status = check_trade_status(trade, price)
        if not status or status == "in_range":
            continue

        estimate_profit(trade)  # обновляем расч. прибыль

        if status in ("stop_low", "stop_high"):
            dur = round((time.time() - trade["since"]) / 3600, 1)
            reason = "Сработал нижний стоп-лосс" if status == "stop_low" else "Сработал верхний стоп"
            close_trade(trade["key"], reason)
            await send_portfolio_stop_alert(trade, status, price)
            logger.info(f"🛡 Стоп портфеля: {sym} [{trade['tf']}] {status} @ {price}")

        elif status in ("near_stop_low", "near_stop_high"):
            await send_portfolio_stop_alert(trade, status, price)
            logger.info(f"⚠️ Близко к стопу: {sym} [{trade['tf']}] @ {price}")


async def notify_portfolio_exit(symbol: str, tf: str, duration_h: float):
    """
    Уведомляет если сделка из портфеля вышла из боковика по сигналу сканера.
    Вызывается из scanner.py при обнаружении выхода.
    """
    from portfolio import get_trade, close_trade, estimate_profit
    from notifier import send_portfolio_exit_alert

    key   = f"{symbol}_{tf}"
    trade = get_trade(key)
    if not trade or trade.get("status") != "active":
        return

    estimate_profit(trade)
    close_trade(key, "Боковик закончился — сигнал сканера")
    await send_portfolio_exit_alert(trade, duration_h, "Боковик закончился!")
    logger.info(f"💼 Портфель: выход {symbol} [{tf}] после {duration_h}ч")


# ──────────────────────────────────────────────────────────────────────────────
#  РЕГИСТРАЦИЯ КОМАНД
# ──────────────────────────────────────────────────────────────────────────────

async def _set_bot_commands():
    if not TELEGRAM_TOKEN:
        return
    await _post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setMyCommands",
        {"commands": [
            {"command": "menu",      "description": "📋 Открыть меню"},
            {"command": "status",    "description": "📊 Статус бота"},
            {"command": "active",    "description": "👁 Активные боковики"},
            {"command": "portfolio", "description": "💼 Мой портфель"},
            {"command": "stats",     "description": "📈 Статистика"},
            {"command": "pause",     "description": "⏸ Приостановить"},
            {"command": "resume",    "description": "▶️ Возобновить"},
            {"command": "help",      "description": "❓ Справка"},
        ]}
    )
    logger.info("✅ Команды зарегистрированы в Telegram")


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

    if cmd in ("/start",):      await _cmd_start(chat_id)
    elif cmd == "/menu":        await _cmd_menu(chat_id)
    elif cmd == "/status":      await _cmd_status(chat_id)
    elif cmd == "/active":      await _cmd_active(chat_id)
    elif cmd == "/portfolio":   await _cmd_portfolio(chat_id)
    elif cmd == "/stats":       await _cmd_stats(chat_id)
    elif cmd == "/pause":       await _cmd_pause(chat_id)
    elif cmd == "/resume":      await _cmd_resume(chat_id)
    elif cmd in ("/help",):     await _cmd_help(chat_id)
    elif text.startswith("/"):  await _send(chat_id,
        "❓ /menu — открыть меню", markup=_main_kb())


# ──────────────────────────────────────────────────────────────────────────────
#  POLLING
# ──────────────────────────────────────────────────────────────────────────────

async def poll_commands():
    global _last_update_id
    logger.info("🎮 Telegram polling запущен")
    await _set_bot_commands()

    while True:
        try:
            updates = await _get_updates()
            for upd in updates:
                _last_update_id = max(_last_update_id, upd.get("update_id", 0))
                if "callback_query" in upd:
                    await _handle_callback(upd["callback_query"])
                else:
                    await _handle_message(upd)
        except Exception as e:
            logger.debug(f"poll: {e}")
        await asyncio.sleep(3)
