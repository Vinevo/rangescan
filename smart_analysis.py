"""
smart_analysis.py — Smart Money анализ v8

Модули:
1. Структура рынка (HH/HL/LH/LL)
2. Детектор импульса
3. Диапазон с касаниями
4. Позиция цены (top/middle/bottom)
5. Накопление объёма
6. FVG (Fair Value Gap)
7. Session filter
8. Displacement
9. Premium/Discount зоны
10. Liquidity Sweep
11. Weekly Open фильтр  ← NEW
"""

import logging
import numpy as np
import pandas as pd
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
#  НАСТРОЙКИ
# ══════════════════════════════════════════════════════════════════════════════

STRUCTURE_LOOKBACK    = 30
IMPULSE_MULT          = 1.5
VOLUME_IMPULSE_MULT   = 1.5
RANGE_MIN_TOUCHES     = 2
RANGE_MIN_WIDTH_PCT   = 0.03
RANGE_TOP_ZONE        = 0.75
RANGE_BOT_ZONE        = 0.25
LONG_FLAT_CANDLES     = 20
VOLUME_GROW_RATIO     = 1.15

FVG_MIN_SIZE_PCT      = 0.002
FVG_LOOKBACK          = 30
FVG_DANGER_DIST_PCT   = 0.03

SESSIONS = {
    "london":   (7,  12),
    "new_york": (13, 17),
    "overlap":  (13, 16),
    "asia":     (0,   7),
}
SESSION_RELEVANT_TF   = {"30", "60"}

DISPLACEMENT_MULT     = 2.0
DISPLACEMENT_LOOKBACK = 15

SWEEP_LOOKBACK        = 30
SWEEP_TOUCH_THRESH    = 0.003
SWEEP_MIN_TOUCHES     = 2
SWEEP_RETURN_PCT      = 0.005

# Weekly Open
WEEKLY_OPEN_DANGER_PCT = 0.05   # цена > 5% от weekly open = риск возврата


# ══════════════════════════════════════════════════════════════════════════════
#  1. СТРУКТУРА РЫНКА
# ══════════════════════════════════════════════════════════════════════════════

def detect_market_structure(df: pd.DataFrame) -> dict:
    try:
        df_s   = df.tail(STRUCTURE_LOOKBACK).copy().reset_index(drop=True)
        highs  = df_s["high"].values
        lows   = df_s["low"].values
        n, w   = len(df_s), 2

        swing_highs, swing_lows = [], []
        for i in range(w, n - w):
            if all(highs[i] >= highs[i-j] for j in range(1,w+1)) and \
               all(highs[i] >= highs[i+j] for j in range(1,w+1)):
                swing_highs.append((i, highs[i]))
            if all(lows[i] <= lows[i-j] for j in range(1,w+1)) and \
               all(lows[i] <= lows[i+j] for j in range(1,w+1)):
                swing_lows.append((i, lows[i]))

        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return _struct("range", 0, 0, 0, 0)

        hh = sum(1 for i in range(1,len(swing_highs)) if swing_highs[i][1] > swing_highs[i-1][1])
        lh = sum(1 for i in range(1,len(swing_highs)) if swing_highs[i][1] < swing_highs[i-1][1])
        hl = sum(1 for i in range(1,len(swing_lows))  if swing_lows[i][1]  > swing_lows[i-1][1])
        ll = sum(1 for i in range(1,len(swing_lows))  if swing_lows[i][1]  < swing_lows[i-1][1])

        total = max(hh+lh+hl+ll, 1)
        up_score = (hh+hl)/total
        dn_score = (lh+ll)/total

        if hh >= 2 and hl >= 2 and up_score > 0.6:
            return _struct("uptrend",   hh, hl, lh, ll, up_score)
        elif lh >= 2 and ll >= 2 and dn_score > 0.6:
            return _struct("downtrend", hh, hl, lh, ll, dn_score)
        else:
            return _struct("range",     hh, hl, lh, ll, 1.0 - max(up_score, dn_score))
    except Exception as e:
        logger.debug(f"structure: {e}")
        return _struct("range", 0, 0, 0, 0)


def _struct(s, hh, hl, lh, ll, strength=0.5):
    return {"structure": s, "hh_count": hh, "hl_count": hl,
            "lh_count": lh, "ll_count": ll, "trend_strength": round(strength, 2)}


# ══════════════════════════════════════════════════════════════════════════════
#  2. ДЕТЕКТОР ИМПУЛЬСА
# ══════════════════════════════════════════════════════════════════════════════

