from collections.abc import Mapping
from typing import Any

from aiogram.exceptions import DataNotDictLikeError
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey
from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.dialects.postgresql import JSONB, insert

from app.database import async_session_maker
from app.models import FsmStorage

KEY_CONSTRAINT = 'uq_fsm_storage_key'


def _key_filter(key: StorageKey):
    """Условие по ВСЕМ полям StorageKey; NULL сравнивается через IS NULL, а не `=`."""
    conditions = []

    for column, value in (
        (FsmStorage.bot_id, key.bot_id),
        (FsmStorage.chat_id, key.chat_id),
        (FsmStorage.user_id, key.user_id),
        (FsmStorage.thread_id, key.thread_id),
        (FsmStorage.business_connection_id, key.business_connection_id),
        (FsmStorage.destiny, key.destiny),
    ):
        conditions.append(column.is_(None) if value is None else column == value)

    return and_(*conditions)


def _key_values(key: StorageKey) -> dict[str, Any]:
    return {
        'bot_id': key.bot_id,
        'chat_id': key.chat_id,
        'user_id': key.user_id,
        'thread_id': key.thread_id,
        'business_connection_id': key.business_connection_id,
        'destiny': key.destiny,
    }


class PostgresStorage(BaseStorage):
    """
    FSM-хранилище aiogram в таблице fsm_storage — состояние и данные форм
    переживают рестарт/крэш бота (в отличие от MemoryStorage).

    Пустая запись не хранится: после записи state=None или data={} строка
    удаляется, если в ней не осталось ни состояния, ни данных. Поэтому
    set_state(None) не трогает data (контракт BaseStorage: state и data
    независимы, как в MemoryStorage), а state.clear() не оставляет мусора.
    Для отсутствующей строки get_state/get_data возвращают None/{}.

    Данные хранятся в JSONB, поэтому в state.update_data можно класть только
    JSON-сериализуемые значения (сейчас хендлеры кладут только строки).

    Сессии берутся из общей фабрики бота (app/database.py) — отдельного
    подключения к БД хранилище не создаёт.
    """

    def __init__(self, session_maker=async_session_maker) -> None:
        self._session_maker = session_maker

    async def close(self) -> None:
        # Engine общий для всего бота — хранилище им не владеет и не закрывает.
        pass

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        state = state.state if isinstance(state, State) else state

        async with self._session_maker() as session:
            if state is None:
                await session.execute(
                    update(FsmStorage).where(_key_filter(key)).values(state=None, updated_at=func.now())
                )
                await self._delete_if_empty(session, key)
            else:
                stmt = insert(FsmStorage).values(**_key_values(key), state=state)
                await session.execute(stmt.on_conflict_do_update(
                    constraint=KEY_CONSTRAINT,
                    set_={'state': stmt.excluded.state, 'updated_at': func.now()},
                ))

            await session.commit()

    async def get_state(self, key: StorageKey) -> str | None:
        async with self._session_maker() as session:
            result = await session.execute(select(FsmStorage.state).where(_key_filter(key)))
            return result.scalar_one_or_none()

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        if not isinstance(data, dict):
            msg = f"Data must be a dict or dict-like object, got {type(data).__name__}"
            raise DataNotDictLikeError(msg)

        async with self._session_maker() as session:
            if not data:
                await session.execute(
                    update(FsmStorage).where(_key_filter(key)).values(data={}, updated_at=func.now())
                )
                await self._delete_if_empty(session, key)
            else:
                stmt = insert(FsmStorage).values(**_key_values(key), data=data)
                await session.execute(stmt.on_conflict_do_update(
                    constraint=KEY_CONSTRAINT,
                    set_={'data': stmt.excluded.data, 'updated_at': func.now()},
                ))

            await session.commit()

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with self._session_maker() as session:
            result = await session.execute(select(FsmStorage.data).where(_key_filter(key)))
            data = result.scalar_one_or_none()

        return dict(data) if data is not None else {}

    async def update_data(self, key: StorageKey, data: Mapping[str, Any]) -> dict[str, Any]:
        """
        Атомарный аналог BaseStorage.update_data (get → dict.update → set) одним
        upsert'ом: JSONB `||` — такой же поверхностный merge, как dict.update.
        """
        if not data:
            return await self.get_data(key)

        async with self._session_maker() as session:
            stmt = insert(FsmStorage).values(**_key_values(key), data=dict(data))
            stmt = stmt.on_conflict_do_update(
                constraint=KEY_CONSTRAINT,
                set_={
                    'data': FsmStorage.data.op('||', return_type=JSONB)(stmt.excluded.data),
                    'updated_at': func.now(),
                },
            ).returning(FsmStorage.data)

            result = await session.execute(stmt)
            new_data = result.scalar_one()
            await session.commit()

        return dict(new_data)

    @staticmethod
    async def _delete_if_empty(session, key: StorageKey) -> None:
        await session.execute(
            delete(FsmStorage).where(
                _key_filter(key),
                FsmStorage.state.is_(None),
                FsmStorage.data == {},
            )
        )
