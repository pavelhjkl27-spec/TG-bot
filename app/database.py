from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from config import Config

DATABASE_URL = Config.SQLALCHEMY_DATABASE_URI

ALEMBIC_INI_PATH = Path(__file__).resolve().parent.parent / 'alembic.ini'

engine = create_async_engine(url=DATABASE_URL)

async_session_maker = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class UnstampedDatabaseError(RuntimeError):
    """Таблицы уже есть, но БД не помечена Alembic'ом (создана до перехода на миграции)."""


def _upgrade_to_head(connection):
    tables = set(inspect(connection).get_table_names())

    # Без этой проверки upgrade попытался бы выполнить CREATE TABLE поверх существующих
    # таблиц. Помечать такую базу автоматически нельзя: её схему (например, вручную
    # добавленный group_message_id) нужно сначала сверить глазами — см. MIGRATIONS.md.
    if 'users' in tables and 'alembic_version' not in tables:
        raise UnstampedDatabaseError(
            'База данных создана до перехода на Alembic (таблицы есть, alembic_version нет). '
            'Сверьте схему и выполните `alembic stamp head` — см. MIGRATIONS.md.'
        )

    alembic_config = AlembicConfig(str(ALEMBIC_INI_PATH))
    alembic_config.attributes['connection'] = connection
    command.upgrade(alembic_config, 'head')


async def run_migrations():
    """
    Приводит схему БД к последней миграции (`alembic upgrade head`) на движке бота
    в одной транзакции. Единственный способ создания/изменения схемы — create_all
    не используется, чтобы не конфликтовать с Alembic.
    """
    async with engine.begin() as conn:
        await conn.run_sync(_upgrade_to_head)
