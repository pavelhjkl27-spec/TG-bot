"""
Окружение Alembic для бота.

Движок и метаданные — те же, что у самого бота (`app.database`), поэтому URL БД
берётся из DATABASE_URL (.env / compose.yaml через config.py) и нигде не дублируется.
Импорт config.py требует ADMIN_ID в окружении — как и при запуске бота.

Два режима online-запуска:
  - соединение передано через `config.attributes['connection']` — так миграции
    вызывает сам бот при старте (`app.database.run_migrations`) изнутри своего
    event loop, в транзакции своего движка;
  - иначе — запуск из CLI (`alembic upgrade head`, `alembic revision --autogenerate` ...):
    поднимаем свой event loop на том же движке.
"""
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

import app.models  # noqa: F401 — регистрирует модели в Base.metadata
from app.database import Base, engine

config = context.config

target_metadata = Base.metadata

shared_connection = config.attributes.get('connection')

# Логирование из alembic.ini настраиваем только для CLI: при вызове из бота
# fileConfig перезаписал бы logging.basicConfig из run.py.
if shared_connection is None and config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def run_migrations_offline() -> None:
    """`alembic upgrade head --sql` — генерирует SQL без подключения к БД."""
    context.configure(
        url=engine.url.render_as_string(hide_password=False),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={'paramstyle': 'named'},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
            await connection.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
elif shared_connection is not None:
    do_run_migrations(shared_connection)
else:
    asyncio.run(run_async_migrations())