def detect_impulse(df: pd.DataFrame) -> dict:
    try:
        close, open_ = df["close"].values, df["open"].values
        volume = df["volume"].values
        n = len(df)

        lookback = min(20, n - 10)
        if lookback < 5:
            return _imp(False, 0, 1.0, 1.0, 999)

        avg_candle = np.mean(np.abs(close[-n:-10] - open_[-n:-10]))
        avg_volume = np.mean(volume[-n:-10])
        if avg_candle == 0 or avg_volume == 0:
            return _imp(False, 0, 1.0, 1.0, 999)

        sizes   = np.abs(close[-10:] - open_[-10:])
        c_mults = sizes / avg_candle
        v_mults = volume[-10:] / avg_volume
        mask    = (c_mults >= IMPULSE_MULT) | (v_mults >= VOLUME_IMPULSE_MULT)
        count   = int(mask.sum())

        last_ago = 999
        for i in range(len(mask)-1, -1, -1):
            if mask[i]:
                last_ago = len(mask)-1-i
                break

        return _imp(count > 0, count, float(c_mults.max()), float(v_mults.max()), last_ago)
    except Exception as e:
        logger.debug(f"impulse: {e}")
        return _imp(False, 0, 1.0, 1.0, 999)


def _imp(has, count, cm, vm, ago):
    return {"has_impulse": has, "impulse_candles": count,
            "max_candle_mult": round(cm,2), "max_vol_mult": round(vm,2),
            "last_impulse_ago": ago}


# ══════════════════════════════════════════════════════════════════════════════
#  3. ДИАПАЗОН
# ══════════════════════════════════════════════════════════════════════════════

def detect_range(df: pd.DataFrame) -> dict:
    try:
        highs  = df["high"].values
        lows   = df["low"].values
        price  = float(df["close"].values[-1])
        n, w   = len(df), 2

        lh = [highs[i] for i in range(w,n-w)
              if all(highs[i]>=highs[i-j] for j in range(1,w+1)) and
                 all(highs[i]>=highs[i+j] for j in range(1,w+1))]
        ll = [lows[i]  for i in range(w,n-w)
              if all(lows[i]<=lows[i-j]  for j in range(1,w+1)) and
                 all(lows[i]<=lows[i+j]  for j in range(1,w+1))]

        if not lh or not ll:
            return _rng(price*1.02, price*0.98, price, 0, 0, False)

        rh = float(np.percentile(lh, 75))
        rl = float(np.percentile(ll, 25))
        if rh <= rl:
            return _rng(price*1.02, price*0.98, price, 0, 0, False)

        tol = (rh - rl) * 0.15
        tt  = sum(1 for h in lh if abs(h-rh) <= tol)
        bt  = sum(1 for l in ll if abs(l-rl) <= tol)
        wp  = (rh-rl)/price*100
        valid = tt >= RANGE_MIN_TOUCHES and bt >= RANGE_MIN_TOUCHES and wp >= RANGE_MIN_WIDTH_PCT*100

        return _rng(rh, rl, price, tt, bt, valid)
    except Exception as e:
        logger.debug(f"range: {e}")
        return _rng(0, 0, 0, 0, 0, False)


def _rng(high, low, price, tt, bt, valid):
    wp = (high-low)/price*100 if price > 0 else 0
    return {"range_high": round(high,8), "range_low": round(low,8),
            "width_pct": round(wp,2), "top_touches": tt,
            "bot_touches": bt, "is_valid": valid}


# ══════════════════════════════════════════════════════════════════════════════
#  4. ПОЗИЦИЯ ЦЕНЫ
# ══════════════════════════════════════════════════════════════════════════════

def detect_price_position(price: float, rh: float, rl: float) -> dict:
    try:
        span = rh - rl
        if span <= 0:
            return {"position": "middle", "position_pct": 50}
        pct = (price - rl) / span
        pos = "top" if pct >= RANGE_TOP_ZONE else ("bottom" if pct <= RANGE_BOT_ZONE else "middle")
        return {"position": pos, "position_pct": round(pct*100, 1)}
    except Exception:
        return {"position": "middle", "position_pct": 50}


# ══════════════════════════════════════════════════════════════════════════════
#  5. НАКОПЛЕНИЕ ОБЪЁМА
# ══════════════════════════════════════════════════════════════════════════════

