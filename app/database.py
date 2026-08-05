from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from config import Config

DATABASE_URL = Config.SQLALCHEMY_DATABASE_URI

engine = create_async_engine(url=DATABASE_URL, echo=True)

async_session_maker = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def init_db():
    from app.models import Users, Settings, Requests

    async with engine.begin() as conn:
        await conn.run_sync(lambda connection: Base.metadata.create_all(bind=connection))
