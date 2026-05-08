"""
scanner.py — Bybit Flat Scanner v9

Улучшения vs v8:
- Параллельные запросы батчами по BATCH_SIZE монет (скан ~1.5 мин вместо 4.5)
- Weekly Open фильтр
- Исправлен grid_count (шаг от % цены, не только ATR)
"""

import asyncio
import logging
import time
import numpy as np
from pybit.unified_trading import HTTP
import pandas as pd
import ta as ta_lib
from notifier import send_signal, send_exit_alert, send_daily_report, send_breakout_alert
from state import load_state, save_state
from funding import analyse_funding
from profit import calc_profit, format_profit_block
from sr_cache import get_cached, set_cached, clear_stale
from bot_commands import is_paused
from smart_analysis import full_smart_analysis

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
#  НАСТРОЙКИ
# ══════════════════════════════════════════════════════════════════════════════

ADX_THRESHOLD        = 20
BB_SQUEEZE_RATIO     = 0.04
ATR_RATIO_MAX        = 0.03
RSI_FLAT_LOW         = 40
RSI_FLAT_HIGH        = 60
RSI_FLAT_CANDLES     = 5
SR_LOOKBACK          = 50
SR_TOUCH_THRESHOLD   = 0.003
SR_MIN_TOUCHES       = 2
SR_PROXIMITY         = 0.015
MIN_VOLUME_USDT      = 5_000_000
MIN_AGE_DAYS         = 14
MIN_FLAT_CANDLES     = 8
MAX_FALSE_BREAKS     = 2
TIMEFRAMES           = ["30", "60", "240", "D"]
TF_LABELS            = {"30": "30м", "60": "1ч", "240": "4ч", "D": "1д"}
MTF_MAP              = {"30": "60", "60": "240", "240": "D", "D": "W"}
WEEKLY_ADX_MAX       = 30
CANDLES_NEEDED       = 70
API_DELAY            = 0.12
GRID_MIN_STEP_PCT    = 0.003
GRID_OPTIMAL_STEP_PCT = 0.005
GRID_MAX_COUNT       = 30
GRID_MIN_COUNT       = 5

# Параллельность: сколько монет сканируем одновременно
BATCH_SIZE           = 8

# ══════════════════════════════════════════════════════════════════════════════

session = HTTP(testnet=False)
active_flats, last_alerts = load_state()
daily_stats: dict = {"found": 0, "exits": 0, "top": [], "skipped": 0}


# ──────────────────────────────────────────────────────────────────────────────
#  ИНДИКАТОРЫ
# ──────────────────────────────────────────────────────────────────────────────

def _adx(high, low, close, length=14):
    try:
        return ta_lib.trend.ADXIndicator(high, low, close, window=length).adx()
    except Exception:
        return None


def _bbands(close, length=20):
    try:
        bb = ta_lib.volatility.BollingerBands(close, window=length, window_dev=2)
        df = pd.DataFrame()
        df["BBU_20_2.0"] = bb.bollinger_hband()
        df["BBM_20_2.0"] = bb.bollinger_mavg()
        df["BBL_20_2.0"] = bb.bollinger_lband()
        return df
    except Exception:
        return None


def _atr(high, low, close, length=14):
    try:
        return ta_lib.volatility.AverageTrueRange(high, low, close, window=length).average_true_range()
    except Exception:
        return None


def _rsi(close, length=14):
    try:
        return ta_lib.momentum.RSIIndicator(close, window=length).rsi()
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────────
#  API С РЕТРАЯМИ
# ──────────────────────────────────────────────────────────────────────────────

async def _api_retry(fn, *args, retries=3, delay=1.0, **kwargs):
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            err = str(e).lower()
            wait = delay * (2 ** attempt) if ("429" in err or "rate" in err) else delay
            if attempt < retries - 1:
                await asyncio.sleep(wait)
            else:
                logger.warning(f"API failed {retries}x: {e}")
                return None
    return None


