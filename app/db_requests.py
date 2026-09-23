from datetime import timedelta

from app.models import Users, Settings, Requests, FsmStorage, DEFAULT_PRICE_TEXT
from app.database import async_session_maker
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, select, update

_SETTINGS_QUERY = select(Settings).where(Settings.id == 1)


async def _get_or_create_settings(session, **create_fields):
    """
    Возвращает (setting, created) для строки Settings(id=1), создавая её
    при отсутствии. При гонке на INSERT (IntegrityError) делает повторный
    SELECT — setting может оказаться None, если строка так и не появилась.
    """
    result = await session.execute(_SETTINGS_QUERY)
    setting = result.scalar_one_or_none()

    if setting is not None:
        return setting, False

    entry = Settings(id=1, **create_fields)
    session.add(entry)

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()

        result = await session.execute(_SETTINGS_QUERY)
        setting = result.scalar_one_or_none()

        return setting, False
    else:
        return entry, True


async def add_user(user_id):
    query = select(Users).where(Users.telegram_id == user_id)

    async with async_session_maker() as session:
        result = await session.execute(query)
        telegram_user = result.scalar_one_or_none()

        if telegram_user is None:
            telegram_user = Users(
                telegram_id=user_id,
                is_active=True
            )
            session.add(telegram_user)
        else:
            telegram_user.is_active = True

        await session.commit()


async def save_user_appeal(user_id, message, appeal_type, name=None, birthday=None, group_message_id=None):
    query = select(Users).where(
        Users.telegram_id == user_id
    )

    async with async_session_maker() as session:
        result = await session.execute(query)

        telegram_user = result.scalar()

        if not telegram_user:
            return False

        entry = Requests(
            user_id=telegram_user.id,
            type=appeal_type,
            text=message,
            name=name,
            birthday=birthday,
            group_message_id=group_message_id
        )

        session.add(entry)
        await session.commit()

        return True


async def get_reply_target_by_group_message_id(group_message_id):
    """
    Обращение клиента, чья карточка в группе имеет этот message_id: строка
    (text, telegram_id, type, status) или None. None означает, что цитируемое сообщение бота —
    не карточка заявки/вопроса (служебное сообщение, корень темы, карточка без сохранённого
    group_message_id). type — 'Bid' | 'Question'; status — статус заказа (у вопросов всегда 'new').
    """
    query = (
        select(Requests.text, Users.telegram_id, Requests.type, Requests.status)
        .join(Users, Users.id == Requests.user_id)
        .where(Requests.group_message_id == group_message_id)
    )

    async with async_session_maker() as session:
        result = await session.execute(query)

        return result.one_or_none()


async def get_bid_history_by_thread_id(thread_id):
    """
    Клиент резолвится тем же способом, что и в get_user_id (Users.topic_id == thread_id),
    без изобретения нового способа связи темы форума с клиентом. Возвращает None, если
    тема ни к одному клиенту не привязана, иначе список (created_at, text, status) записей
    Requests.type == 'Bid' этого клиента в хронологическом порядке (может быть пустым).
    """
    query = select(Users.id).where(Users.topic_id == thread_id)

    async with async_session_maker() as session:
        result = await session.execute(query)
        user_pk = result.scalar_one_or_none()

        if user_pk is None:
            return None

        requests_query = (
            select(Requests.created_at, Requests.text, Requests.status)
            .where(Requests.user_id == user_pk, Requests.type == 'Bid')
            .order_by(Requests.created_at)
        )
        result = await session.execute(requests_query)

        return result.all()


async def transition_request_status(group_message_id, from_statuses, to_status):
    """
    Атомарный CAS статуса заказа прямо на requests (не FSM): UPDATE ... WHERE group_message_id = ?
    AND status IN from_statuses. Конкурентный UPDATE той же строки ждёт коммита первого, перепроверяет
    status и обновляет 0 строк. Данные для карточки и клиента берутся тем же выражением (RETURNING),
    в той же транзакции. Возвращает строку (name, birthday, text, created_at, telegram_id), если
    обновилась ровно одна заявка, иначе None (статус уже не тот или такой карточки нет).
    """
    query = (
        update(Requests)
        .where(Requests.group_message_id == group_message_id,
               Requests.type == 'Bid',
               Requests.status.in_(from_statuses),
               Users.id == Requests.user_id)
        .values(status=to_status)
        .returning(Requests.name, Requests.birthday, Requests.text, Requests.created_at, Users.telegram_id)
    )

    async with async_session_maker() as session:
        result = await session.execute(query)
        rows = result.all()

        if len(rows) != 1:
            await session.rollback()
            return None

        await session.commit()

        return rows[0]


async def get_bid_card_by_group_message_id(group_message_id):
    """(status, name, birthday, text) заявки по её карточке в группе или None."""
    query = select(Requests.status, Requests.name, Requests.birthday, Requests.text).where(
        Requests.group_message_id == group_message_id, Requests.type == 'Bid'
    )

    async with async_session_maker() as session:
        result = await session.execute(query)

        return result.one_or_none()


async def get_user_thread_id(user_id):
    query = select(Users).where(Users.telegram_id == user_id)

    async with async_session_maker() as session:
        result = await session.execute(query)

        telegram_user = result.scalar_one_or_none()

        if telegram_user is None:
            return None

    if telegram_user.topic_id is None:
        return None

    return telegram_user.topic_id


