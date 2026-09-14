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

FSM_CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60


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
    storage=MemoryStorage(),
    events_isolation=events_isolation
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


async def cleanup_idle_fsm_state(dispatcher: Dispatcher, isolation: SimpleEventIsolation) -> None:
    """
    aiogram хранит блокировки диалогов (SimpleEventIsolation._locks) и записи
    FSM (MemoryStorage.storage) в defaultdict, которые растут с каждым новым
    диалогом и никогда сами не очищаются (в исходниках aiogram на этот счёт
    прямо стоит комментарий авторов: "TODO: Unused locks cleaner is needed").
    Это утечка памяти при долгой работе процесса без перезапуска.

    Чистим периодически только то, что безопасно чистить:
      - locks, которые прямо сейчас никем не удерживаются (между проверкой
        `not lock.locked()` и удалением ключа нет ни одного `await`, поэтому
        в кооперативной модели asyncio интерливинг с новым обращением к
        этому же ключу исключён);
      - записи FSM без состояния и без данных — то есть полностью
        простаивающие диалоги, где нечего терять. Диалоги с незавершённым
        шагом формы (есть state и/или уже введённые данные) не трогаются.

    Оба атрибута — приватные детали реализации aiogram, а не публичный API,
    поэтому обращение к ним обёрнуто в защитные try/except: если структура
    изменится в новой версии aiogram, очистка просто перестанет работать (с
    предупреждением в логах), а не уронит бота.
    """
    while True:
        await asyncio.sleep(FSM_CLEANUP_INTERVAL_SECONDS)

        removed_locks = remaining_locks = None
        removed_records = remaining_records = None

        try:
            locks = isolation._locks
            stale_lock_keys = [key for key, lock in locks.items() if not lock.locked()]

            for key in stale_lock_keys:
                del locks[key]

            removed_locks = len(stale_lock_keys)
            remaining_locks = len(locks)
        except AttributeError as error:
            logger.warning('Очистка FSM-блокировок пропущена (несовместимая версия aiogram?): %s', error)

        try:
            storage = dispatcher.storage.storage
            idle_keys = [
                key for key, record in storage.items()
                if record.state is None and not record.data
            ]

            for key in idle_keys:
                del storage[key]

            removed_records = len(idle_keys)
            remaining_records = len(storage)
        except AttributeError as error:
            logger.warning('Очистка простаивающих FSM-записей пропущена (несовместимая версия aiogram?): %s', error)

        logger.info(
            'Периодическая очистка FSM: удалено locks=%s (осталось %s), удалено idle-записей=%s (осталось %s)',
            removed_locks, remaining_locks, removed_records, remaining_records
        )


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

    # Ссылка сохраняется на месте вызова: `dp.start_polling` ниже держит цикл
    # событий живым до остановки бота, так что задача не будет собрана GC раньше времени.
    cleanup_task = asyncio.create_task(cleanup_idle_fsm_state(dp, events_isolation))

    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())