async def get_klines_async(symbol: str, interval: str, limit: int = None) -> pd.DataFrame | None:
    resp = await _api_retry(session.get_kline, category="linear",
                            symbol=symbol, interval=interval, limit=limit or CANDLES_NEEDED)
    if resp is None:
        daily_stats["skipped"] += 1
        return None
    try:
        data = resp["result"]["list"]
        if not data or len(data) < 20:
            return None
        df = pd.DataFrame(data, columns=["timestamp","open","high","low","close","volume","turnover"])
        df = df.astype({"open":float,"high":float,"low":float,"close":float,"volume":float,"turnover":float})
        df["timestamp"] = df["timestamp"].astype(int)
        return df.sort_values("timestamp").reset_index(drop=True)
    except Exception:
        return None


def get_klines(symbol: str, interval: str, limit: int = None) -> pd.DataFrame | None:
    try:
        resp = session.get_kline(category="linear", symbol=symbol,
                                 interval=interval, limit=limit or CANDLES_NEEDED)
        data = resp["result"]["list"]
        if not data or len(data) < 20:
            return None
        df = pd.DataFrame(data, columns=["timestamp","open","high","low","close","volume","turnover"])
        df = df.astype({"open":float,"high":float,"low":float,"close":float,"volume":float,"turnover":float})
        df["timestamp"] = df["timestamp"].astype(int)
        return df.sort_values("timestamp").reset_index(drop=True)
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────────
#  ПОЛУЧЕНИЕ СИМВОЛОВ
# ──────────────────────────────────────────────────────────────────────────────

def get_all_usdt_symbols() -> list[dict]:
    try:
        resp    = session.get_instruments_info(category="linear", limit=1000)
        tickers = session.get_tickers(category="linear")
        vol_map = {}
        for t in tickers["result"]["list"]:
            try: vol_map[t["symbol"]] = float(t.get("turnover24h", 0))
            except: pass
        now_ms       = int(time.time() * 1000)
        two_weeks_ms = MIN_AGE_DAYS * 24 * 3600 * 1000
        result = []
        for item in resp["result"]["list"]:
            sym = item["symbol"]
            if not sym.endswith("USDT") or item.get("status") != "Trading": continue
            launch = int(item.get("launchTime", 0))
            if launch == 0 or (now_ms - launch) < two_weeks_ms: continue
            vol = vol_map.get(sym, 0)
            if vol < MIN_VOLUME_USDT: continue
            result.append({"symbol": sym, "volume24h": vol})
        result.sort(key=lambda x: x["volume24h"], reverse=True)
        logger.info(f"📊 Монет: {len(result)} (объём > ${MIN_VOLUME_USDT/1e6:.0f}M)")
        return result
    except Exception as e:
        logger.error(f"get_symbols: {e}")
        return []


# ──────────────────────────────────────────────────────────────────────────────
#  S/R УРОВНИ
# ──────────────────────────────────────────────────────────────────────────────

def find_sr_levels(df: pd.DataFrame, symbol: str = "", tf: str = "") -> list[dict]:
    if symbol and tf:
        cached = get_cached(symbol, tf)
        if cached is not None:
            return cached
    try:
        highs  = df["high"].values
        lows   = df["low"].values
        closes = df["close"].values
        n, w   = len(df), 3

        ph = [highs[i] for i in range(w,n-w)
              if all(highs[i]>=highs[i-j] for j in range(1,w+1)) and
                 all(highs[i]>=highs[i+j] for j in range(1,w+1))]
        pl = [lows[i]  for i in range(w,n-w)
              if all(lows[i]<=lows[i-j]  for j in range(1,w+1)) and
                 all(lows[i]<=lows[i+j]  for j in range(1,w+1))]

        def cluster(prices):
            if not prices: return []
            ps = sorted(prices)
            clusters, cur = [], [ps[0]]
            for p in ps[1:]:
                if abs(p-cur[-1])/cur[-1] < SR_TOUCH_THRESHOLD*2: cur.append(p)
                else: clusters.append(cur); cur=[p]
            clusters.append(cur)
            return [{"price": round(float(np.mean(c)),8), "touches": len(c)}
                    for c in clusters if len(c) >= SR_MIN_TOUCHES]

        price  = float(closes[-1])
        levels = []
        for s in cluster(pl): s["type"]="support";    levels.append(s)
        for r in cluster(ph): r["type"]="resistance"; levels.append(r)

        if symbol and tf: set_cached(symbol, tf, levels)
        return levels
    except Exception:
        return []


