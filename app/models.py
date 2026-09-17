from app.database import Base
from sqlalchemy import (Column, Integer,
                        String, DateTime,
                        BigInteger, ForeignKey,
                        Text, Boolean, UniqueConstraint,
                        func, text)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from datetime import datetime, timezone

class Users(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, nullable=False, unique=True)
    topic_id = Column(Integer, unique=True, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)

    requests = relationship("Requests", back_populates="user")


class Requests(Base):
    __tablename__ = "requests"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    type = Column(String(20), nullable=False)
    name = Column(Text, nullable=True)
    birthday = Column(String(10), nullable=True)
    text = Column(Text, nullable=False)
    group_message_id = Column(Integer, unique=True, nullable=True)
    # 'new' | 'in_progress' | 'done' — статус заказа (кнопки под карточкой заявки в теме).
    # server_default, а не ORM-default: строки, созданные до колонки, миграция пометила 'done'.
    status = Column(String(20), nullable=False, server_default='new')
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda _: datetime.now(timezone.utc))

    user = relationship('Users', back_populates='requests')


class Settings(Base):
    __tablename__ = 'settings'

    id = Column(Integer, primary_key=True)
    price_text = Column(Text, nullable=False, default='Прайс уточняется у администратора')
    about_us_text = Column(Text, nullable=False, default='Описание уточняется у администратора')
    group_id = Column(BigInteger, nullable=True, unique=True)


class FsmStorage(Base):
    """
    Состояние FSM aiogram (см. app/fsm_storage.py). Одна строка на StorageKey;
    все его поля — часть уникального ключа. NULLS NOT DISTINCT (Postgres 15+):
    иначе строки с thread_id / business_connection_id = NULL не конфликтовали бы
    между собой и upsert плодил бы дубли.
    """
    __tablename__ = 'fsm_storage'
    __table_args__ = (
        UniqueConstraint('bot_id', 'chat_id', 'user_id', 'thread_id', 'business_connection_id', 'destiny',
                         name='uq_fsm_storage_key', postgresql_nulls_not_distinct=True),
    )

    id = Column(Integer, primary_key=True)
    bot_id = Column(BigInteger, nullable=False)
    chat_id = Column(BigInteger, nullable=False)
    user_id = Column(BigInteger, nullable=False)
    thread_id = Column(BigInteger, nullable=True)
    business_connection_id = Column(String(255), nullable=True)
    destiny = Column(String(255), nullable=False, server_default='default')
    state = Column(Text, nullable=True)
    data = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
