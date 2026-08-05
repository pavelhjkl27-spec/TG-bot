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

