"""
backtest.py — Бэктест параметров сканера на исторических данных Bybit.

Запуск:
    python backtest.py                        # топ-20 монет, 90 дней, 1ч таймфрейм
    python backtest.py --symbol BTCUSDT       # одна монета
    python backtest.py --tf 240 --days 60     # 4ч таймфрейм, 60 дней
    python backtest.py --adx 25 --bb 0.05     # другие параметры

Что проверяет:
    - Сколько боковиков нашлось за период
    - Средняя длительность боковика в часах
    - % боковиков которые закончились пробоем (не просто затихли)
    - Лучший/худший таймфрейм
    - Сравнение разных пороговых значений ADX
"""

import argparse
import time
import sys
from datetime import datetime
from collections import defaultdict

import numpy as np
import pandas as pd
import pandas_ta as ta
from pybit.unified_trading import HTTP

# ══════════════════════════════════════════════════════════════════════════════
#  ПАРАМЕТРЫ ПО УМОЛЧАНИЮ (те же что в scanner.py)
# ══════════════════════════════════════════════════════════════════════════════
DEFAULT_ADX      = 20
DEFAULT_BB       = 0.04
DEFAULT_ATR      = 0.03
DEFAULT_DAYS     = 90
DEFAULT_TF       = "60"       # 1ч
DEFAULT_SYMBOLS  = 20         # топ-N по объёму
MIN_FLAT_CANDLES = 8
GRID_MIN_STEP_PCT = 0.003
GRID_MAX_COUNT    = 30
GRID_MIN_COUNT    = 5

TF_LABELS = {"30": "30м", "60": "1ч", "240": "4ч", "D": "1д"}
TF_MINUTES = {"30": 30, "60": 60, "240": 240, "D": 1440}

session = HTTP(testnet=False)


# ──────────────────────────────────────────────────────────────────────────────
#  ПОЛУЧЕНИЕ ДАННЫХ
# ──────────────────────────────────────────────────────────────────────────────