def detect_volume_accumulation(df: pd.DataFrame) -> dict:
    try:
        close  = df["close"].values
        volume = df["volume"].values
        if len(volume) < 15:
            return {"is_accumulation": False, "vol_ratio": 1.0, "price_range_pct": 0}
        vr  = float(np.mean(volume[-5:])) / max(float(np.mean(volume[-10:-5])), 0.001)
        prng = (max(close[-10:]) - min(close[-10:])) / close[-1] * 100
        return {"is_accumulation": vr >= VOLUME_GROW_RATIO and prng < 3.0,
                "vol_ratio": round(vr,2), "price_range_pct": round(prng,2)}
    except Exception:
        return {"is_accumulation": False, "vol_ratio": 1.0, "price_range_pct": 0}


# ══════════════════════════════════════════════════════════════════════════════
#  6. FVG
# ══════════════════════════════════════════════════════════════════════════════

def detect_fvg(df: pd.DataFrame) -> dict:
    try:
        highs  = df["high"].values
        lows   = df["low"].values
        closes = df["close"].values
        n      = len(df)
        price  = float(closes[-1])
        start  = max(0, n - FVG_LOOKBACK - 2)
        bull_fvgs, bear_fvgs = [], []

        for i in range(start+1, n-1):
            age = n-1-i
            # Бычий FVG
            if lows[i+1] > highs[i-1]:
                fb, ft = float(highs[i-1]), float(lows[i+1])
                rl = lows[i+1:]
                if len(rl)==0 or float(min(rl)) > fb:
                    if (ft-fb)/price >= FVG_MIN_SIZE_PCT:
                        bull_fvgs.append({"top": round(ft,8), "bottom": round(fb,8),
                                          "mid": round((ft+fb)/2,8),
                                          "size_pct": round((ft-fb)/price*100,3), "age": age})
            # Медвежий FVG
            if highs[i+1] < lows[i-1]:
                ft, fb = float(lows[i-1]), float(highs[i+1])
                rh = highs[i+1:]
                if len(rh)==0 or float(max(rh)) < ft:
                    if (ft-fb)/price >= FVG_MIN_SIZE_PCT:
                        bear_fvgs.append({"top": round(ft,8), "bottom": round(fb,8),
                                          "mid": round((ft+fb)/2,8),
                                          "size_pct": round((ft-fb)/price*100,3), "age": age})

        bull_below = [f for f in bull_fvgs if f["mid"] < price]
        bear_above = [f for f in bear_fvgs if f["mid"] > price]
        nb = min(bull_below, key=lambda x: price-x["mid"]) if bull_below else None
        na = min(bear_above, key=lambda x: x["mid"]-price) if bear_above else None

        db = nb is not None and (price-nb["mid"])/price < FVG_DANGER_DIST_PCT
        da = na is not None and (na["mid"]-price)/price < FVG_DANGER_DIST_PCT
        rs = min(int(db)+int(da)+(1 if nb or na else 0), 3)

        return {"bull_fvgs": bull_fvgs, "bear_fvgs": bear_fvgs,
                "nearest_bull": nb, "nearest_bear": na,
                "danger_below": db, "danger_above": da,
                "risk_score": rs, "total_fvgs": len(bull_fvgs)+len(bear_fvgs)}
    except Exception as e:
        logger.debug(f"fvg: {e}")
        return {"bull_fvgs":[], "bear_fvgs":[], "nearest_bull": None, "nearest_bear": None,
                "danger_below": False, "danger_above": False, "risk_score": 0, "total_fvgs": 0}


# ══════════════════════════════════════════════════════════════════════════════
#  7. SESSION FILTER
# ══════════════════════════════════════════════════════════════════════════════

def detect_session(tf: str) -> dict:
    try:
        now  = datetime.now(timezone.utc)
        hour = now.hour
        if now.weekday() >= 5:
            return _sess("off", hour, "weekend")
        if tf not in SESSION_RELEVANT_TF:
            return _sess("any", hour, "tf_irrelevant")
        if 13 <= hour < 16:
            return _sess("overlap",  hour, f"⚠️ Перекрытие London+NY ({hour}:00 UTC)", "high")
        if 7  <= hour < 12:
            return _sess("london",   hour, f"🇬🇧 Лондонская сессия ({hour}:00 UTC)", "medium")
        if 13 <= hour < 17:
            return _sess("new_york", hour, f"🇺🇸 Нью-Йоркская сессия ({hour}:00 UTC)", "medium")
        if 0  <= hour < 7:
            return _sess("asia",     hour, f"🌏 Азиатская сессия ({hour}:00 UTC)", "low")
        return _sess("off", hour, f"😴 Межсессионное время ({hour}:00 UTC)")
    except Exception:
        return _sess("any", 0, "error")


