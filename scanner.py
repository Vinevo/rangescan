import asyncio
import logging
import time
import numpy as np
from pybit.unified_trading import HTTP
import pandas as pd
import ta as ta_lib
from notifier import send_signal, send_exit_alert, send_daily_report
from state import load_state, save_state
from funding import analyse_funding
from profit import calc_profit, format_profit_block
from sr_cache import get_cached, set_cached, clear_stale
from bot_commands import is_paused

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
#  ОБЁРТКИ ИНДИКАТОРОВ (замена pandas_ta → ta)
# ──────────────────────────────────────────────────────────────────────────────

def _adx(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series | None:
    """Возвращает Series значений ADX."""
    try:
        indicator = ta_lib.trend.ADXIndicator(high, low, close, window=length)
        return indicator.adx()
    except Exception:
        return None


def _bbands(close: pd.Series, length: int = 20) -> pd.DataFrame | None:
    """Возвращает DataFrame с колонками BBU, BBM, BBL."""
    try:
        bb = ta_lib.volatility.BollingerBands(close, window=length, window_dev=2)
        df = pd.DataFrame()
        df["BBU_20_2.0"] = bb.bollinger_hband()
        df["BBM_20_2.0"] = bb.bollinger_mavg()
        df["BBL_20_2.0"] = bb.bollinger_lband()
        return df
    except Exception:
        return None


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series | None:
    """Возвращает Series значений ATR."""
    try:
        return ta_lib.volatility.AverageTrueRange(
            high, low, close, window=length
        ).average_true_range()
    except Exception:
        return None


def _rsi(close: pd.Series, length: int = 14) -> pd.Series | None:
    """Возвращает Series значений RSI."""
    try:
        return ta_lib.momentum.RSIIndicator(close, window=length).rsi()
    except Exception:
        return None

# ══════════════════════════════════════════════════════════════════════════════
#  НАСТРОЙКИ
# ══════════════════════════════════════════════════════════════════════════════

# Индикаторы — основные условия
ADX_THRESHOLD       = 20
BB_SQUEEZE_RATIO    = 0.04
ATR_RATIO_MAX       = 0.03

# RSI флет: застрял между этими значениями = нейтральный рынок
RSI_FLAT_LOW        = 40
RSI_FLAT_HIGH       = 60
RSI_FLAT_CANDLES    = 5    # RSI должен быть в зоне последние N свечей подряд

# Уровни поддержки/сопротивления
SR_LOOKBACK         = 50   # Свечей назад для поиска уровней
SR_TOUCH_THRESHOLD  = 0.003  # 0.3% — точность касания уровня
SR_MIN_TOUCHES      = 2      # Мин. касаний чтобы уровень считался сильным
SR_PROXIMITY        = 0.015  # Боковик в пределах 1.5% от уровня = хорошо

# Фильтры качества
MIN_VOLUME_USDT     = 5_000_000
MIN_AGE_DAYS        = 14
MIN_FLAT_CANDLES    = 8
MAX_FALSE_BREAKS    = 2

# Таймфреймы (добавлен 4ч)
TIMEFRAMES  = ["30", "60", "240", "D"]
TF_LABELS   = {"30": "30м", "60": "1ч", "240": "4ч", "D": "1д"}

# MTF: младший → старший для подтверждения
MTF_MAP = {
    "30":  "60",    # 30м подтверждается 1ч
    "60":  "240",   # 1ч  подтверждается 4ч   ← новая связка
    "240": "D",     # 4ч  подтверждается 1д
    "D":   "W",     # 1д  фильтруется неделей
}
# Недельный используется только как фильтр тренда (ADX < 30)
WEEKLY_ADX_MAX      = 30

CANDLES_NEEDED  = 70
API_DELAY       = 0.15

# Grid Bot — ограничения
GRID_MIN_STEP_PCT = 0.003   # Мин. шаг сетки = 0.3% от цены
GRID_MAX_COUNT    = 30      # Макс. сеток
GRID_MIN_COUNT    = 5       # Мин. сеток

# ══════════════════════════════════════════════════════════════════════════════

session = HTTP(testnet=False)

# Загружаем с диска — переживает рестарты
active_flats, last_alerts = load_state()
daily_stats: dict = {"found": 0, "exits": 0, "top": [], "skipped": 0}

# ──────────────────────────────────────────────────────────────────────────────
#  API РЕТРАИ
# ──────────────────────────────────────────────────────────────────────────────

async def _api_call_with_retry(fn, *args, retries: int = 3, delay: float = 1.0, **kwargs):
    """
    Обёртка для API вызовов с ретраями.
    При ошибке ждёт delay секунд и повторяет до retries раз.
    """
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            err_str = str(e).lower()
            is_rate_limit = "too many" in err_str or "429" in err_str or "rate" in err_str
            wait = delay * (2 ** attempt) if is_rate_limit else delay
            if attempt < retries - 1:
                logger.debug(f"API retry {attempt+1}/{retries}: {e} (ждём {wait:.1f}с)")
                await asyncio.sleep(wait)
            else:
                logger.warning(f"API failed after {retries} retries: {e}")
                return None
    return None


# ──────────────────────────────────────────────────────────────────────────────
#  ПОЛУЧЕНИЕ ДАННЫХ
# ──────────────────────────────────────────────────────────────────────────────

def get_all_usdt_symbols() -> list[dict]:
    try:
        resp    = session.get_instruments_info(category="linear", limit=1000)
        tickers = session.get_tickers(category="linear")

        vol_map = {}
        for t in tickers["result"]["list"]:
            try:
                vol_map[t["symbol"]] = float(t.get("turnover24h", 0))
            except Exception:
                pass

        now_ms       = int(time.time() * 1000)
        two_weeks_ms = MIN_AGE_DAYS * 24 * 3600 * 1000
        result = []

        for item in resp["result"]["list"]:
            sym = item["symbol"]
            if not sym.endswith("USDT") or item.get("status") != "Trading":
                continue
            launch = int(item.get("launchTime", 0))
            if launch == 0 or (now_ms - launch) < two_weeks_ms:
                continue
            vol = vol_map.get(sym, 0)
            if vol < MIN_VOLUME_USDT:
                continue
            result.append({"symbol": sym, "volume24h": vol})

        result.sort(key=lambda x: x["volume24h"], reverse=True)
        logger.info(f"📊 Монет: {len(result)} (объём > ${MIN_VOLUME_USDT/1e6:.0f}M)")
        return result
    except Exception as e:
        logger.error(f"get_all_usdt_symbols: {e}")
        return []


async def get_klines_async(symbol: str, interval: str, limit: int = None) -> pd.DataFrame | None:
    """get_klines с ретраями и счётчиком пропущенных."""
    resp = await _api_call_with_retry(
        session.get_kline,
        category="linear", symbol=symbol,
        interval=interval, limit=limit or CANDLES_NEEDED
    )
    if resp is None:
        daily_stats["skipped"] += 1
        return None
    try:
        data = resp["result"]["list"]
        if not data or len(data) < 20:
            return None
        df = pd.DataFrame(
            data,
            columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"]
        )
        df = df.astype({
            "open": float, "high": float, "low": float,
            "close": float, "volume": float, "turnover": float
        })
        df["timestamp"] = df["timestamp"].astype(int)
        return df.sort_values("timestamp").reset_index(drop=True)
    except Exception:
        return None


def get_klines(symbol: str, interval: str, limit: int = None) -> pd.DataFrame | None:
    """Синхронная версия для MTF и бэктеста."""
    try:
        resp = session.get_kline(
            category="linear", symbol=symbol,
            interval=interval, limit=limit or CANDLES_NEEDED
        )
        data = resp["result"]["list"]
        if not data or len(data) < 20:
            return None
        df = pd.DataFrame(
            data,
            columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"]
        )
        df = df.astype({
            "open": float, "high": float, "low": float,
            "close": float, "volume": float, "turnover": float
        })
        df["timestamp"] = df["timestamp"].astype(int)
        return df.sort_values("timestamp").reset_index(drop=True)
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────────
#  УРОВНИ ПОДДЕРЖКИ И СОПРОТИВЛЕНИЯ
# ──────────────────────────────────────────────────────────────────────────────

def find_sr_levels(df: pd.DataFrame, symbol: str = "", tf: str = "") -> list[dict]:
    """
    Ищем значимые уровни поддержки/сопротивления через pivot-точки.
    Кэшируется на 1 час через sr_cache.
    """
    # Проверяем кэш
    if symbol and tf:
        cached = get_cached(symbol, tf)
        if cached is not None:
            return cached

    highs  = df["high"].values
    lows   = df["low"].values
    closes = df["close"].values
    n      = len(df)

    # Локальные пики и впадины (pivot points)
    pivot_highs = []
    pivot_lows  = []
    window = 3

    for i in range(window, n - window):
        if all(highs[i] >= highs[i-j] for j in range(1, window+1)) and \
           all(highs[i] >= highs[i+j] for j in range(1, window+1)):
            pivot_highs.append(highs[i])
        if all(lows[i] <= lows[i-j] for j in range(1, window+1)) and \
           all(lows[i] <= lows[i+j] for j in range(1, window+1)):
            pivot_lows.append(lows[i])

    # Кластеризуем близкие уровни
    def cluster_levels(prices: list[float]) -> list[dict]:
        if not prices:
            return []
        prices_sorted = sorted(prices)
        clusters = []
        current  = [prices_sorted[0]]

        for p in prices_sorted[1:]:
            if abs(p - current[-1]) / current[-1] < SR_TOUCH_THRESHOLD * 2:
                current.append(p)
            else:
                clusters.append(current)
                current = [p]
        clusters.append(current)

        result = []
        for cl in clusters:
            if len(cl) >= SR_MIN_TOUCHES:
                result.append({
                    "price":   round(float(np.mean(cl)), 8),
                    "touches": len(cl)
                })
        return result

    supports    = cluster_levels(pivot_lows)
    resistances = cluster_levels(pivot_highs)

    current_price = float(closes[-1])
    levels = []

    for s in supports:
        s["type"] = "support"
        levels.append(s)
    for r in resistances:
        r["type"] = "resistance"
        levels.append(r)

    # Сохраняем в кэш
    if symbol and tf:
        set_cached(symbol, tf, levels)

    return levels


def analyse_sr_context(levels: list[dict], price: float, range_low: float, range_high: float) -> dict:
    """
    Оцениваем насколько боковик "защищён" уровнями:
    - снизу есть поддержка → grid не провалится
    - сверху есть сопротивление → grid не сломается вверх
    - боковик прямо между двумя уровнями → идеально
    """
    support_below    = None
    resistance_above = None
    best_support_dist    = 999
    best_resistance_dist = 999

    for lvl in levels:
        lp = lvl["price"]
        # Поддержка снизу от нижней границы диапазона
        if lp < range_low:
            dist = (range_low - lp) / price
            if dist < best_support_dist:
                best_support_dist = dist
                support_below = lvl
        # Сопротивление выше верхней границы
        if lp > range_high:
            dist = (lp - range_high) / price
            if dist < best_resistance_dist:
                best_resistance_dist = dist
                resistance_above = lvl

    has_support    = support_below    is not None and best_support_dist    < SR_PROXIMITY
    has_resistance = resistance_above is not None and best_resistance_dist < SR_PROXIMITY
    sandwiched     = has_support and has_resistance  # Идеал: боковик между двух уровней

    return {
        "support_below":      support_below,
        "resistance_above":   resistance_above,
        "has_support":        has_support,
        "has_resistance":     has_resistance,
        "sandwiched":         sandwiched,
        "support_dist_pct":   round(best_support_dist    * 100, 2) if support_below    else None,
        "resistance_dist_pct":round(best_resistance_dist * 100, 2) if resistance_above else None,
    }


# ──────────────────────────────────────────────────────────────────────────────
#  RSI ФИЛЬТР
# ──────────────────────────────────────────────────────────────────────────────

def check_rsi_flat(df: pd.DataFrame) -> tuple[bool, float]:
    try:
        rsi_s = _rsi(df["close"], length=14)
        if rsi_s is None or rsi_s.dropna().empty:
            return False, 50.0
        rsi_current = float(rsi_s.iloc[-1])
        recent_rsi  = rsi_s.iloc[-RSI_FLAT_CANDLES:]
        in_zone = all(RSI_FLAT_LOW <= v <= RSI_FLAT_HIGH for v in recent_rsi if not np.isnan(v))
        return in_zone, round(rsi_current, 1)
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

        # ── ADX ────────────────────────────────────────────────────────────────
        adx_s = _adx(high, low, close, length=14)
        if adx_s is None or adx_s.dropna().empty:
            return None
        adx_val = float(adx_s.iloc[-1])

        # ── Bollinger Bands ────────────────────────────────────────────────────
        bb_df = _bbands(close, length=20)
        if bb_df is None or bb_df.empty:
            return None
        bb_upper = float(bb_df["BBU_20_2.0"].iloc[-1])
        bb_lower = float(bb_df["BBL_20_2.0"].iloc[-1])
        bb_mid   = float(bb_df["BBM_20_2.0"].iloc[-1])
        bb_width = (bb_upper - bb_lower) / price

        # ── ATR ────────────────────────────────────────────────────────────────
        atr_s = _atr(high, low, close, length=14)
        if atr_s is None or atr_s.dropna().empty:
            return None
        atr_val   = float(atr_s.iloc[-1])
        atr_ratio = atr_val / price

        # Базовые условия
        adx_ok = adx_val  < ADX_THRESHOLD
        bb_ok  = bb_width < BB_SQUEEZE_RATIO
        atr_ok = atr_ratio < ATR_RATIO_MAX
        if not (adx_ok and bb_ok and atr_ok):
            return None

        # ── RSI флет ───────────────────────────────────────────────────────────
        rsi_flat, rsi_val = check_rsi_flat(df)
        # RSI не блокирует сигнал, но влияет на скор

        # ── Длительность боковика ──────────────────────────────────────────────
        in_range = ((close >= bb_lower) & (close <= bb_upper)).tolist()
        flat_candles = 0
        for v in reversed(in_range):
            if v:
                flat_candles += 1
            else:
                break
        if flat_candles < MIN_FLAT_CANDLES:
            return None

        # ── Ложные выходы ──────────────────────────────────────────────────────
        recent_high = high.iloc[-flat_candles:]
        recent_low  = low.iloc[-flat_candles:]
        false_breaks = int(
            (recent_high > bb_upper * 1.005).sum() +
            (recent_low  < bb_lower * 0.995).sum()
        )
        if false_breaks > MAX_FALSE_BREAKS:
            return None

        # ── Объём ──────────────────────────────────────────────────────────────
        vol = df["volume"]
        vol_recent  = float(vol.iloc[-flat_candles:].mean())
        prev_start  = max(0, len(vol) - flat_candles * 2)
        prev_end    = len(vol) - flat_candles
        vol_prev    = float(vol.iloc[prev_start:prev_end].mean()) if prev_end > prev_start else vol_recent
        vol_growing = vol_recent > vol_prev * 1.1

        # ── Диапазон Grid Bot ──────────────────────────────────────────────────
        range_low  = round(bb_lower * 0.998, 8)
        range_high = round(bb_upper * 1.002, 8)
        range_pct  = round((range_high - range_low) / price * 100, 2)
        span       = range_high - range_low

        # Шаг сетки: максимум из ATR и минимального % от цены
        # Это защищает от ситуации когда ATR очень мал и получается 40+ сеток
        atr_step     = atr_val
        min_step_abs = price * GRID_MIN_STEP_PCT
        grid_step    = round(max(atr_step, min_step_abs), 8)

        # Кол-во сеток в диапазоне GRID_MIN_COUNT..GRID_MAX_COUNT
        raw_count  = int(span / grid_step) if grid_step > 0 else GRID_MIN_COUNT
        grid_count = max(GRID_MIN_COUNT, min(GRID_MAX_COUNT, raw_count))

        # ── Уровни S/R (с кэшем) ──────────────────────────────────────────────
        sr_levels = find_sr_levels(df.tail(SR_LOOKBACK), symbol=symbol, tf=tf)
        sr_ctx    = analyse_sr_context(sr_levels, price, range_low, range_high)

        # ── Скор 0–13 ──────────────────────────────────────────────────────────
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

        # ── Funding rate ───────────────────────────────────────────────────────
        funding = analyse_funding(symbol) if symbol else {
            "is_safe": True, "is_warning": False, "comment": "⚪ Funding: н/д",
            "daily_pct": 0.0, "rate_pct": "н/д"
        }

        # ── Расчёт прибыли (для 24ч, депозит $1000) ───────────────────────────
        tf_hours = {"30": 0.5, "60": 1.0, "240": 4.0, "D": 24.0}
        expected_duration_h = flat_candles * tf_hours.get(tf, 1.0)
        profit = calc_profit(
            stats={
                "price": price, "range_low": range_low, "range_high": range_high,
                "grid_count": grid_count, "grid_step": grid_step,
            },
            tf=tf,
            funding_daily_pct=funding.get("daily_pct", 0.0),
            deposit_usdt=1000.0,
            duration_h=max(expected_duration_h, 4.0),
        )

        return {
            "price":        price,
            "adx":          round(adx_val, 2),
            "bb_width_pct": round(bb_width * 100, 2),
            "atr_pct":      round(atr_ratio * 100, 2),
            "rsi":          rsi_val,
            "rsi_flat":     rsi_flat,
            "bb_upper":     round(bb_upper, 8),
            "bb_lower":     round(bb_lower, 8),
            "bb_mid":       round(bb_mid, 8),
            "flat_candles": flat_candles,
            "false_breaks": false_breaks,
            "vol_growing":  vol_growing,
            "range_low":    range_low,
            "range_high":   range_high,
            "range_pct":    range_pct,
            "grid_count":   grid_count,
            "grid_step":    round(grid_step, 8),
            "sr":           sr_ctx,
            "funding":      funding,
            "profit":       profit,
            "score":        score,
            "adx_ok":       adx_ok,
            "bb_ok":        bb_ok,
            "atr_ok":       atr_ok,
        }

    except Exception as e:
        logger.debug(f"analyse_flat: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────────────
#  ВЫХОД ИЗ БОКОВИКА
# ──────────────────────────────────────────────────────────────────────────────

def check_exit(df: pd.DataFrame, saved: dict) -> bool:
    try:
        price = float(df["close"].iloc[-1])
        adx_s = _adx(df["high"], df["low"], df["close"], length=14)
        if adx_s is None:
            return False
        if float(adx_s.iloc[-1]) > 25:
            return True
        if price > saved["range_high"] * 1.01:
            return True
        if price < saved["range_low"] * 0.99:
            return True
        return False
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────────────
#  МУЛЬТИТАЙМФРЕЙМНОЕ ПОДТВЕРЖДЕНИЕ
# ──────────────────────────────────────────────────────────────────────────────

async def mtf_confirmed(symbol: str, junior_tf: str) -> bool:
    """
    Проверяем старший таймфрейм.
    Для 1д дополнительно проверяем недельный (ADX < WEEKLY_ADX_MAX).
    """
    senior = MTF_MAP.get(junior_tf)
    if not senior:
        return True

    await asyncio.sleep(API_DELAY)

    # Недельный таймфрейм — только как фильтр сильного тренда
    if senior == "W":
        df_w = get_klines(symbol, "W", limit=30)
        if df_w is None:
            return True
        try:
            adx_s = _adx(df_w["high"], df_w["low"], df_w["close"], length=14)
            if adx_s is None:
                return True
            weekly_adx = float(adx_s.iloc[-1])
            if weekly_adx > WEEKLY_ADX_MAX:
                logger.debug(f"Weekly ADX filter: {symbol} ADX={weekly_adx:.1f}")
                return False
            return True
        except Exception:
            return True

    # Обычная MTF проверка: старший ADX < 25
    df_s = get_klines(symbol, senior)
    if df_s is None:
        return False
    try:
        adx_s = _adx(df_s["high"], df_s["low"], df_s["close"], length=14)
        if adx_s is None:
            return False
        return float(adx_s.iloc[-1]) < 25
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────────────
#  ГЛАВНЫЙ СКАН
# ──────────────────────────────────────────────────────────────────────────────

async def scan_market():
    # Проверяем паузу
    if is_paused():
        logger.info("⏸ Скан пропущен — бот на паузе")
        return

    logger.info("🔍 Сканирование запущено...")
    start   = time.time()
    now     = time.time()
    found   = 0
    exits   = 0
    skipped = 0
    top_signals = []

    symbols = get_all_usdt_symbols()
    if not symbols:
        logger.warning("⚠️ Список символов пуст — скан прерван")
        return

    # Чистим устаревший S/R кэш раз в скан
    cleared = clear_stale()
    if cleared:
        logger.debug(f"S/R кэш: удалено {cleared} устаревших записей")

    for item in symbols:
        symbol = item["symbol"]
        vol24h = item["volume24h"]

        for tf in TIMEFRAMES:
            await asyncio.sleep(API_DELAY)

            # Используем async версию с ретраями
            df = await get_klines_async(symbol, tf)
            if df is None:
                skipped += 1
                continue

            key = f"{symbol}_{tf}"

            # ── Проверка выхода из активного боковика ─────────────────────────
            if key in active_flats:
                if check_exit(df, active_flats[key]):
                    old = active_flats.pop(key)
                    exits += 1
                    daily_stats["exits"] += 1
                    duration_h = round((now - old.get("since", now)) / 3600, 1)
                    logger.info(f"⚠️ Выход: {symbol} [{TF_LABELS[tf]}] {duration_h}ч")
                    await send_exit_alert(symbol, tf, old, duration_h)
                continue

            # ── Анализ нового боковика (передаём symbol+tf для кэша и funding) ─
            stats = analyse_flat(df, symbol=symbol, tf=tf)
            if stats is None:
                continue

            # ── MTF подтверждение ──────────────────────────────────────────────
            if not await mtf_confirmed(symbol, tf):
                logger.debug(f"MTF reject: {symbol} [{TF_LABELS[tf]}]")
                continue

            # ── Funding фильтр: блокируем опасный funding ──────────────────────
            funding = stats.get("funding", {})
            if not funding.get("is_safe", True):
                logger.info(
                    f"💸 Funding reject: {symbol} [{TF_LABELS[tf]}] "
                    f"{funding.get('rate_pct', '?')} — grid нецелесообразен"
                )
                continue

            stats["volume24h"] = vol24h

            # ── Дедупликация — раз в 6 часов ──────────────────────────────────
            if now - last_alerts.get(key, 0) < 21600:
                continue

            stats["since"]    = now
            active_flats[key] = stats
            last_alerts[key]  = now
            found += 1
            daily_stats["found"] += 1
            top_signals.append((stats["score"], symbol, tf, stats))

            sr     = stats["sr"]
            profit = stats.get("profit", {})
            logger.info(
                f"✅ {symbol} [{TF_LABELS[tf]}] score={stats['score']}/10 "
                f"ADX={stats['adx']} RSI={stats['rsi']} "
                f"S/R={'🏆' if sr['sandwiched'] else ('✅' if sr['has_support'] else '⚪')} "
                f"APY~{profit.get('apy_pct', 0):.0f}% "
                f"funding={funding.get('rate_pct', '?')}"
            )

    # Сортируем по скору, отправляем лучшие первыми
    top_signals.sort(key=lambda x: x[0], reverse=True)
    for _, symbol, tf, stats in top_signals:
        await send_signal(symbol, tf, stats)

    daily_stats["top"] = [
        {"symbol": s, "tf": t, "score": sc}
        for sc, s, t, _ in top_signals[:5]
    ]
    daily_stats["skipped"] = daily_stats.get("skipped", 0) + skipped

    # Сохраняем состояние на диск
    save_state(active_flats, last_alerts)

    elapsed = round(time.time() - start, 1)
    logger.info(
        f"Итог: {elapsed}с | новых={found} | выходов={exits} | "
        f"активных={len(active_flats)} | пропущено={skipped}"
    )


async def send_daily_summary():
    await send_daily_report(daily_stats, len(active_flats))
    daily_stats["found"] = 0
    daily_stats["exits"] = 0
    daily_stats["top"]   = []