def analyse_sr_context(levels, price, range_low, range_high):
    sup = res = None
    bd = br = 999
    for lvl in levels:
        lp = lvl["price"]
        if lp < range_low:
            d = (range_low-lp)/price
            if d < bd: bd, sup = d, lvl
        if lp > range_high:
            d = (lp-range_high)/price
            if d < br: br, res = d, lvl
    hs = sup is not None and bd < SR_PROXIMITY
    hr = res is not None and br < SR_PROXIMITY
    return {"support_below": sup, "resistance_above": res,
            "has_support": hs, "has_resistance": hr, "sandwiched": hs and hr,
            "support_dist_pct": round(bd*100,2) if sup else None,
            "resistance_dist_pct": round(br*100,2) if res else None}


# ──────────────────────────────────────────────────────────────────────────────
#  RSI ФЛЕТ
# ──────────────────────────────────────────────────────────────────────────────

def check_rsi_flat(df: pd.DataFrame) -> tuple[bool, float]:
    try:
        rsi_s = _rsi(df["close"], length=14)
        if rsi_s is None or rsi_s.dropna().empty:
            return False, 50.0
        rv  = float(rsi_s.iloc[-1])
        rec = rsi_s.iloc[-RSI_FLAT_CANDLES:]
        ok  = all(RSI_FLAT_LOW <= v <= RSI_FLAT_HIGH for v in rec if not np.isnan(v))
        return ok, round(rv, 1)
    except Exception:
        return False, 50.0


# ──────────────────────────────────────────────────────────────────────────────
#  АНАЛИЗ БОКОВИКА
# ──────────────────────────────────────────────────────────────────────────────

