import asyncio
import logging
import os
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from keep_alive import keep_alive
from scanner import scan_market, send_daily_summary, active_flats, daily_stats
from notifier import flush_retry_queue
from bot_commands import poll_commands, set_refs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


async def main():
    logger.info("🚀 Bybit Flat Scanner v5 запущен")

    # Проверка переменных окружения
    if not os.getenv("TELEGRAM_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID"):
        logger.error("❌ Не заданы TELEGRAM_TOKEN или TELEGRAM_CHAT_ID!")
        logger.error("Создай .env файл или добавь переменные на Railway/Render")
        return

    # Передаём ссылки на данные сканера в обработчик команд
    set_refs(active_flats, daily_stats)

    # Keep-alive HTTP сервер (Railway/Render не засыпает)
    keep_alive()

    scheduler = AsyncIOScheduler()

    # Сканирование каждые 5 минут
    scheduler.add_job(
        lambda: asyncio.create_task(scan_market()),
        "interval", minutes=5, id="scan_job"
    )

    # Дневной отчёт в 09:00 UTC
    scheduler.add_job(
        lambda: asyncio.create_task(send_daily_summary()),
        "cron", hour=9, minute=0, id="daily_report"
    )

    # Flush retry очереди каждые 3 минуты
    scheduler.add_job(
        lambda: asyncio.create_task(flush_retry_queue()),
        "interval", minutes=3, id="retry_flush"
    )

    scheduler.start()
    logger.info("⏰ Планировщик: скан 5м | отчёт 09:00 UTC | retry 3м")

   # Задержка перед первым сканом — ждём пока контейнер полностью поднимется
    logger.info("⏳ Ожидание 15 секунд перед первым сканом...")
    await asyncio.sleep(15)
    logger.info("🔍 Первый скан...")
    await scan_market()

    # Запускаем polling Telegram команд в фоне
    asyncio.create_task(poll_commands())
    logger.info("🎮 Команды Telegram активны: /status /active /pause /resume /help")

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("Бот остановлен")


if __name__ == "__main__":
    asyncio.run(main())
