from app.database import Base
from sqlalchemy import Column, Integer, String, DateTime, BigInteger, ForeignKey, Text
from sqlalchemy.orm import relationship
from datetime import datetime, timezone

class Users(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, nullable=False, unique=True)
    topic_id = Column(Integer, unique=True, nullable=True)

    requests = relationship("Requests", back_populates="user")


class Requests(Base):
    __tablename__ = "requests"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    type = Column(String(20), nullable=False)
    text = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda _: datetime.now(timezone.utc))

    user = relationship('Users', back_populates='requests')


class Settings(Base):
    __tablename__ = 'settings'

    id = Column(Integer, primary_key=True)
    price_text = Column(Text, nullable=False, default='Прайс уточняется у администратора')
    about_us_text = Column(Text, nullable=False, default='Описание уточняется у администратора')
    group_id = Column(BigInteger, nullable=True, unique=True)
