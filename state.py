import json
import logging
import os
import time

logger = logging.getLogger(__name__)

STATE_FILE = "state.json"
STATE_TTL_HOURS = 72


def _now() -> float:
    return time.time()


def load_state() -> tuple[dict, dict]:
    if not os.path.exists(STATE_FILE):
        logger.info("📂 state.json не найден — начинаем с чистого листа")
        return {}, {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        active_flats = data.get("active_flats", {})
        last_alerts  = data.get("last_alerts",  {})
        cutoff       = _now() - STATE_TTL_HOURS * 3600
        stale_flats  = [k for k, v in active_flats.items() if v.get("since", 0) < cutoff]
        stale_alerts = [k for k, ts in last_alerts.items() if ts < cutoff]
        for k in stale_flats:  del active_flats[k]
        for k in stale_alerts: del last_alerts[k]
        logger.info(f"📂 Состояние загружено: активных={len(active_flats)} дедупл.={len(last_alerts)}")
        return active_flats, last_alerts
    except Exception as e:
        logger.error(f"Ошибка загрузки state.json: {e}")
        return {}, {}


def save_state(active_flats: dict, last_alerts: dict) -> None:
    try:
        data = {"saved_at": _now(), "active_flats": active_flats, "last_alerts": last_alerts}
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logger.error(f"Ошибка сохранения state.json: {e}")
