import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.exceptions import TelegramRetryAfter

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


@bot.session.middleware()
async def retry_after_middleware(make_request, bot, method):
    attempts = 3

    for attempt in range(attempts):
        try:
            return await make_request(bot, method)

        except TelegramRetryAfter as error:
            if attempt == attempts - 1:
                logger.warning(
                    'Telegram rate limit after %s attempts: %s',
                    attempts,
                    error
                )
                raise

            logger.warning(
                'Telegram rate limit. Retry after %s seconds.',
                error.retry_after
            )

            await asyncio.sleep(error.retry_after)



dp = Dispatcher(
    storage=MemoryStorage(),
    events_isolation=SimpleEventIsolation()
)
dp.include_router(router)


async def main():
    logger.info('Bot is starting...')

    await init_db()

    logger.info('Database initialized')

    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())