async def get_topic_name(user_id):
    query = select(Users).where(Users.telegram_id == user_id)

    async with async_session_maker() as session:
        result = await session.execute(query)

        telegram_user = result.scalar_one_or_none()

        if telegram_user is None:
            return None

    return f'Клиент №{telegram_user.id}'


async def set_user_thread_id(user_id, topic_id):
    query = select(Users).where(Users.telegram_id == user_id)

    async with async_session_maker() as session:
        result = await session.execute(query)
        telegram_user = result.scalar_one_or_none()

        if telegram_user is None:
            return False

        telegram_user.topic_id = topic_id

        await session.commit()

        return True


async def clear_user_thread_id(user_id, topic_id):
    """
    Сбрасывает привязку клиента к теме, которой больше нет в Telegram, чтобы
    следующее обращение создало новую тему с нуля.

    Условный UPDATE (… AND topic_id = :topic_id): сбрасываем ровно ту тему,
    на которой споткнулась отправка. Если параллельная попытка того же клиента
    уже успела привязать новую тему, условие не совпадёт и свежая привязка
    уцелеет. Возвращает True, если строка действительно сброшена.
    """
    query = (
        update(Users)
        .where(Users.telegram_id == user_id, Users.topic_id == topic_id)
        .values(topic_id=None)
        .returning(Users.id)
    )

    async with async_session_maker() as session:
        result = await session.execute(query)
        cleared = result.scalar_one_or_none()

        await session.commit()

        return cleared is not None


async def get_idle_fsm_rows(bot_id: int, state: str, idle_for: timedelta) -> list[tuple[int, dict]]:
    """
    (user_id, data) личных FSM-записей в состоянии state, которые не менялись дольше idle_for
    (fsm_storage.updated_at ставит каждая запись PostgresStorage). Самые старые — первыми.

    Только чтение, без блокировок: это кандидаты. Решение о переходе принимает атомарный
    PostgresStorage.transition_state, который перепроверит состояние и data под FOR UPDATE.
    """
    query = (
        select(FsmStorage.user_id, FsmStorage.data)
        .where(
            FsmStorage.bot_id == bot_id,
            FsmStorage.chat_id == FsmStorage.user_id,
            FsmStorage.thread_id.is_(None),
            FsmStorage.business_connection_id.is_(None),
            FsmStorage.destiny == 'default',
            FsmStorage.state == state,
            FsmStorage.updated_at < func.now() - idle_for,
        )
        .order_by(FsmStorage.updated_at, FsmStorage.id)
    )

    async with async_session_maker() as session:
        result = await session.execute(query)

        return [(user_id, dict(data)) for user_id, data in result.all()]


async def get_user_id(message_thread_id):
    query = select(Users).where(Users.topic_id == message_thread_id)

    async with async_session_maker() as session:
        result = await session.execute(query)

        telegram_user = result.scalar_one_or_none()

        if telegram_user:
            return telegram_user.telegram_id
        return None


async def get_group_id():
    async with async_session_maker() as session:
        result = await session.execute(_SETTINGS_QUERY)

        setting = result.scalar_one_or_none()

        if setting is None:
            return None

        if setting.group_id is None:
            return None

        return setting.group_id


async def save_group_id(group_id):
    async with async_session_maker() as session:
        setting, created = await _get_or_create_settings(session, group_id=group_id)

        if setting is None:
            return False

        if created:
            return True

        if setting.group_id is not None:
            if setting.group_id != group_id:
                return False

            return True

        setting.group_id = group_id
        await session.commit()

        return True


async def get_about_us():
    async with async_session_maker() as session:
        result = await session.execute(_SETTINGS_QUERY)

        setting = result.scalar_one_or_none()

        if setting is None:
            return None

        if setting.about_us_text is None:
            return None

        return setting.about_us_text


async def get_price():
    async with async_session_maker() as session:
        result = await session.execute(_SETTINGS_QUERY)

        setting = result.scalar_one_or_none()

        if setting is None or setting.price_text is None:
            return None

        # Строку Settings мог создать не прайс (/bind, «О нас») — тогда в price_text лежит дефолт
        # модели; для клиента это «не задан», как и пустой текст.
        if not setting.price_text.strip() or setting.price_text == DEFAULT_PRICE_TEXT:
            return None

        return setting.price_text


async def get_users():
    query = select(Users.telegram_id, Users.is_active)

    async with async_session_maker() as session:
        result = await session.execute(query)

        telegram_users = result.all()

        if not telegram_users:
            return None

        return telegram_users


async def activated_user(user_id):
    query = select(Users).where(Users.telegram_id == user_id)

    async with async_session_maker() as session:
        result = await session.execute(query)

        telegram_user = result.scalar_one_or_none()

        if telegram_user is None:
            return False

        telegram_user.is_active = True

        await session.commit()

        return True


async def deactivated_user(user_id):
    query = select(Users).where(Users.telegram_id == user_id)

    async with async_session_maker() as session:
        result = await session.execute(query)

        telegram_user = result.scalar_one_or_none()

        if telegram_user is None:
            return False

        telegram_user.is_active = False

        await session.commit()

        return True


async def set_about_us_text(about_us_text):
    async with async_session_maker() as session:
        setting, created = await _get_or_create_settings(session, group_id=None, about_us_text=about_us_text)

        if setting is None:
            return False

        if created:
            return True

        setting.about_us_text = about_us_text
        await session.commit()

        return True


async def set_price(price):
    async with async_session_maker() as session:
        setting, created = await _get_or_create_settings(session, group_id=None, price_text=price)

        if setting is None:
            return False

        if created:
            return True

        setting.price_text = price
        await session.commit()

        return True