def _sess(s, h, comment, risk="low"):
    return {"current_session": s, "is_active": s in ("london","new_york","overlap"),
            "is_asia": s=="asia", "is_overlap": s=="overlap",
            "hour_utc": h, "grid_risk": risk, "comment": comment}


# ══════════════════════════════════════════════════════════════════════════════
#  8. DISPLACEMENT
# ══════════════════════════════════════════════════════════════════════════════

def detect_displacement(df: pd.DataFrame) -> dict:
    try:
        o, c = df["open"].values, df["close"].values
        h, l = df["high"].values, df["low"].values
        n    = len(df)
        if n < 20:
            return {"has_displacement": False, "direction": None,
                    "candles_ago": 999, "size_mult": 1.0, "is_recent": False}

        bodies   = np.abs(c[:-DISPLACEMENT_LOOKBACK] - o[:-DISPLACEMENT_LOOKBACK])
        avg_body = float(np.mean(bodies)) if len(bodies) > 0 else 0.001

        best_mult, best_ago, best_dir = 1.0, 999, None
        for i in range(n-DISPLACEMENT_LOOKBACK, n):
            body = abs(c[i]-o[i])
            rng  = h[i]-l[i]
            if rng == 0: continue
            if body/rng >= 0.6 and body/avg_body >= DISPLACEMENT_MULT:
                ago = n-1-i
                if body/avg_body > best_mult:
                    best_mult = body/avg_body
                    best_ago  = ago
                    best_dir  = "bull" if c[i]>o[i] else "bear"

        has = best_dir is not None
        return {"has_displacement": has, "direction": best_dir,
                "candles_ago": best_ago, "size_mult": round(best_mult,2),
                "is_recent": best_ago <= 5 if has else False}
    except Exception as e:
        logger.debug(f"displacement: {e}")
        return {"has_displacement": False, "direction": None,
                "candles_ago": 999, "size_mult": 1.0, "is_recent": False}


# ══════════════════════════════════════════════════════════════════════════════
#  9. LIQUIDITY SWEEP
# ══════════════════════════════════════════════════════════════════════════════

def detect_liquidity_sweep(df: pd.DataFrame) -> dict:
    try:
        highs  = df["high"].values
        lows   = df["low"].values
        opens  = df["open"].values
        closes = df["close"].values
        n      = len(df)

        if n < SWEEP_LOOKBACK + 5:
            return _sweep(False, None, 999, 0, 0)

        he, hs = n-5, max(0, n-5-SWEEP_LOOKBACK)
        hh = highs[hs:he]
        hl = lows[hs:he]

        def liquid(prices):
            if not prices: return []
            ps = sorted(set(prices))
            res = []
            for p in ps:
                t = sum(1 for x in prices if abs(x-p)/max(p,1e-9) < SWEEP_TOUCH_THRESH)
                if t >= SWEEP_MIN_TOUCHES: res.append(p)
            return res

        liq_h = liquid([hh[i] for i in range(2,len(hh)-2)
                        if all(hh[i]>=hh[i-j] for j in range(1,3)) and
                           all(hh[i]>=hh[i+j] for j in range(1,3))])
        liq_l = liquid([hl[i] for i in range(2,len(hl)-2)
                        if all(hl[i]<=hl[i-j] for j in range(1,3)) and
                           all(hl[i]<=hl[i+j] for j in range(1,3))])

        best_type, best_ago, best_lvl, best_ret = None, 999, 0.0, 0.0

        for i in range(n-5, n):
            ago = n-1-i
            ch, cl, cc, co = float(highs[i]), float(lows[i]), float(closes[i]), float(opens[i])
            for lvl in liq_h:
                if ch > lvl*(1+SWEEP_TOUCH_THRESH) and cc < lvl and co < lvl:
                    rp = (ch-cc)/ch
                    if rp >= SWEEP_RETURN_PCT and ago < best_ago:
                        best_type, best_ago, best_lvl, best_ret = "bear_sweep", ago, lvl, round(rp*100,3)
            for lvl in liq_l:
                if cl < lvl*(1-SWEEP_TOUCH_THRESH) and cc > lvl and co > lvl:
                    rp = (cc-cl)/cc
                    if rp >= SWEEP_RETURN_PCT and ago < best_ago:
                        best_type, best_ago, best_lvl, best_ret = "bull_sweep", ago, lvl, round(rp*100,3)

        return _sweep(best_type is not None, best_type, best_ago, best_lvl, best_ret)
    except Exception as e:
        logger.debug(f"sweep: {e}")
        return _sweep(False, None, 999, 0, 0)


