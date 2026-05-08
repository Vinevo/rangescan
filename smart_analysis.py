"""
smart_analysis.py — Smart Money анализ для фильтрации сигналов.

Реализует:
1. Определение структуры рынка (uptrend/downtrend/range)
2. Детектор импульса
3. Определение диапазона (верх/низ, касания, ширина)
4. Позиция цены (top/middle/bottom)
5. BB squeeze
6. Анализ объёма (накопление)
7. Длительность флета

Выдаёт:
    range_score     (0–10) — насколько хорош для Grid
    breakout_risk   (0–10) — насколько опасен пробой
    market_structure — "uptrend" / "downtrend" / "range"
    recommendation  — "grid" / "breakout" / "wait"
"""

import logging
import numpy as np
import pandas as pd
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
#  НАСТРОЙКИ
# ══════════════════════════════════════════════════════════════════════════════

STRUCTURE_LOOKBACK   = 30     # свечей для определения структуры
IMPULSE_MULT         = 1.5    # свеча > 1.5x средней = импульс
VOLUME_IMPULSE_MULT  = 1.5    # объём > 1.5x средней = импульс
RANGE_MIN_TOUCHES    = 2      # мин. касаний с каждой стороны
RANGE_MIN_WIDTH_PCT  = 0.03   # мин. ширина диапазона 3%
RANGE_TOP_ZONE       = 0.75   # верхние 25% диапазона = top
RANGE_BOT_ZONE       = 0.25   # нижние 25% диапазона = bottom
LONG_FLAT_CANDLES    = 20     # флет держится > 20 свечей = долгий
VOLUME_GROW_RATIO    = 1.15   # объём растёт на 15%+ = накопление

# FVG настройки
FVG_MIN_SIZE_PCT     = 0.002  # минимальный размер FVG 0.2% от цены
FVG_LOOKBACK         = 30     # свечей назад для поиска FVG
FVG_DANGER_DIST_PCT  = 0.03   # FVG в пределах 3% от цены = опасно

# Session filter (UTC часы)
SESSIONS = {
    "london":   (7,  12),   # 07:00–12:00 UTC
    "new_york": (13, 17),   # 13:00–17:00 UTC
    "overlap":  (13, 16),   # London+NY overlap — самый активный
    "asia":     (0,   7),   # 00:00–07:00 UTC — тихая сессия
}
# На каком таймфрейме сессии имеют смысл (только внутри дня)
SESSION_RELEVANT_TF  = {"30", "60"}

# Displacement
DISPLACEMENT_MULT    = 2.0    # свеча > 2x средней = displacement
DISPLACEMENT_LOOKBACK = 15    # свечей назад

# Liquidity Sweep
SWEEP_LOOKBACK       = 30     # свечей для поиска уровней ликвидности
SWEEP_TOUCH_THRESH   = 0.003  # 0.3% — точность касания уровня
SWEEP_MIN_TOUCHES    = 2      # мин. касаний чтобы уровень считался ликвидным
SWEEP_RETURN_PCT     = 0.005  # цена вернулась на 0.5%+ после пробоя = sweep


# ══════════════════════════════════════════════════════════════════════════════
#  1. СТРУКТУРА РЫНКА
# ══════════════════════════════════════════════════════════════════════════════

def detect_market_structure(df: pd.DataFrame) -> dict:
    """
    Определяет структуру рынка по последним STRUCTURE_LOOKBACK свечам.

    Алгоритм:
    - Находим локальные пики и впадины (swing high/low)
    - Uptrend:   HH (higher high) + HL (higher low)
    - Downtrend: LH (lower high)  + LL (lower low)
    - Range:     нет чёткой последовательности HH/HL или LH/LL

    Возвращает dict:
        structure   "uptrend" / "downtrend" / "range"
        hh_count    кол-во Higher Highs
        hl_count    кол-во Higher Lows
        lh_count    кол-во Lower Highs
        ll_count    кол-во Lower Lows
        trend_strength  0.0–1.0
    """
    try:
        df_s = df.tail(STRUCTURE_LOOKBACK).copy().reset_index(drop=True)
        highs  = df_s["high"].values
        lows   = df_s["low"].values
        n      = len(df_s)

        # Находим swing highs и swing lows (окно 3)
        w = 2
        swing_highs = []
        swing_lows  = []

        for i in range(w, n - w):
            if all(highs[i] >= highs[i-j] for j in range(1, w+1)) and \
               all(highs[i] >= highs[i+j] for j in range(1, w+1)):
                swing_highs.append((i, highs[i]))
            if all(lows[i] <= lows[i-j] for j in range(1, w+1)) and \
               all(lows[i] <= lows[i+j] for j in range(1, w+1)):
                swing_lows.append((i, lows[i]))

        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return _structure_result("range", 0, 0, 0, 0)

        # Считаем HH/HL/LH/LL по последним свингам
        hh = sum(1 for i in range(1, len(swing_highs))
                 if swing_highs[i][1] > swing_highs[i-1][1])
        lh = sum(1 for i in range(1, len(swing_highs))
                 if swing_highs[i][1] < swing_highs[i-1][1])
        hl = sum(1 for i in range(1, len(swing_lows))
                 if swing_lows[i][1] > swing_lows[i-1][1])
        ll = sum(1 for i in range(1, len(swing_lows))
                 if swing_lows[i][1] < swing_lows[i-1][1])

        total = max(hh + lh + hl + ll, 1)

        # Определяем структуру
        uptrend_score   = (hh + hl) / total
        downtrend_score = (lh + ll) / total

        if hh >= 2 and hl >= 2 and uptrend_score > 0.6:
            structure = "uptrend"
            strength  = uptrend_score
        elif lh >= 2 and ll >= 2 and downtrend_score > 0.6:
            structure = "downtrend"
            strength  = downtrend_score
        else:
            structure = "range"
            strength  = 1.0 - max(uptrend_score, downtrend_score)

        return _structure_result(structure, hh, hl, lh, ll, strength)

    except Exception as e:
        logger.debug(f"detect_market_structure: {e}")
        return _structure_result("range", 0, 0, 0, 0)


