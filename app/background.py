"""
Фоновые asyncio-задачи бота (запускаются из run.py). Вынесены из run.py, чтобы их можно
было проверить в e2e без импорта run.py (тот при импорте цепляет router к своему Dispatcher).

Каждая итерация цикла защищена try/except Exception: задачи никто не await-ит и не
перезапускает, поэтому одно необработанное исключение не должно убивать их навсегда.
CancelledError (BaseException) не перехватывается — штатная остановка проходит.
"""

import asyncio
import logging

import aiohttp
from aiogram import Bot
from aiogram.fsm.storage.memory import SimpleEventIsolation

from app.handlers import close_idle_dialogs

logger = logging.getLogger(__name__)

FSM_CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60
HEARTBEAT_TIMEOUT_SECONDS = 10
DIALOG_TIMEOUT_CHECK_INTERVAL_SECONDS = 5 * 60


async def cleanup_idle_fsm_locks(isolation: SimpleEventIsolation,
                                 interval_seconds: float = FSM_CLEANUP_INTERVAL_SECONDS) -> None:
    """
    aiogram хранит блокировки диалогов (SimpleEventIsolation._locks) в
    defaultdict, который растёт с каждым новым диалогом и никогда сам не
    очищается (в исходниках aiogram на этот счёт прямо стоит комментарий
    авторов: "TODO: Unused locks cleaner is needed"). Это утечка памяти при
    долгой работе процесса без перезапуска.

    Периодически удаляем locks, которые прямо сейчас никем не удерживаются
    (между проверкой `not lock.locked()` и удалением ключа нет ни одного
    `await`, поэтому в кооперативной модели asyncio интерливинг с новым
    обращением к этому же ключу исключён).

    Сами записи FSM живут в Postgres (PostgresStorage) и чистки не требуют:
    пустая запись удаляется хранилищем сразу при state.clear().

    `_locks` — приватная деталь реализации aiogram, а не публичный API,
    поэтому обращение к нему обёрнуто в защитный try/except: если структура
    изменится в новой версии aiogram, очистка просто перестанет работать (с
    предупреждением в логах), а не уронит бота.

    Любое другое исключение итерации логируется и не завершает задачу: следующая
    итерация пройдёт по расписанию (задачу никто не перезапускает, поэтому она не
    должна умирать).
    """
    while True:
        await asyncio.sleep(interval_seconds)

        try:
            locks = isolation._locks
            stale_lock_keys = [key for key, lock in locks.items() if not lock.locked()]

            for key in stale_lock_keys:
                del locks[key]
        except AttributeError as error:
            logger.warning('Очистка FSM-блокировок пропущена (несовместимая версия aiogram?): %s', error)
            continue
        except Exception:
            logger.exception('Очистка FSM-блокировок: непредвиденная ошибка, повтор на следующей итерации')
            continue

        logger.info(
            'Периодическая очистка FSM-блокировок: удалено %s (осталось %s)',
            len(stale_lock_keys), len(locks)
        )


async def send_heartbeat(url: str, interval_minutes: int) -> None:
    """
    Push-heartbeat во внешний dead-man's-switch (healthchecks.io / cronitor):
    если пинги перестают приходить (процесс упал, завис цикл событий, контейнер
    не поднялся), сервис сам алертит админа. Первый пинг — сразу при старте.

    Любая ошибка пинга только логируется warning'ом: недоступность сервиса
    мониторинга не должна влиять на работу бота.
    """
    timeout = aiohttp.ClientTimeout(total=HEARTBEAT_TIMEOUT_SECONDS)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            try:
                async with session.get(url) as response:
                    if response.status >= 400:
                        logger.warning('Heartbeat: сервис ответил HTTP %s', response.status)
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                logger.warning('Heartbeat не отправлен: %r', error)
            except Exception:
                logger.exception('Heartbeat: непредвиденная ошибка, повтор на следующей итерации')

            await asyncio.sleep(interval_minutes * 60)


async def close_idle_dialogs_periodically(bot: Bot, storage,
                                          interval_seconds: float = DIALOG_TIMEOUT_CHECK_INTERVAL_SECONDS) -> None:
    """
    Автозакрытие забытых запросов на диалог и активных диалогов (handlers.close_idle_dialogs:
    сроки DIALOG_WAITING_TIMEOUT / DIALOG_ACTIVE_TIMEOUT, уведомление обеих сторон, атомарный
    переход против параллельных действий клиента и админа). Первый проход — сразу при старте:
    за время простоя бота что-то могло просрочиться.

    Ошибка отдельного диалога изолирована внутри прохода; здесь ловится то, что уронило весь
    проход (например, недоступная БД), — задача продолжает работу со следующей итерации.
    """
    while True:
        try:
            closed = await close_idle_dialogs(bot, storage)

            if closed:
                logger.info('Автозакрытие диалогов: закрыто %s', closed)
        except Exception:
            logger.exception('Автозакрытие диалогов: непредвиденная ошибка, повтор на следующей итерации')

        await asyncio.sleep(interval_seconds)