def _sweep(has, stype, ago, lvl, ret):
    fresh = has and ago <= 3
    return {"has_sweep": has, "sweep_type": stype, "candles_ago": ago,
            "swept_level": round(lvl,8), "return_pct": ret,
            "is_fresh": fresh, "is_dangerous": fresh,
            "direction": ("up" if stype=="bull_sweep" else "down" if stype=="bear_sweep" else None)}


# ══════════════════════════════════════════════════════════════════════════════
#  10. PREMIUM / DISCOUNT
# ══════════════════════════════════════════════════════════════════════════════

def detect_premium_discount(df: pd.DataFrame) -> dict:
    try:
        lookback = min(50, len(df))
        dr    = df.tail(lookback)
        hi    = float(dr["high"].max())
        lo    = float(dr["low"].min())
        price = float(df["close"].iloc[-1])
        span  = hi - lo
        if span <= 0:
            return {"zone":"equilibrium","pct":50,"high_50":hi,"low_50":lo,
                    "eq_level":round((hi+lo)/2,8),"grid_bias":"neutral"}
        pct  = (price-lo)/span*100
        eq   = round((hi+lo)/2, 8)
        if pct > 55:   zone, bias = "premium",     "short"
        elif pct < 45: zone, bias = "discount",    "long"
        else:          zone, bias = "equilibrium", "neutral"
        return {"zone":zone,"pct":round(pct,1),"high_50":round(hi,8),
                "low_50":round(lo,8),"eq_level":eq,"grid_bias":bias}
    except Exception as e:
        logger.debug(f"prem_disc: {e}")
        return {"zone":"equilibrium","pct":50,"high_50":0,"low_50":0,
                "eq_level":0,"grid_bias":"neutral"}


# ══════════════════════════════════════════════════════════════════════════════
#  11. WEEKLY OPEN  ← NEW
# ══════════════════════════════════════════════════════════════════════════════

def detect_weekly_open(df: pd.DataFrame, tf: str) -> dict:
    """
    Определяет уровень открытия текущей недели и расстояние до него.

    Логика:
    - Для дневного и 4ч таймфреймов берём первую свечу недели из истории
    - Если цена далеко от Weekly Open (> WEEKLY_OPEN_DANGER_PCT) —
      повышенный риск возврата к этому уровню

    Возвращает dict:
        weekly_open     float — цена открытия текущей недели
        dist_pct        float — расстояние от текущей цены до WO (%)
        is_above        bool  — цена выше WO
        is_far          bool  — цена далеко от WO (риск возврата)
        risk_direction  "down" / "up" / None — куда может вернуться
        comment         str
    """
    try:
        # Weekly Open актуален для 4ч и 1д таймфреймов
        if tf not in ("240", "D"):
            return {"weekly_open": 0, "dist_pct": 0,
                    "is_above": False, "is_far": False,
                    "risk_direction": None, "comment": ""}

        opens  = df["open"].values
        closes = df["close"].values
        n      = len(df)
        price  = float(closes[-1])

        # Определяем сколько свечей в неделе
        candles_per_week = 42 if tf == "240" else 7  # 4ч: 42 свечи/неделю, 1д: 7

        # Берём открытие первой свечи текущей/прошлой недели
        # (первая свеча в окне последних candles_per_week свечей)
        week_start = max(0, n - candles_per_week)
        weekly_open = float(opens[week_start])

        if weekly_open <= 0:
            return {"weekly_open": 0, "dist_pct": 0,
                    "is_above": False, "is_far": False,
                    "risk_direction": None, "comment": ""}

        dist_pct    = (price - weekly_open) / weekly_open * 100
        abs_dist    = abs(dist_pct)
        is_above    = price > weekly_open
        is_far      = abs_dist > WEEKLY_OPEN_DANGER_PCT * 100

        if is_far:
            risk_direction = "down" if is_above else "up"
            arrow = "📉" if is_above else "📈"
            comment = (
                f"⚠️ Weekly Open: `{round(weekly_open,8)}` "
                f"({dist_pct:+.2f}%) — далеко, риск возврата {arrow}"
            )
        else:
            risk_direction = None
            comment = (
                f"✅ Weekly Open: `{round(weekly_open,8)}` "
                f"({dist_pct:+.2f}%) — близко, нейтрально"
            )

        return {
            "weekly_open":    round(weekly_open, 8),
            "dist_pct":       round(dist_pct, 2),
            "is_above":       is_above,
            "is_far":         is_far,
            "risk_direction": risk_direction,
            "comment":        comment,
        }

    except Exception as e:
        logger.debug(f"weekly_open: {e}")
        return {"weekly_open": 0, "dist_pct": 0,
                "is_above": False, "is_far": False,
                "risk_direction": None, "comment": ""}