def _structure_result(structure, hh, hl, lh, ll, strength=0.5):
    return {
        "structure":      structure,
        "hh_count":       hh,
        "hl_count":       hl,
        "lh_count":       lh,
        "ll_count":       ll,
        "trend_strength": round(strength, 2),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  2. ДЕТЕКТОР ИМПУЛЬСА
# ══════════════════════════════════════════════════════════════════════════════

def detect_impulse(df: pd.DataFrame) -> dict:
    """
    Ищет импульсные свечи в последних 10 свечах.

    Импульс = свеча > IMPULSE_MULT × средней ИЛИ объём > VOLUME_IMPULSE_MULT × среднего.

    Возвращает dict:
        has_impulse     bool
        impulse_candles кол-во импульсных свечей
        max_candle_mult максимальный множитель (насколько сильнее средней)
        max_vol_mult    максимальный объёмный множитель
        last_impulse_ago сколько свечей назад был последний импульс
    """
    try:
        close  = df["close"].values
        open_  = df["open"].values
        volume = df["volume"].values
        n      = len(df)

        # Средняя величина свечи и объём за предыдущие 20 свечей
        lookback = min(20, n - 10)
        if lookback < 5:
            return _impulse_result(False, 0, 1.0, 1.0, 999)

        avg_candle = np.mean(np.abs(close[-n:-10] - open_[-n:-10]))
        avg_volume = np.mean(volume[-n:-10])

        if avg_candle == 0 or avg_volume == 0:
            return _impulse_result(False, 0, 1.0, 1.0, 999)

        # Проверяем последние 10 свечей
        recent_close  = close[-10:]
        recent_open   = open_[-10:]
        recent_volume = volume[-10:]

        candle_sizes = np.abs(recent_close - recent_open)
        candle_mults = candle_sizes / avg_candle
        volume_mults = recent_volume / avg_volume

        impulse_mask = (candle_mults >= IMPULSE_MULT) | (volume_mults >= VOLUME_IMPULSE_MULT)
        impulse_count = int(impulse_mask.sum())

        max_candle_mult = float(candle_mults.max())
        max_vol_mult    = float(volume_mults.max())

        # Сколько свечей назад был последний импульс
        last_impulse_ago = 999
        for i in range(len(impulse_mask) - 1, -1, -1):
            if impulse_mask[i]:
                last_impulse_ago = len(impulse_mask) - 1 - i
                break

        has_impulse = impulse_count > 0

        return _impulse_result(has_impulse, impulse_count,
                               max_candle_mult, max_vol_mult, last_impulse_ago)

    except Exception as e:
        logger.debug(f"detect_impulse: {e}")
        return _impulse_result(False, 0, 1.0, 1.0, 999)


def _impulse_result(has_impulse, count, candle_mult, vol_mult, ago):
    return {
        "has_impulse":      has_impulse,
        "impulse_candles":  count,
        "max_candle_mult":  round(candle_mult, 2),
        "max_vol_mult":     round(vol_mult, 2),
        "last_impulse_ago": ago,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  3. ОПРЕДЕЛЕНИЕ ДИАПАЗОНА
# ══════════════════════════════════════════════════════════════════════════════

def detect_range(df: pd.DataFrame) -> dict:
    """
    Находит диапазон по последним 30–50 свечам.

    Алгоритм:
    - Кластеризует локальные максимумы → верхняя граница
    - Кластеризует локальные минимумы → нижняя граница
    - Проверяет минимум RANGE_MIN_TOUCHES касаний с каждой стороны
    - Считает ширину диапазона

    Возвращает dict:
        range_high      float
        range_low       float
        width_pct       float (%)
        top_touches     int
        bot_touches     int
        is_valid        bool (касания >= MIN и ширина >= MIN)
    """
    try:
        highs  = df["high"].values
        lows   = df["low"].values
        closes = df["close"].values
        price  = float(closes[-1])
        n      = len(df)
        w      = 2

        # Локальные максимумы и минимумы
        local_highs = [highs[i] for i in range(w, n-w)
                       if all(highs[i] >= highs[i-j] for j in range(1,w+1)) and
                          all(highs[i] >= highs[i+j] for j in range(1,w+1))]
        local_lows  = [lows[i]  for i in range(w, n-w)
                       if all(lows[i] <= lows[i-j] for j in range(1,w+1)) and
                          all(lows[i] <= lows[i+j] for j in range(1,w+1))]

        if not local_highs or not local_lows:
            return _range_result(price * 1.02, price * 0.98, price, 0, 0, False)

        # Верхняя граница — кластер верхних максимумов
        range_high = float(np.percentile(local_highs, 75))
        range_low  = float(np.percentile(local_lows, 25))

        if range_high <= range_low:
            return _range_result(price * 1.02, price * 0.98, price, 0, 0, False)

        tolerance = (range_high - range_low) * 0.15

        # Считаем касания
        top_touches = sum(1 for h in local_highs if abs(h - range_high) <= tolerance)
        bot_touches = sum(1 for l in local_lows  if abs(l - range_low)  <= tolerance)

        width_pct = (range_high - range_low) / price * 100

        is_valid = (
            top_touches >= RANGE_MIN_TOUCHES and
            bot_touches >= RANGE_MIN_TOUCHES and
            width_pct   >= RANGE_MIN_WIDTH_PCT * 100
        )

        return _range_result(range_high, range_low, price, top_touches, bot_touches, is_valid)

    except Exception as e:
        logger.debug(f"detect_range: {e}")
        return _range_result(0, 0, 0, 0, 0, False)


def _range_result(high, low, price, top_touches, bot_touches, is_valid):
    width_pct = (high - low) / price * 100 if price > 0 else 0
    return {
        "range_high":   round(high, 8),
        "range_low":    round(low, 8),
        "width_pct":    round(width_pct, 2),
        "top_touches":  top_touches,
        "bot_touches":  bot_touches,
        "is_valid":     is_valid,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  4. ПОЗИЦИЯ ЦЕНЫ
# ══════════════════════════════════════════════════════════════════════════════

def detect_price_position(price: float, range_high: float, range_low: float) -> dict:
    """
    Определяет где цена находится внутри диапазона.

    top    — верхние 25% (RANGE_TOP_ZONE..1.0)
    bottom — нижние 25%  (0..RANGE_BOT_ZONE)
    middle — центр       (RANGE_BOT_ZONE..RANGE_TOP_ZONE)

    Возвращает dict:
        position    "top" / "middle" / "bottom"
        position_pct  0–100 (0 = дно диапазона, 100 = верх)
    """
    try:
        span = range_high - range_low
        if span <= 0:
            return {"position": "middle", "position_pct": 50}

        pct = (price - range_low) / span  # 0.0 – 1.0

        if pct >= RANGE_TOP_ZONE:
            position = "top"
        elif pct <= RANGE_BOT_ZONE:
            position = "bottom"
        else:
            position = "middle"

        return {
            "position":     position,
            "position_pct": round(pct * 100, 1),
        }
    except Exception:
        return {"position": "middle", "position_pct": 50}


# ══════════════════════════════════════════════════════════════════════════════
#  5 + 6 + 7. BB SQUEEZE, ОБЪЁМ, ДЛИТЕЛЬНОСТЬ
# ══════════════════════════════════════════════════════════════════════════════

def detect_volume_accumulation(df: pd.DataFrame) -> dict:
    """
    Объём растёт пока цена стоит = накопление = риск пробоя.

    Возвращает dict:
        is_accumulation bool
        vol_ratio       объём последних 5 свечей / предыдущих 5
        price_range_pct диапазон цены за последние 10 свечей (%)
    """
    try:
        close  = df["close"].values
        volume = df["volume"].values

        if len(volume) < 15:
            return {"is_accumulation": False, "vol_ratio": 1.0, "price_range_pct": 0}

        vol_recent = float(np.mean(volume[-5:]))
        vol_prev   = float(np.mean(volume[-10:-5]))
        vol_ratio  = vol_recent / vol_prev if vol_prev > 0 else 1.0

        price_range = (max(close[-10:]) - min(close[-10:])) / close[-1] * 100

        # Объём растёт но цена не движется = накопление
        is_accumulation = vol_ratio >= VOLUME_GROW_RATIO and price_range < 3.0

        return {
            "is_accumulation": is_accumulation,
            "vol_ratio":       round(vol_ratio, 2),
            "price_range_pct": round(price_range, 2),
        }
    except Exception:
        return {"is_accumulation": False, "vol_ratio": 1.0, "price_range_pct": 0}


# ══════════════════════════════════════════════════════════════════════════════
#  FVG — FAIR VALUE GAP
# ══════════════════════════════════════════════════════════════════════════════

def detect_fvg(df: pd.DataFrame) -> dict:
    """
    Ищет незакрытые Fair Value Gap в последних FVG_LOOKBACK свечах.

    FVG = три свечи где:
        Бычий FVG: low[i+2] > high[i-1]  — дыра между тенями
        Медвежий FVG: high[i+2] < low[i-1] — дыра между тенями

    Незакрытый = цена ещё не вернулась в эту зону.

    Возвращает dict:
        bull_fvgs       список бычьих FVG [{top, bottom, age}]
        bear_fvgs       список медвежьих FVG [{top, bottom, age}]
        nearest_bull    ближайший бычий FVG снизу (магнит вниз)
        nearest_bear    ближайший медвежий FVG сверху (магнит вверх)
        danger_below    bool — опасный FVG снизу (в пределах 3%)
        danger_above    bool — опасный FVG сверху (в пределах 3%)
        risk_score      0–3 (сколько опасных FVG рядом)
    """
    try:
        highs  = df["high"].values
        lows   = df["low"].values
        closes = df["close"].values
        n      = len(df)
        price  = float(closes[-1])

        lookback_start = max(0, n - FVG_LOOKBACK - 2)
        bull_fvgs = []
        bear_fvgs = []

        for i in range(lookback_start + 1, n - 1):
            age = n - 1 - i  # свечей назад

            # Бычий FVG: дыра между high[i-1] и low[i+1]
            if lows[i + 1] > highs[i - 1]:
                fvg_bottom = float(highs[i - 1])
                fvg_top    = float(lows[i + 1])
                # Проверяем что FVG ещё не закрыт (цена не входила в зону)
                recent_lows = lows[i + 1:]
                if len(recent_lows) == 0 or float(min(recent_lows)) > fvg_bottom:
                    if (fvg_top - fvg_bottom) / price >= FVG_MIN_SIZE_PCT:
                        bull_fvgs.append({
                            "top":    round(fvg_top, 8),
                            "bottom": round(fvg_bottom, 8),
                            "mid":    round((fvg_top + fvg_bottom) / 2, 8),
                            "size_pct": round((fvg_top - fvg_bottom) / price * 100, 3),
                            "age":    age,
                        })

            # Медвежий FVG: дыра между low[i-1] и high[i+1]
            if highs[i + 1] < lows[i - 1]:
                fvg_top    = float(lows[i - 1])
                fvg_bottom = float(highs[i + 1])
                recent_highs = highs[i + 1:]
                if len(recent_highs) == 0 or float(max(recent_highs)) < fvg_top:
                    if (fvg_top - fvg_bottom) / price >= FVG_MIN_SIZE_PCT:
                        bear_fvgs.append({
                            "top":    round(fvg_top, 8),
                            "bottom": round(fvg_bottom, 8),
                            "mid":    round((fvg_top + fvg_bottom) / 2, 8),
                            "size_pct": round((fvg_top - fvg_bottom) / price * 100, 3),
                            "age":    age,
                        })

        # Ближайший бычий FVG снизу от цены (магнит вниз)
        bull_below = [f for f in bull_fvgs if f["mid"] < price]
        nearest_bull = min(bull_below, key=lambda x: price - x["mid"]) if bull_below else None

        # Ближайший медвежий FVG сверху от цены (магнит вверх)
        bear_above = [f for f in bear_fvgs if f["mid"] > price]
        nearest_bear = min(bear_above, key=lambda x: x["mid"] - price) if bear_above else None

        # Опасность: FVG в пределах FVG_DANGER_DIST_PCT от цены
        danger_below = (
            nearest_bull is not None and
            (price - nearest_bull["mid"]) / price < FVG_DANGER_DIST_PCT
        )
        danger_above = (
            nearest_bear is not None and
            (nearest_bear["mid"] - price) / price < FVG_DANGER_DIST_PCT
        )

        risk_score = int(danger_below) + int(danger_above) + (
            1 if (nearest_bull or nearest_bear) else 0
        )

        return {
            "bull_fvgs":     bull_fvgs,
            "bear_fvgs":     bear_fvgs,
            "nearest_bull":  nearest_bull,
            "nearest_bear":  nearest_bear,
            "danger_below":  danger_below,
            "danger_above":  danger_above,
            "risk_score":    min(risk_score, 3),
            "total_fvgs":    len(bull_fvgs) + len(bear_fvgs),
        }

    except Exception as e:
        logger.debug(f"detect_fvg: {e}")
        return {
            "bull_fvgs": [], "bear_fvgs": [],
            "nearest_bull": None, "nearest_bear": None,
            "danger_below": False, "danger_above": False,
            "risk_score": 0, "total_fvgs": 0,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  SESSION FILTER — ТОРГОВЫЕ СЕССИИ
# ══════════════════════════════════════════════════════════════════════════════

def detect_session(tf: str) -> dict:
    """
    Определяет текущую торговую сессию по UTC времени.
    Актуально только для внутридневных таймфреймов (30м, 1ч).

    Возвращает dict:
        current_session  "london" / "new_york" / "overlap" / "asia" / "off"
        is_active        bool — активная сессия (London или NY)
        is_asia          bool — тихая азиатская сессия
        is_overlap       bool — перекрытие London+NY (самый опасный период)
        hour_utc         текущий час UTC
        grid_risk        "low" / "medium" / "high"
        comment          текстовое описание
    """
    try:
        now_utc  = datetime.now(timezone.utc)
        hour     = now_utc.hour
        weekday  = now_utc.weekday()  # 0=Monday, 6=Sunday

        # Выходные — рынок тихий
        if weekday >= 5:
            return _session_result("off", hour, "weekend")

        # Для дневного и 4ч таймфреймов сессия не критична
        if tf not in SESSION_RELEVANT_TF:
            return _session_result("any", hour, "tf_irrelevant")

        # Определяем сессию
        lon_start, lon_end = SESSIONS["london"]
        ny_start,  ny_end  = SESSIONS["new_york"]
        ovl_start, ovl_end = SESSIONS["overlap"]
        asia_start, asia_end = SESSIONS["asia"]

        is_london   = lon_start  <= hour < lon_end
        is_ny       = ny_start   <= hour < ny_end
        is_overlap  = ovl_start  <= hour < ovl_end
        is_asia     = asia_start <= hour < asia_end

        if is_overlap:
            session   = "overlap"
            grid_risk = "high"
            comment   = f"⚠️ Перекрытие London+NY ({hour}:00 UTC) — высокая волатильность"
        elif is_london:
            session   = "london"
            grid_risk = "medium"
            comment   = f"🇬🇧 Лондонская сессия ({hour}:00 UTC)"
        elif is_ny:
            session   = "new_york"
            grid_risk = "medium"
            comment   = f"🇺🇸 Нью-Йоркская сессия ({hour}:00 UTC)"
        elif is_asia:
            session   = "asia"
            grid_risk = "low"
            comment   = f"🌏 Азиатская сессия ({hour}:00 UTC) — тихий рынок"
        else:
            session   = "off"
            grid_risk = "low"
            comment   = f"😴 Межсессионное время ({hour}:00 UTC)"

        return _session_result(session, hour, comment, grid_risk)

    except Exception as e:
        logger.debug(f"detect_session: {e}")
        return _session_result("any", 0, "error")


def _session_result(session, hour, comment, grid_risk="low"):
    return {
        "current_session": session,
        "is_active":       session in ("london", "new_york", "overlap"),
        "is_asia":         session == "asia",
        "is_overlap":      session == "overlap",
        "hour_utc":        hour,
        "grid_risk":       grid_risk,
        "comment":         comment,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  DISPLACEMENT ДЕТЕКТОР
# ══════════════════════════════════════════════════════════════════════════════

def detect_displacement(df: pd.DataFrame) -> dict:
    """
    Ищет displacement — большую импульсную свечу которая часто создаёт FVG
    и сигнализирует о входе крупных игроков.

    Displacement отличается от обычного импульса:
    - Свеча > DISPLACEMENT_MULT × средней
    - Закрытие близко к экстремуму (тело > 60% свечи)
    - Создаёт направление: бычий или медвежий

    Возвращает dict:
        has_displacement    bool
        direction           "bull" / "bear" / None
        candles_ago         сколько свечей назад
        size_mult           насколько больше средней
        is_recent           bool (последние 5 свечей)
    """
    try:
        opens  = df["open"].values
        closes = df["close"].values
        highs  = df["high"].values
        lows   = df["low"].values
        n      = len(df)

        if n < 20:
            return {"has_displacement": False, "direction": None,
                    "candles_ago": 999, "size_mult": 1.0, "is_recent": False}

        # Средняя величина свечи за предыдущие 20 свечей
        bodies  = np.abs(closes[:-DISPLACEMENT_LOOKBACK] - opens[:-DISPLACEMENT_LOOKBACK])
        avg_body = float(np.mean(bodies)) if len(bodies) > 0 else 0.001

        best_mult  = 1.0
        best_ago   = 999
        best_dir   = None

        for i in range(n - DISPLACEMENT_LOOKBACK, n):
            body      = abs(closes[i] - opens[i])
            candle_rng = highs[i] - lows[i]
            if candle_rng == 0:
                continue

            body_ratio = body / candle_rng
            size_mult  = body / avg_body if avg_body > 0 else 1.0

            if size_mult >= DISPLACEMENT_MULT and body_ratio >= 0.6:
                ago = n - 1 - i
                if size_mult > best_mult:
                    best_mult = size_mult
                    best_ago  = ago
                    best_dir  = "bull" if closes[i] > opens[i] else "bear"

        has = best_dir is not None

        return {
            "has_displacement": has,
            "direction":        best_dir,
            "candles_ago":      best_ago,
            "size_mult":        round(best_mult, 2),
            "is_recent":        best_ago <= 5 if has else False,
        }

    except Exception as e:
        logger.debug(f"detect_displacement: {e}")
        return {"has_displacement": False, "direction": None,
                "candles_ago": 999, "size_mult": 1.0, "is_recent": False}


# ══════════════════════════════════════════════════════════════════════════════
#  LIQUIDITY SWEEP
# ══════════════════════════════════════════════════════════════════════════════

def detect_liquidity_sweep(df: pd.DataFrame) -> dict:
    """
    Определяет был ли недавно Liquidity Sweep — ложный пробой
    очевидного уровня с возвратом цены.

    Алгоритм:
    1. Находим "очевидные" уровни ликвидности — локальные хаи/лои
       с минимум SWEEP_MIN_TOUCHES касаниями (туда стекаются стопы)
    2. Проверяем последние 5 свечей — пробивала ли цена уровень
       тенью (wick) но закрылась обратно
    3. Если да — это sweep. Smart Money выбили стопы и теперь
       готовы к движению в обратную сторону

    Типы:
        bull_sweep — выбили лои (стопы лонгистов) → ожидаем рост
        bear_sweep — выбили хаи (стопы шортистов) → ожидаем падение

    Для Grid бота:
        Свежий sweep (1-3 свечи назад) = опасно, сигнал к движению
        Sweep 4-10 свечей назад = уже отыгран, можно осторожно

    Возвращает dict:
        has_sweep       bool
        sweep_type      "bull_sweep" / "bear_sweep" / None
        candles_ago     int
        swept_level     float — уровень который был пробит
        return_pct      float — насколько % цена вернулась
        is_fresh        bool — свежий (≤ 3 свечи назад)
        is_dangerous    bool — свежий sweep = Grid опасен
    """
    try:
        highs  = df["high"].values
        lows   = df["low"].values
        opens  = df["open"].values
        closes = df["close"].values
        n      = len(df)

        if n < SWEEP_LOOKBACK + 5:
            return _sweep_result(False, None, 999, 0, 0)

        # Ищем уровни ликвидности в исторической части (не последние 5 свечей)
        hist_end   = n - 5
        hist_start = max(0, hist_end - SWEEP_LOOKBACK)

        hist_highs = highs[hist_start:hist_end]
        hist_lows  = lows[hist_start:hist_end]

        # Кластеризуем локальные максимумы → уровни сопротивления (bear liquidity)
        local_highs = []
        local_lows  = []
        w = 2
        for i in range(w, len(hist_highs) - w):
            if all(hist_highs[i] >= hist_highs[i-j] for j in range(1, w+1)) and \
               all(hist_highs[i] >= hist_highs[i+j] for j in range(1, w+1)):
                local_highs.append(float(hist_highs[i]))
            if all(hist_lows[i] <= hist_lows[i-j] for j in range(1, w+1)) and \
               all(hist_lows[i] <= hist_lows[i+j] for j in range(1, w+1)):
                local_lows.append(float(hist_lows[i]))

        # Фильтруем уровни с минимум SWEEP_MIN_TOUCHES касаниями
        def get_liquid_levels(levels: list[float]) -> list[float]:
            if not levels:
                return []
            result = []
            sorted_lvls = sorted(set(levels))
            for lvl in sorted_lvls:
                touches = sum(
                    1 for l in levels
                    if abs(l - lvl) / max(lvl, 0.0001) < SWEEP_TOUCH_THRESH
                )
                if touches >= SWEEP_MIN_TOUCHES:
                    result.append(lvl)
            return result

        liquid_highs = get_liquid_levels(local_highs)
        liquid_lows  = get_liquid_levels(local_lows)

        # Проверяем последние 5 свечей на sweep
        best_sweep_type  = None
        best_candles_ago = 999
        best_level       = 0.0
        best_return_pct  = 0.0

        for i in range(n - 5, n):
            candles_ago = n - 1 - i
            candle_high = float(highs[i])
            candle_low  = float(lows[i])
            candle_close = float(closes[i])
            candle_open  = float(opens[i])

            # Bear sweep: свеча пробила уровень сопротивления тенью
            # но закрылась ниже уровня
            for lvl in liquid_highs:
                if candle_high > lvl * (1 + SWEEP_TOUCH_THRESH):
                    # Тень пробила уровень
                    if candle_close < lvl and candle_open < lvl:
                        # Закрылась ниже — это sweep
                        return_pct = (candle_high - candle_close) / candle_high
                        if return_pct >= SWEEP_RETURN_PCT:
                            if candles_ago < best_candles_ago:
                                best_sweep_type  = "bear_sweep"
                                best_candles_ago = candles_ago
                                best_level       = lvl
                                best_return_pct  = round(return_pct * 100, 3)

            # Bull sweep: свеча пробила уровень поддержки тенью
            # но закрылась выше уровня
            for lvl in liquid_lows:
                if candle_low < lvl * (1 - SWEEP_TOUCH_THRESH):
                    if candle_close > lvl and candle_open > lvl:
                        return_pct = (candle_close - candle_low) / candle_close
                        if return_pct >= SWEEP_RETURN_PCT:
                            if candles_ago < best_candles_ago:
                                best_sweep_type  = "bull_sweep"
                                best_candles_ago = candles_ago
                                best_level       = lvl
                                best_return_pct  = round(return_pct * 100, 3)

        has_sweep = best_sweep_type is not None
        return _sweep_result(
            has_sweep, best_sweep_type,
            best_candles_ago, best_level, best_return_pct
        )

    except Exception as e:
        logger.debug(f"detect_liquidity_sweep: {e}")
        return _sweep_result(False, None, 999, 0, 0)


def _sweep_result(has_sweep, sweep_type, candles_ago, level, return_pct):
    is_fresh     = has_sweep and candles_ago <= 3
    is_dangerous = is_fresh
    return {
        "has_sweep":     has_sweep,
        "sweep_type":    sweep_type,
        "candles_ago":   candles_ago,
        "swept_level":   round(level, 8),
        "return_pct":    return_pct,
        "is_fresh":      is_fresh,
        "is_dangerous":  is_dangerous,
        "direction":     (
            "up"   if sweep_type == "bull_sweep" else
            "down" if sweep_type == "bear_sweep" else None
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  PREMIUM / DISCOUNT ЗОНЫ
# ══════════════════════════════════════════════════════════════════════════════

def detect_premium_discount(df: pd.DataFrame) -> dict:
    """
    Определяет находится ли цена в Premium или Discount зоне
    относительно последнего значимого диапазона (последние 50 свечей).

    Premium  (выше 50% диапазона) → дорого, продавцы активны
    Discount (ниже 50% диапазона) → дёшево, покупатели активны
    Equilibrium (45–55%)          → нейтральная зона

    Для Grid бота:
        Discount → лучше для лонгов (сетка вверх)
        Premium  → лучше для шортов или осторожно
        Equilibrium → нейтрально

    Возвращает dict:
        zone        "premium" / "discount" / "equilibrium"
        pct         0–100 (позиция внутри диапазона)
        high_50     верхняя граница 50-дневного диапазона
        low_50      нижняя граница 50-дневного диапазона
        eq_level    уровень равновесия (50%)
        grid_bias   "long" / "short" / "neutral"
    """
    try:
        lookback = min(50, len(df))
        df_r  = df.tail(lookback)
        high50 = float(df_r["high"].max())
        low50  = float(df_r["low"].min())
        price  = float(df["close"].iloc[-1])
        span   = high50 - low50

        if span <= 0:
            return {"zone": "equilibrium", "pct": 50,
                    "high_50": high50, "low_50": low50,
                    "eq_level": round((high50 + low50) / 2, 8),
                    "grid_bias": "neutral"}

        pct    = (price - low50) / span * 100
        eq     = round((high50 + low50) / 2, 8)

        if pct > 55:
            zone = "premium"
            bias = "short"
        elif pct < 45:
            zone = "discount"
            bias = "long"
        else:
            zone = "equilibrium"
            bias = "neutral"

        return {
            "zone":      zone,
            "pct":       round(pct, 1),
            "high_50":   round(high50, 8),
            "low_50":    round(low50, 8),
            "eq_level":  eq,
            "grid_bias": bias,
        }

    except Exception as e:
        logger.debug(f"detect_premium_discount: {e}")
        return {"zone": "equilibrium", "pct": 50,
                "high_50": 0, "low_50": 0,
                "eq_level": 0, "grid_bias": "neutral"}


# ══════════════════════════════════════════════════════════════════════════════
#  ГЛАВНАЯ ФУНКЦИЯ — ПОЛНЫЙ АНАЛИЗ
# ══════════════════════════════════════════════════════════════════════════════

def full_smart_analysis(df: pd.DataFrame, flat_candles: int,
                        bb_width_pct: float, tf: str = "") -> dict:
    """
    Запускает все анализы и возвращает:
        range_score     (0–10)
        breakout_risk   (0–10)
        recommendation  "grid" / "breakout" / "wait"
        market_structure
        impulse
        range_info
        price_position
        volume_acc
        grid_allowed    bool
        details         dict со всеми sub-результатами
    """

    price = float(df["close"].iloc[-1])

    # Запускаем все детекторы
    structure    = detect_market_structure(df)
    impulse      = detect_impulse(df)
    range_info   = detect_range(df)
    price_pos    = detect_price_position(price, range_info["range_high"], range_info["range_low"])
    vol_acc      = detect_volume_accumulation(df)
    fvg          = detect_fvg(df)
    session      = detect_session(tf)
    displacement = detect_displacement(df)
    prem_disc    = detect_premium_discount(df)
    sweep        = detect_liquidity_sweep(df)

    ms = structure["structure"]

    # ── RANGE SCORE (0–10) ─────────────────────────────────────────────────
    rs = 0

    if range_info["is_valid"]:
        rs += 2
    if ms == "range":
        rs += 2
    if range_info["width_pct"] >= 3.0:
        rs += 2
    if price_pos["position"] != "middle":
        rs += 1
    if not vol_acc["is_accumulation"]:
        rs += 1
    # +1 нет опасных FVG рядом
    if fvg["risk_score"] == 0:
        rs += 1
    # +1 цена в Discount (лучше для лонгов в сетке)
    if prem_disc["zone"] == "discount":
        rs += 1

    range_score = min(rs, 10)

    # ── BREAKOUT RISK (0–10) ───────────────────────────────────────────────
    br = 0

    if impulse["has_impulse"]:
        br += 2
    if bb_width_pct < 2.0:
        br += 2
    if vol_acc["is_accumulation"]:
        br += 2
    if flat_candles >= LONG_FLAT_CANDLES:
        br += 1
    if price_pos["position"] == "middle":
        br += 1
    # +2 опасные FVG рядом — цена пойдёт их закрывать
    br += min(fvg["risk_score"], 2)
    # +1 был displacement недавно
    if displacement["has_displacement"] and displacement["is_recent"]:
        br += 1
    # +1 активная сессия с высоким риском (London/NY overlap)
    if session["is_overlap"]:
        br += 1
    # +2 свежий Liquidity Sweep — Smart Money выбили стопы, движение близко
    if sweep["is_fresh"]:
        br += 2
    elif sweep["has_sweep"]:
        br += 1

    breakout_risk = min(br, 10)

    # ── ЗАПРЕТЫ НА GRID ────────────────────────────────────────────────────
    grid_blocked_reasons = []

    if ms in ("uptrend", "downtrend"):
        grid_blocked_reasons.append(f"тренд ({ms})")
    if range_info["width_pct"] < 3.0:
        grid_blocked_reasons.append(f"диапазон {range_info['width_pct']:.1f}% < 3%")
    if price_pos["position"] == "middle":
        grid_blocked_reasons.append("цена в середине")
    if impulse["has_impulse"] and impulse["last_impulse_ago"] <= 3:
        grid_blocked_reasons.append(f"импульс {impulse['last_impulse_ago']} свечей назад")
    # Блокируем если опасный FVG прямо рядом (< 1.5%)
    if fvg["danger_above"] or fvg["danger_below"]:
        dir_str = []
        if fvg["danger_above"]:
            nb = fvg["nearest_bear"]
            dir_str.append(f"FVG сверху {nb['mid']}" if nb else "FVG сверху")
        if fvg["danger_below"]:
            nb = fvg["nearest_bull"]
            dir_str.append(f"FVG снизу {nb['mid']}" if nb else "FVG снизу")
        grid_blocked_reasons.append(", ".join(dir_str))
    # Блокируем если displacement был совсем недавно (≤ 2 свечи)
    if displacement["has_displacement"] and displacement["candles_ago"] <= 2:
        grid_blocked_reasons.append(
            f"displacement {displacement['candles_ago']} свечи назад"
        )
    # Предупреждение об overlap (не блокируем, но понижаем скор)
    if session["is_overlap"] and tf in SESSION_RELEVANT_TF:
        grid_blocked_reasons.append("London+NY overlap — высокая волатильность")

    # Блокируем если свежий sweep — цена уже готова к движению
    if sweep["is_dangerous"]:
        sw_dir = "вверх" if sweep["direction"] == "up" else "вниз"
        grid_blocked_reasons.append(
            f"Liquidity Sweep {sweep['candles_ago']} св. назад → движение {sw_dir}"
        )

    grid_allowed = len(grid_blocked_reasons) == 0

    # ── РЕКОМЕНДАЦИЯ ───────────────────────────────────────────────────────
    if grid_allowed and range_score >= 7 and breakout_risk <= 4:
        recommendation = "grid"
    elif breakout_risk >= 6:
        recommendation = "breakout"
    else:
        recommendation = "wait"

    return {
        "range_score":      range_score,
        "breakout_risk":    breakout_risk,
        "recommendation":   recommendation,
        "grid_allowed":     grid_allowed,
        "grid_blocked":     grid_blocked_reasons,
        "market_structure": ms,
        "trend_strength":   structure["trend_strength"],
        "impulse":          impulse,
        "range_info":       range_info,
        "price_position":   price_pos,
        "volume_acc":       vol_acc,
        "fvg":              fvg,
        "session":          session,
        "displacement":     displacement,
        "premium_discount": prem_disc,
        "sweep":            sweep,
    }
