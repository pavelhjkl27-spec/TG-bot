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
            method_name = method.__class__.__name__
            chat_id = getattr(method, 'chat_id', None)

            if attempt == attempts - 1:
                logger.warning(
                    'Telegram rate limit after %s attempts for %s (chat_id=%s): %s',
                    attempts,
                    method_name,
                    chat_id,
                    error
                )
                raise

            logger.warning(
                'Telegram rate limit for %s (chat_id=%s). Retry after %s seconds.',
                method_name,
                chat_id,
                error.retry_after
            )

            await asyncio.sleep(error.retry_after)



dp = Dispatcher(
    storage=MemoryStorage(),
    events_isolation=SimpleEventIsolation()
)
dp.include_router(router)


@dp.errors()
async def on_error(event):
    logger.exception(
        'Необработанное исключение при обработке апдейта %s: %s',
        event.update.update_id,
        event.exception
    )
    return True


async def main():
    logger.info('Bot is starting...')

    db_init_attempts = 5

    for attempt in range(db_init_attempts):
        try:
            await init_db()
            break
        except Exception as error:
            if attempt == db_init_attempts - 1:
                raise

            logger.warning(
                'Database not ready (attempt %s/%s): %s',
                attempt + 1, db_init_attempts, error
            )
            await asyncio.sleep(2)

    logger.info('Database initialized')

    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())