def get_top_symbols(n: int = 20) -> list[str]:
    """Топ-N USDT пар по объёму."""
    try:
        tickers = session.get_tickers(category="linear")
        pairs = [
            (t["symbol"], float(t.get("turnover24h", 0)))
            for t in tickers["result"]["list"]
            if t["symbol"].endswith("USDT")
        ]
        pairs.sort(key=lambda x: x[1], reverse=True)
        return [p[0] for p in pairs[:n]]
    except Exception as e:
        print(f"Ошибка получения символов: {e}")
        return ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def get_history(symbol: str, interval: str, days: int) -> pd.DataFrame | None:
    """
    Получаем исторические данные за N дней.
    Bybit отдаёт max 200 свечей за запрос — делаем несколько запросов.
    """
    tf_min   = TF_MINUTES.get(interval, 60)
    total    = int(days * 24 * 60 / tf_min)
    per_req  = 200
    all_data = []
    end_time = int(time.time() * 1000)

    requests_needed = (total // per_req) + 1

    for _ in range(requests_needed):
        try:
            resp = session.get_kline(
                category="linear",
                symbol=symbol,
                interval=interval,
                limit=per_req,
                end=end_time
            )
            batch = resp["result"]["list"]
            if not batch:
                break
            all_data.extend(batch)
            end_time = int(batch[-1][0]) - 1   # сдвигаем окно назад
            time.sleep(0.1)
        except Exception as e:
            print(f"  Ошибка запроса {symbol}: {e}")
            break

    if not all_data:
        return None

    df = pd.DataFrame(
        all_data,
        columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"]
    )
    df = df.astype({
        "open": float, "high": float, "low": float,
        "close": float, "volume": float
    })
    df["timestamp"] = df["timestamp"].astype(int)
    df = df.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    return df.tail(total)


# ──────────────────────────────────────────────────────────────────────────────
#  ДЕТЕКТОР БОКОВИКА (упрощённая версия из scanner.py)
# ──────────────────────────────────────────────────────────────────────────────

def detect_flat_window(df_window: pd.DataFrame, adx_thr: float, bb_ratio: float, atr_ratio_max: float) -> dict | None:
    """Проверяет одно окно свечей на боковик. Возвращает stats или None."""
    if len(df_window) < 30:
        return None

    close = df_window["close"]
    high  = df_window["high"]
    low   = df_window["low"]
    price = float(close.iloc[-1])

    try:
        adx_df = ta.adx(high, low, close, length=14)
        if adx_df is None or adx_df.empty:
            return None
        adx_val = float(adx_df.iloc[-1, 0])
        if adx_val >= adx_thr:
            return None

        bb_df = ta.bbands(close, length=20, std=2)
        if bb_df is None or bb_df.empty:
            return None
        bb_upper = float(bb_df["BBU_20_2.0"].iloc[-1])
        bb_lower = float(bb_df["BBL_20_2.0"].iloc[-1])
        bb_width = (bb_upper - bb_lower) / price
        if bb_width >= bb_ratio:
            return None

        atr_s = ta.atr(high, low, close, length=14)
        if atr_s is None or atr_s.empty:
            return None
        atr_val = float(atr_s.iloc[-1])
        if atr_val / price >= atr_ratio_max:
            return None

        # Длительность
        in_range = ((close >= bb_lower) & (close <= bb_upper)).tolist()
        flat_c = 0
        for v in reversed(in_range):
            if v:
                flat_c += 1
            else:
                break
        if flat_c < MIN_FLAT_CANDLES:
            return None

        # Диапазон Grid Bot
        range_low  = bb_lower * 0.998
        range_high = bb_upper * 1.002
        span       = range_high - range_low
        min_step   = price * GRID_MIN_STEP_PCT
        grid_step  = max(atr_val, min_step)
        grid_count = max(GRID_MIN_COUNT, min(GRID_MAX_COUNT, int(span / grid_step)))

        return {
            "price":       price,
            "adx":         round(adx_val, 2),
            "bb_width_pct":round(bb_width * 100, 2),
            "atr_pct":     round(atr_val / price * 100, 2),
            "flat_candles":flat_c,
            "range_low":   round(range_low, 8),
            "range_high":  round(range_high, 8),
            "range_pct":   round(span / price * 100, 2),
            "grid_count":  grid_count,
        }
    except Exception:
        return None


def measure_flat_duration(df: pd.DataFrame, start_idx: int, range_low: float, range_high: float) -> tuple[int, bool]:
    """
    От точки входа считаем сколько свечей цена оставалась в диапазоне.
    Возвращает (кол-во свечей, был_ли_пробой).
    """
    duration   = 0
    had_breakout = False

    for i in range(start_idx, len(df)):
        price = float(df["close"].iloc[i])
        high  = float(df["high"].iloc[i])
        low   = float(df["low"].iloc[i])

        if high > range_high * 1.01 or low < range_low * 0.99:
            had_breakout = True
            break
        duration += 1

    return duration, had_breakout


# ──────────────────────────────────────────────────────────────────────────────
#  ОСНОВНОЙ БЭКТЕСТ
# ──────────────────────────────────────────────────────────────────────────────

def backtest_symbol(
    symbol: str,
    df: pd.DataFrame,
    interval: str,
    adx_thr: float,
    bb_ratio: float,
    atr_max: float
) -> dict:
    """
    Прогоняем детектор по всей истории скользящим окном.
    """
    window    = 50
    step      = 5    # Шаг — каждые 5 свечей (не каждую, для скорости)
    flats     = []
    last_flat = -999  # Индекс последнего найденного боковика (дедупликация)

    for i in range(window, len(df) - 10, step):
        # Пропускаем если только что нашли боковик (не дублируем)
        if i - last_flat < MIN_FLAT_CANDLES:
            continue

        window_df = df.iloc[i - window:i].copy().reset_index(drop=True)
        stats = detect_flat_window(window_df, adx_thr, bb_ratio, atr_max)

        if stats is None:
            continue

        # Измеряем реальную длительность боковика начиная с этой точки
        duration_candles, breakout = measure_flat_duration(
            df, i, stats["range_low"], stats["range_high"]
        )

        tf_min = TF_MINUTES.get(interval, 60)
        ts     = int(df["timestamp"].iloc[i])
        dt_str = datetime.utcfromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")

        flats.append({
            "date":             dt_str,
            "adx":              stats["adx"],
            "bb_width_pct":     stats["bb_width_pct"],
            "atr_pct":          stats["atr_pct"],
            "flat_candles":     stats["flat_candles"],
            "range_pct":        stats["range_pct"],
            "grid_count":       stats["grid_count"],
            "duration_candles": duration_candles,
            "duration_hours":   round(duration_candles * tf_min / 60, 1),
            "had_breakout":     breakout,
        })

        last_flat = i

    return {
        "symbol":  symbol,
        "tf":      interval,
        "total":   len(flats),
        "flats":   flats,
    }


def print_results(results: list[dict], adx_thr: float, bb_ratio: float, atr_max: float, days: int):
    """Красивый вывод результатов бэктеста."""

    print("\n" + "═" * 62)
    print(f"  БЭКТЕСТ РЕЗУЛЬТАТЫ")
    print(f"  Параметры: ADX<{adx_thr} | BB<{bb_ratio*100:.1f}% | ATR<{atr_max*100:.1f}%")
    print(f"  Период: {days} дней")
    print("═" * 62)

    all_flats = []
    for r in results:
        all_flats.extend(r["flats"])

    if not all_flats:
        print("  ❌ Боковиков не найдено. Попробуй ослабить параметры.")
        return

    # Общая статистика
    total       = len(all_flats)
    breakouts   = sum(1 for f in all_flats if f["had_breakout"])
    avg_dur     = np.mean([f["duration_hours"] for f in all_flats])
    avg_range   = np.mean([f["range_pct"] for f in all_flats])
    avg_grids   = np.mean([f["grid_count"] for f in all_flats])

    print(f"\n  📊 ИТОГО боковиков найдено:  {total}")
    print(f"  📤 Закончились пробоем:      {breakouts} ({breakouts/total*100:.0f}%)")
    print(f"  ⏱  Средняя длительность:     {avg_dur:.1f} ч")
    print(f"  📐 Средний диапазон:          {avg_range:.2f}%")
    print(f"  # Среднее кол-во сеток:      {avg_grids:.0f}")

    # По символам
    print(f"\n  {'Монета':<14} {'Найдено':>8} {'Пробоев':>8} {'Ср.длит':>9} {'Ср.диап':>9}")
    print("  " + "-" * 52)

    by_symbol = defaultdict(list)
    for f in all_flats:
        pass  # нет символа в flat — берём из results

    for r in results:
        if not r["flats"]:
            continue
        sym     = r["symbol"]
        flats   = r["flats"]
        n       = len(flats)
        brk     = sum(1 for f in flats if f["had_breakout"])
        avg_h   = np.mean([f["duration_hours"] for f in flats])
        avg_rng = np.mean([f["range_pct"] for f in flats])
        print(f"  {sym:<14} {n:>8} {brk:>7}({brk/n*100:.0f}%) {avg_h:>7.1f}ч {avg_rng:>8.2f}%")

    # Топ-5 лучших боковиков (длинные + с пробоем)
    best = sorted(all_flats, key=lambda x: x["duration_hours"], reverse=True)[:5]
    print(f"\n  🏆 ТОП-5 САМЫХ ДЛИННЫХ БОКОВИКОВ:")
    print(f"  {'Дата':<18} {'ADX':>6} {'BB%':>6} {'Длит':>8} {'Диап':>7} {'Пробой':>7}")
    print("  " + "-" * 58)
    for f in best:
        brk = "✅ да" if f["had_breakout"] else "❌ нет"
        print(
            f"  {f['date']:<18} "
            f"{f['adx']:>6} "
            f"{f['bb_width_pct']:>5.2f}% "
            f"{f['duration_hours']:>7.1f}ч "
            f"{f['range_pct']:>6.2f}% "
            f"{brk:>8}"
        )

    print("\n" + "═" * 62)


def compare_adx_thresholds(symbol: str, df: pd.DataFrame, interval: str, days: int):
    """Сравниваем разные пороги ADX на одной монете."""
    thresholds = [15, 20, 25, 30]
    print(f"\n  📊 СРАВНЕНИЕ ПОРОГОВ ADX для {symbol} [{TF_LABELS.get(interval, interval)}]:")
    print(f"  {'ADX<':>6} {'Найдено':>8} {'Ср.длит':>9} {'Пробоев%':>10}")
    print("  " + "-" * 38)

    for thr in thresholds:
        r = backtest_symbol(symbol, df, interval, thr, DEFAULT_BB, DEFAULT_ATR)
        flats = r["flats"]
        if not flats:
            print(f"  {thr:>5}  {'0':>8}  {'—':>9}  {'—':>10}")
            continue
        avg_h = np.mean([f["duration_hours"] for f in flats])
        brk_pct = sum(1 for f in flats if f["had_breakout"]) / len(flats) * 100
        print(f"  {thr:>5}  {len(flats):>8}  {avg_h:>7.1f}ч  {brk_pct:>8.0f}%")


# ──────────────────────────────────────────────────────────────────────────────
#  ТОЧКА ВХОДА
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Bybit Flat Scanner — Бэктест")
    parser.add_argument("--symbol", type=str,   default=None,          help="Символ (напр. BTCUSDT)")
    parser.add_argument("--tf",     type=str,   default=DEFAULT_TF,    help="Таймфрейм: 30/60/240/D")
    parser.add_argument("--days",   type=int,   default=DEFAULT_DAYS,  help="Дней истории")
    parser.add_argument("--top",    type=int,   default=DEFAULT_SYMBOLS, help="Топ-N символов")
    parser.add_argument("--adx",    type=float, default=DEFAULT_ADX,   help="Порог ADX")
    parser.add_argument("--bb",     type=float, default=DEFAULT_BB,    help="Порог BB ширины")
    parser.add_argument("--atr",    type=float, default=DEFAULT_ATR,   help="Порог ATR")
    parser.add_argument("--compare-adx", action="store_true",          help="Сравнить пороги ADX")
    args = parser.parse_args()

    print(f"\n🔍 Bybit Flat Scanner — Бэктест")
    print(f"   Таймфрейм: {TF_LABELS.get(args.tf, args.tf)} | Период: {args.days} дней")
    print(f"   ADX<{args.adx} | BB<{args.bb*100:.1f}% | ATR<{args.atr*100:.1f}%\n")

    # Список символов
    if args.symbol:
        symbols = [args.symbol.upper()]
    else:
        print(f"📥 Получаем топ-{args.top} символов по объёму...")
        symbols = get_top_symbols(args.top)
        print(f"   {', '.join(symbols[:5])}{'...' if len(symbols) > 5 else ''}\n")

    results = []

    for sym in symbols:
        print(f"⏳ {sym}: загружаем {args.days} дней [{TF_LABELS.get(args.tf, args.tf)}]...", end=" ", flush=True)
        df = get_history(sym, args.tf, args.days)
        if df is None or len(df) < 50:
            print("нет данных")
            continue
        print(f"{len(df)} свечей", end=" → ", flush=True)

        result = backtest_symbol(sym, df, args.tf, args.adx, args.bb, args.atr)
        results.append(result)
        print(f"найдено {result['total']} боковиков")

        if args.compare_adx and len(symbols) == 1:
            compare_adx_thresholds(sym, df, args.tf, args.days)

    if results:
        print_results(results, args.adx, args.bb, args.atr, args.days)

        # Если одна монета — показываем сравнение ADX автоматически
        if len(symbols) == 1 and not args.compare_adx:
            compare_adx_thresholds(symbols[0], df, args.tf, args.days)

    print()


if __name__ == "__main__":
    main()