def analyse_flat(df: pd.DataFrame, symbol: str = "", tf: str = "") -> dict | None:
    try:
        close = df["close"]
        high  = df["high"]
        low   = df["low"]
        price = float(close.iloc[-1])

        adx_s = _adx(high, low, close, length=14)
        if adx_s is None or adx_s.dropna().empty: return None
        adx_val = float(adx_s.iloc[-1])

        bb_df = _bbands(close, length=20)
        if bb_df is None or bb_df.empty: return None
        bb_upper = float(bb_df["BBU_20_2.0"].iloc[-1])
        bb_lower = float(bb_df["BBL_20_2.0"].iloc[-1])
        bb_mid   = float(bb_df["BBM_20_2.0"].iloc[-1])
        bb_width = (bb_upper - bb_lower) / price

        atr_s = _atr(high, low, close, length=14)
        if atr_s is None or atr_s.dropna().empty: return None
        atr_val   = float(atr_s.iloc[-1])
        atr_ratio = atr_val / price

        if not (adx_val < ADX_THRESHOLD and bb_width < BB_SQUEEZE_RATIO and atr_ratio < ATR_RATIO_MAX):
            return None

        rsi_flat, rsi_val = check_rsi_flat(df)

        in_range = ((close >= bb_lower) & (close <= bb_upper)).tolist()
        flat_candles = 0
        for v in reversed(in_range):
            if v: flat_candles += 1
            else: break
        if flat_candles < MIN_FLAT_CANDLES: return None

        rh = high.iloc[-flat_candles:]
        rl = low.iloc[-flat_candles:]
        false_breaks = int((rh > bb_upper*1.005).sum() + (rl < bb_lower*0.995).sum())
        if false_breaks > MAX_FALSE_BREAKS: return None

        vol = df["volume"]
        vr  = float(vol.iloc[-flat_candles:].mean())
        vp  = float(vol.iloc[max(0,len(vol)-flat_candles*2):len(vol)-flat_candles].mean()) if len(vol) >= flat_candles*2 else vr
        vol_growing = vr > vp * 1.1

        range_low  = round(bb_lower * 0.998, 8)
        range_high = round(bb_upper * 1.002, 8)
        range_pct  = round((range_high - range_low) / price * 100, 2)
        span       = range_high - range_low
        min_step   = price * GRID_MIN_STEP_PCT
        opt_step   = price * GRID_OPTIMAL_STEP_PCT
        grid_step  = round(max(atr_val, min_step, opt_step), 8)
        raw_count  = int(span / grid_step) if grid_step > 0 else GRID_MIN_COUNT
        grid_count = max(GRID_MIN_COUNT, min(GRID_MAX_COUNT, raw_count))

        sr_levels = find_sr_levels(df.tail(SR_LOOKBACK), symbol=symbol, tf=tf)
        sr_ctx    = analyse_sr_context(sr_levels, price, range_low, range_high)

        funding = analyse_funding(symbol) if symbol else {
            "is_safe": True, "is_warning": False,
            "comment": "⚪ Funding: н/д", "daily_pct": 0.0, "rate_pct": "н/д"}

        tf_hours = {"30": 0.5, "60": 1.0, "240": 4.0, "D": 24.0}
        dur_h    = max(flat_candles * tf_hours.get(tf, 1.0), 4.0)
        profit   = calc_profit(
            {"price": price, "range_low": range_low, "range_high": range_high,
             "grid_count": grid_count, "grid_step": grid_step},
            tf=tf, funding_daily_pct=funding.get("daily_pct", 0.0),
            deposit_usdt=1000.0, duration_h=dur_h)

        smart = full_smart_analysis(df, flat_candles, round(bb_width*100,2), tf=tf)

        # Скор 0–10
        score = 0
        score += 3 if adx_val   < 15   else (2 if adx_val   < 18   else 1)
        score += 2 if bb_width  < 0.02 else (1 if bb_width  < 0.03 else 0)
        score += 2 if atr_ratio < 0.015 else (1 if atr_ratio < 0.025 else 0)
        score += 2 if flat_candles >= 15 else (1 if flat_candles >= 8 else 0)
        score += 1 if false_breaks == 0 else 0
        score += 2 if rsi_flat else 0
        score += 1 if sr_ctx["has_support"]    else 0
        score += 1 if sr_ctx["has_resistance"] else 0
        score += 1 if sr_ctx["sandwiched"]     else 0
        score  = round(min(score / 13 * 10, 10))

        return {
            "price": price, "adx": round(adx_val,2),
            "bb_width_pct": round(bb_width*100,2), "atr_pct": round(atr_ratio*100,2),
            "rsi": rsi_val, "rsi_flat": rsi_flat,
            "bb_upper": round(bb_upper,8), "bb_lower": round(bb_lower,8),
            "flat_candles": flat_candles, "false_breaks": false_breaks,
            "vol_growing": vol_growing, "range_low": range_low,
            "range_high": range_high, "range_pct": range_pct,
            "grid_count": grid_count, "grid_step": round(grid_step,8),
            "sr": sr_ctx, "funding": funding, "profit": profit,
            "smart": smart, "score": score,
            "adx_ok": True, "bb_ok": True, "atr_ok": True,
        }
    except Exception as e:
        logger.debug(f"analyse_flat: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────────────
#  ВЫХОД ИЗ БОКОВИКА
# ──────────────────────────────────────────────────────────────────────────────

def check_exit(df: pd.DataFrame, saved: dict) -> tuple[bool, str | None]:
    """
    Проверяет вышла ли цена из боковика.
    Возвращает (exited, direction) где direction = "up" / "down" / None.

    "up"   — пробой вверх  (бычий пробой, возможен лонг)
    "down" — пробой вниз   (медвежий пробой, возможен шорт)
    None   — ADX вырос, но нет чёткого пробоя уровня
    """
    try:
        price = float(df["close"].iloc[-1])
        adx_s = _adx(df["high"], df["low"], df["close"], length=14)
        if adx_s is None:
            return False, None

        adx_now = float(adx_s.iloc[-1])
        rh = saved["range_high"]
        rl = saved["range_low"]

        # Пробой вверх: цена закрылась выше верхней границы на 1%+
        if price > rh * 1.01:
            return True, "up"

        # Пробой вниз: цена закрылась ниже нижней границы на 1%+
        if price < rl * 0.99:
            return True, "down"

        # ADX вырос — тренд начался, но направление неясно
        if adx_now > 25:
            # Определяем направление по положению цены
            mid = (rh + rl) / 2
            direction = "up" if price > mid else "down"
            return True, direction

        return False, None
    except Exception:
        return False, None


# ──────────────────────────────────────────────────────────────────────────────
#  MTF ПОДТВЕРЖДЕНИЕ
# ──────────────────────────────────────────────────────────────────────────────

async def mtf_confirmed(symbol: str, junior_tf: str) -> bool:
    senior = MTF_MAP.get(junior_tf)
    if not senior: return True
    await asyncio.sleep(API_DELAY)
    if senior == "W":
        df_w = get_klines(symbol, "W", limit=30)
        if df_w is None: return True
        try:
            adx_s = _adx(df_w["high"], df_w["low"], df_w["close"], length=14)
            return adx_s is None or float(adx_s.iloc[-1]) < WEEKLY_ADX_MAX
        except: return True
    df_s = get_klines(symbol, senior)
    if df_s is None: return False
    try:
        adx_s = _adx(df_s["high"], df_s["low"], df_s["close"], length=14)
        return adx_s is not None and float(adx_s.iloc[-1]) < 25
    except: return False


# ──────────────────────────────────────────────────────────────────────────────
#  ПАРАЛЛЕЛЬНОЕ СКАНИРОВАНИЕ ОДНОЙ МОНЕТЫ
# ──────────────────────────────────────────────────────────────────────────────

async def scan_symbol(item: dict, now: float, semaphore: asyncio.Semaphore) -> list[tuple]:
    """
    Сканирует одну монету по всем таймфреймам.
    Возвращает список готовых сигналов [(score, symbol, tf, stats)].
    semaphore ограничивает параллельность.
    """
    async with semaphore:
        symbol = item["symbol"]
        vol24h = item["volume24h"]
        signals = []
        exits   = []

        for tf in TIMEFRAMES:
            await asyncio.sleep(API_DELAY)
            df = await get_klines_async(symbol, tf)
            if df is None:
                continue

            key = f"{symbol}_{tf}"

            # Проверка выхода
            if key in active_flats:
                exited, direction = check_exit(df, active_flats[key])
                if exited:
                    old = active_flats.pop(key)
                    dur = round((now - old.get("since", now)) / 3600, 1)
                    bp  = float(df["close"].iloc[-1])
                    exits.append((symbol, tf, old, dur, direction, bp))
                continue

            stats = analyse_flat(df, symbol=symbol, tf=tf)
            if stats is None:
                continue

            if not await mtf_confirmed(symbol, tf):
                continue

            funding = stats.get("funding", {})
            if not funding.get("is_safe", True):
                logger.info(f"💸 Funding reject: {symbol} [{TF_LABELS[tf]}] "
                            f"{funding.get('rate_pct','?')}")
                continue

            smart          = stats.get("smart", {})
            recommendation = smart.get("recommendation", "grid")
            grid_allowed   = smart.get("grid_allowed", True)

            if not grid_allowed and recommendation != "breakout":
                reasons = ", ".join(smart.get("grid_blocked", []))
                logger.info(f"🚫 Grid заблокирован: {symbol} [{TF_LABELS[tf]}] — {reasons}")
                continue

            if now - last_alerts.get(key, 0) < 21600:
                continue

            stats["volume24h"] = vol24h
            stats["since"]     = now
            stats["mode"]      = recommendation
            signals.append((stats["score"], symbol, tf, stats))

        return signals, exits


# ──────────────────────────────────────────────────────────────────────────────
#  ГЛАВНЫЙ СКАН
# ──────────────────────────────────────────────────────────────────────────────

async def scan_market():
    if is_paused():
        logger.info("⏸ Скан пропущен — пауза")
        return

    logger.info("🔍 Сканирование запущено...")
    start = time.time()
    now   = time.time()

    symbols = get_all_usdt_symbols()
    if not symbols:
        logger.warning("⚠️ Список символов пуст — скан прерван")
        return

    cleared = clear_stale()
    if cleared:
        logger.debug(f"S/R кэш: удалено {cleared}")

    # Семафор ограничивает параллельность до BATCH_SIZE
    semaphore    = asyncio.Semaphore(BATCH_SIZE)
    all_signals  = []
    total_exits  = 0

    # Запускаем все монеты параллельно батчами
    tasks = [scan_symbol(item, now, semaphore) for item in symbols]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, Exception):
            logger.debug(f"scan_symbol error: {result}")
            continue
        signals, exits = result

        # Обрабатываем выходы
        for symbol, tf, old, dur, direction, bp in exits:
            total_exits += 1
            daily_stats["exits"] += 1
            logger.info(f"⚠️ Выход: {symbol} [{TF_LABELS[tf]}] {dur}ч direction={direction}")

            # Если был сигнал BREAKOUT и теперь случился пробой — отправляем спец.алерт
            if old.get("mode") == "breakout" and direction:
                logger.info(f"💥 Пробой подтверждён: {symbol} [{TF_LABELS[tf]}] → {direction}")
                daily_stats["breakouts"] = daily_stats.get("breakouts", 0) + 1
                await send_breakout_alert(
                    symbol, tf, old, direction, bp,
                    old["range_high"], old["range_low"]
                )
            else:
                # Обычный выход из боковика
                await send_exit_alert(symbol, tf, old, dur)

        # Сохраняем новые сигналы
        for score, symbol, tf, stats in signals:
            key = f"{symbol}_{tf}"
            active_flats[key] = stats
            last_alerts[key]  = now
            daily_stats["found"] += 1
            all_signals.append((score, symbol, tf, stats))
            logger.info(
                f"✅ {symbol} [{TF_LABELS[tf]}] score={stats['score']}/10 "
                f"ADX={stats['adx']} RSI={stats['rsi']} "
                f"mode={stats.get('mode','grid')} "
                f"APY~{stats.get('profit',{}).get('apy_pct',0):.0f}%"
            )

    # Сортируем по скору и отправляем
    all_signals.sort(key=lambda x: x[0], reverse=True)
    for _, symbol, tf, stats in all_signals:
        await send_signal(symbol, tf, stats)

    daily_stats["top"] = [
        {"symbol": s, "tf": t, "score": sc}
        for sc, s, t, _ in all_signals[:5]
    ]

    save_state(active_flats, last_alerts)

    elapsed = round(time.time() - start, 1)
    logger.info(
        f"Итог: {elapsed}с | новых={len(all_signals)} | "
        f"выходов={total_exits} | активных={len(active_flats)} | "
        f"пропущено={daily_stats.get('skipped',0)}"
    )


async def send_daily_summary():
    await send_daily_report(daily_stats, len(active_flats))
    daily_stats["found"]   = 0
    daily_stats["exits"]   = 0
    daily_stats["top"]     = []
    daily_stats["skipped"] = 0
