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
    logger.info("🚀 Bybit Flat Scanner v9 запущен")

    if not os.getenv("TELEGRAM_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID"):
        logger.error("❌ Не заданы TELEGRAM_TOKEN или TELEGRAM_CHAT_ID!")
        return

    set_refs(active_flats, daily_stats)
    keep_alive()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(scan_market,         "interval", minutes=5,  id="scan")
    scheduler.add_job(send_daily_summary,  "cron",     hour=9, minute=0, id="report")
    scheduler.add_job(flush_retry_queue,   "interval", minutes=3,  id="retry")
    scheduler.start()
    logger.info("⏰ Планировщик: скан 5м | отчёт 09:00 UTC | retry 3м")

    asyncio.ensure_future(poll_commands())
    logger.info("🎮 Команды: /status /active /pause /resume /help")

    logger.info("⏳ Ожидание 20 секунд перед первым сканом...")
    await asyncio.sleep(20)
    logger.info("🔍 Первый скан...")
    await scan_market()

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("Бот остановлен")


if __name__ == "__main__":
    asyncio.run(main())
