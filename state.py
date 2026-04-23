"""
state.py — Сохранение состояния бота в JSON файл.
При рестарте бот восстанавливает активные боковики и не шлёт дубли.
"""
import json
import logging
import os
import time

logger = logging.getLogger(__name__)

STATE_FILE = "state.json"
# Через сколько часов считаем запись устаревшей и удаляем
STATE_TTL_HOURS = 72


def _now() -> float:
    return time.time()


def load_state() -> tuple[dict, dict]:
    """
    Загружает active_flats и last_alerts из файла.
    Удаляет записи старше STATE_TTL_HOURS.
    Возвращает (active_flats, last_alerts).
    """
    if not os.path.exists(STATE_FILE):
        logger.info("📂 state.json не найден — начинаем с чистого листа")
        return {}, {}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        active_flats = data.get("active_flats", {})
        last_alerts  = data.get("last_alerts",  {})
        cutoff       = _now() - STATE_TTL_HOURS * 3600

        # Чистим устаревшие записи
        stale_flats = [k for k, v in active_flats.items() if v.get("since", 0) < cutoff]
        stale_alerts = [k for k, ts in last_alerts.items() if ts < cutoff]

        for k in stale_flats:
            del active_flats[k]
        for k in stale_alerts:
            del last_alerts[k]

        logger.info(
            f"📂 Состояние загружено: "
            f"активных боковиков={len(active_flats)}, "
            f"дедупликаций={len(last_alerts)} "
            f"(удалено устаревших: {len(stale_flats)+len(stale_alerts)})"
        )
        return active_flats, last_alerts

    except Exception as e:
        logger.error(f"Ошибка загрузки state.json: {e} — начинаем с чистого листа")
        return {}, {}


def save_state(active_flats: dict, last_alerts: dict) -> None:
    """
    Сохраняет состояние в JSON файл.
    Вызывается после каждого скана.
    """
    try:
        data = {
            "saved_at":    _now(),
            "active_flats": active_flats,
            "last_alerts":  last_alerts,
        }
        # Пишем через временный файл чтобы избежать corruption при сбое
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, STATE_FILE)

    except Exception as e:
        logger.error(f"Ошибка сохранения state.json: {e}")


def clear_state() -> None:
    """Полная очистка состояния (например для отладки)."""
    try:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        logger.info("🗑 state.json удалён")
    except Exception as e:
        logger.error(f"clear_state: {e}")