# ══════════════════════════════════════════════════════════════════════════════
#  ГЛАВНАЯ ФУНКЦИЯ
# ══════════════════════════════════════════════════════════════════════════════

def full_smart_analysis(df: pd.DataFrame, flat_candles: int,
                        bb_width_pct: float, tf: str = "") -> dict:

    price = float(df["close"].iloc[-1])

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
    weekly_open  = detect_weekly_open(df, tf)

    ms = structure["structure"]

    # ── RANGE SCORE (0–10) ─────────────────────────────────────────────────
    rs = 0
    if range_info["is_valid"]:                     rs += 2
    if ms == "range":                              rs += 2
    if range_info["width_pct"] >= 3.0:             rs += 2
    if price_pos["position"] != "middle":          rs += 1
    if not vol_acc["is_accumulation"]:             rs += 1
    if fvg["risk_score"] == 0:                     rs += 1
    if prem_disc["zone"] == "discount":            rs += 1
    range_score = min(rs, 10)

    # ── BREAKOUT RISK (0–10) ───────────────────────────────────────────────
    br = 0
    if impulse["has_impulse"]:                                     br += 2
    if bb_width_pct < 2.0:                                         br += 2
    if vol_acc["is_accumulation"]:                                 br += 2
    if flat_candles >= LONG_FLAT_CANDLES:                          br += 1
    if price_pos["position"] == "middle":                          br += 1
    br += min(fvg["risk_score"], 2)
    if displacement["has_displacement"] and displacement["is_recent"]: br += 1
    if session["is_overlap"]:                                      br += 1
    if sweep["is_fresh"]:                                          br += 2
    elif sweep["has_sweep"]:                                       br += 1
    # Weekly Open далеко = риск возврата
    if weekly_open["is_far"]:                                      br += 1
    breakout_risk = min(br, 10)

    # ── ЗАПРЕТЫ НА GRID ────────────────────────────────────────────────────
    blocked = []
    if ms in ("uptrend", "downtrend"):
        blocked.append(f"тренд ({ms})")
    if range_info["width_pct"] < 3.0:
        blocked.append(f"диапазон {range_info['width_pct']:.1f}% < 3%")
    if price_pos["position"] == "middle":
        blocked.append("цена в середине")
    if impulse["has_impulse"] and impulse["last_impulse_ago"] <= 3:
        blocked.append(f"импульс {impulse['last_impulse_ago']} св. назад")
    if fvg["danger_above"] or fvg["danger_below"]:
        parts = []
        if fvg["danger_above"] and fvg["nearest_bear"]:
            parts.append(f"FVG сверху {fvg['nearest_bear']['mid']}")
        if fvg["danger_below"] and fvg["nearest_bull"]:
            parts.append(f"FVG снизу {fvg['nearest_bull']['mid']}")
        blocked.append(", ".join(parts))
    if displacement["has_displacement"] and displacement["candles_ago"] <= 2:
        blocked.append(f"displacement {displacement['candles_ago']} св. назад")
    if session["is_overlap"] and tf in SESSION_RELEVANT_TF:
        blocked.append("London+NY overlap")
    if sweep["is_dangerous"]:
        sw_dir = "вверх" if sweep["direction"]=="up" else "вниз"
        blocked.append(f"Liquidity Sweep {sweep['candles_ago']} св. назад → {sw_dir}")
    # Weekly Open блокирует Grid если цена далеко И это 4ч таймфрейм
    if weekly_open["is_far"] and tf == "240":
        blocked.append(
            f"Weekly Open {weekly_open['weekly_open']} "
            f"({weekly_open['dist_pct']:+.1f}%) — риск возврата"
        )

    grid_allowed = len(blocked) == 0

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
        "grid_blocked":     blocked,
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
        "weekly_open":      weekly_open,
    }
