from app.models import Users, Settings, Requests
from app.database import async_session_maker
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select


async def add_user(user_id):
    entry = Users(telegram_id=user_id)

    async with async_session_maker() as session:
        try:
            session.add(entry)
            await session.commit()

        except IntegrityError:
            await session.rollback()


async def save_user_appeal(user_id, message):
    query = select(Users).where(
        Users.telegram_id == user_id
    )

    async with async_session_maker() as session:
        result = await session.execute(query)

        telegram_user = result.scalar()

        if not telegram_user:
            return 'ERROR'

    entry = Requests(
        user_id=telegram_user.id,
        type='Bid',
        text=message
    )

    async with async_session_maker() as session:
        session.add(entry)
        await session.commit()


async def get_user_thread_id(user_id):
    query = select(Users).where(Users.telegram_id == user_id)

    async with async_session_maker() as session:
        result = await session.execute(query)

        telegram_user = result.scalar_one_or_none()

    if telegram_user.topic_id is None:
        return None

    return telegram_user.topic_id


async def get_topic_name(user_id):
    query = select(Users).where(Users.telegram_id == user_id)

    async with async_session_maker() as session:
        result = await session.execute(query)

        telegram_user = result.scalar_one_or_none()



    return f'Клиент №{telegram_user.id}'


async def set_user_thread_id(user_id, topic_id):
    query = select(Users).where(Users.telegram_id == user_id)

    async with async_session_maker() as session:
        result = await session.execute(query)
        telegram_user = result.scalar_one_or_none()
        telegram_user.topic_id = topic_id

        await session.commit()


async def get_user_id(message_thread_id):
    query = select(Users).where(Users.topic_id == message_thread_id)

    async with async_session_maker() as session:
        result = await session.execute(query)

        telegram_user = result.scalar_one_or_none()

        if telegram_user:
            return telegram_user.telegram_id
        return None


async def get_group_id():
    query = select(Settings).where(Settings.id == 1)

    async with async_session_maker() as session:
        result = await session.execute(query)

        setting = result.scalar_one_or_none()

        if setting is None:
            return None

        if setting.group_id is None:
            return None

        return setting.group_id


async def save_group_id(group_id):
    query = select(Settings).where(Settings.id == 1)

    async with async_session_maker() as session:
        result = await session.execute(query)

        setting = result.scalar_one_or_none()

        if setting is None:
            entry = Settings(id=1, group_id=group_id)

            session.add(entry)
            await session.commit()

            return True

        if setting.group_id is not None:
            if setting.group_id != group_id:
                return False

            return True

        setting.group_id = group_id
        await session.commit()

        return True

