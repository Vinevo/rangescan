# 🤖 Bybit Flat Scanner v2 — Telegram Bot

Сканирует все ликвидные USDT пары на Bybit и уведомляет в Telegram когда монета входит в боковик. Идеально для Grid Bot.

## 🆕 Улучшения v2

| # | Улучшение | Описание |
|---|-----------|----------|
| ① | Фильтр ликвидности | Только монеты с объёмом > $5M/сутки |
| ② | Качество боковика | Скор 0–10, длительность, ложные пробои |
| ③ | Мультитаймфрейм | 30м подтверждается 1ч, 1ч подтверждается 1д |
| ④ | Растущий объём | Предупреждение если объём растёт (скоро пробой) |
| ⑤ | Алерт на выход | Уведомление когда боковик сломался — закрой Grid Bot |
| ⑥ | Диапазон Grid Bot | Границы и кол-во сеток рассчитываются автоматически |
| ⑦ | Умная дедупликация | Повтор сигнала не чаще 1 раза в 6 часов |
| ⑧ | Дневной отчёт | Каждый день в 09:00 UTC — сводка и топ-5 монет |

## 📊 Логика боковика

ADX < 20 + BB ширина < 4% + ATR < 3% + старший ТФ нейтральный + минимум 8 свечей подряд.

## 🚀 Запуск

```bash
pip install -r requirements.txt
cp .env.example .env   # заполни TELEGRAM_TOKEN и TELEGRAM_CHAT_ID
python main.py
```

## ☁️ Railway (бесплатно)
1. Залей в GitHub → railway.app → Deploy from GitHub
2. Variables: TELEGRAM_TOKEN + TELEGRAM_CHAT_ID
3. Settings → Networking → Generate Domain

## ☁️ Render (бесплатно)
1. render.com → Web Service → GitHub
2. Build: `pip install -r requirements.txt` | Start: `python main.py`
3. Добавь UptimeRobot для пингования каждые 5 мин

## ⚙️ Настройки (scanner.py)
```python
MIN_VOLUME_USDT  = 5_000_000   # мин. объём (можно поднять до $10M)
ADX_THRESHOLD    = 20
BB_SQUEEZE_RATIO = 0.04
ATR_RATIO_MAX    = 0.03
MIN_FLAT_CANDLES = 8
```
