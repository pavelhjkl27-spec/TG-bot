import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from config import Config
from app.handlers import router
from app.database import init_db


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s'
)

logger = logging.getLogger(__name__)


bot = Bot(
    token=Config.BOT_TOKEN,
    default=DefaultBotProperties(parse_mode='HTML')
)

dp = Dispatcher()
dp.include_router(router)


async def main():
    logger.info('Bot is starting...')

    await init_db()

    logger.info('Database initialized')

    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())