import asyncio
import logging

import sentry_sdk
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sentry_sdk.integrations.logging import ignore_logger
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import ErrorEvent

from config import Config
from app.handlers import router
from app.database import run_migrations, UnstampedDatabaseError
from app.fsm_storage import PostgresStorage
from app.background import cleanup_idle_fsm_locks, send_heartbeat
from app.utils import notify_update_error


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s'
)

logger = logging.getLogger(__name__)

# Встроенная в sentry-sdk LoggingIntegration (включена по умолчанию) превращает
# каждую запись уровня ERROR и выше — в том числе `logger.exception` в `on_error`
# ниже — в событие Sentry с трейсбеком, а записи INFO+ прикладывает как breadcrumbs.
# Поэтому отдельный `capture_exception` не нужен: вывод в stdout остаётся прежним,
# дублей в Sentry нет. AsyncioIntegration дополнительно ловит исключения, которыми
# упали фоновые задачи (очистка FSM, heartbeat).
if Config.SENTRY_DSN:
    sentry_sdk.init(
        dsn=Config.SENTRY_DSN,
        integrations=[AsyncioIntegration()],
        send_default_pii=False,
    )
    # aiogram пишет ERROR "Failed to fetch updates" на каждую неудачную попытку
    # long-polling и сам переподключается с backoff — сетевой сбой дал бы серию
    # ложных алертов. В stdout эти записи остаются.
    ignore_logger('aiogram.dispatcher')
    logger.info('Sentry initialized')
else:
    logger.info('SENTRY_DSN не задан — Sentry отключён')


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



events_isolation = SimpleEventIsolation()

dp = Dispatcher(
    storage=PostgresStorage(),
    events_isolation=events_isolation
)
dp.include_router(router)


@dp.errors()
async def on_error(event: ErrorEvent, bot: Bot):
    logger.error(
        'Необработанное исключение при обработке апдейта %s: %s',
        event.update.update_id,
        event.exception,
        exc_info=event.exception
    )
    await notify_update_error(bot, event.update)
    return True


async def main():
    logger.info('Bot is starting...')

    db_init_attempts = 5

    for attempt in range(db_init_attempts):
        try:
            await run_migrations()
            break
        except UnstampedDatabaseError:
            # Повтор не поможет — нужна ручная разметка базы (MIGRATIONS.md).
            raise
        except Exception as error:
            if attempt == db_init_attempts - 1:
                raise

            logger.warning(
                'Database not ready (attempt %s/%s): %s',
                attempt + 1, db_init_attempts, error
            )
            await asyncio.sleep(2)

    logger.info('Database migrations applied')

    # Ссылка сохраняется на месте вызова: `dp.start_polling` ниже держит цикл
    # событий живым до остановки бота, так что задача не будет собрана GC раньше времени.
    cleanup_task = asyncio.create_task(cleanup_idle_fsm_locks(events_isolation))

    if Config.HEARTBEAT_URL:
        heartbeat_task = asyncio.create_task(
            send_heartbeat(Config.HEARTBEAT_URL, Config.HEARTBEAT_INTERVAL_MINUTES)
        )
        logger.info('Heartbeat enabled: every %s min', Config.HEARTBEAT_INTERVAL_MINUTES)
    else:
        logger.info('HEARTBEAT_URL не задан — heartbeat отключён')

    # Проверка токена и связи с Telegram до polling: `bot.me()` кэширует ответ, и start_polling его
    # переиспользует. По строке 'Bot is ready' scripts/deploy_common.sh (BOT_READY_MARKER) понимает,
    # что бот после деплоя действительно поднялся — меняете текст, поменяйте и там.
    me = await bot.me()
    logger.info('Bot is ready: @%s', me.username)

    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())