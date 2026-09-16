"""fsm_storage: персистентное хранилище FSM aiogram (app/fsm_storage.py)

Заменяет MemoryStorage: состояние и данные форм переживают рестарт бота.

- Уникальный ключ — все поля aiogram StorageKey. NULLS NOT DISTINCT (Postgres 15+)
  обязателен: thread_id и business_connection_id почти всегда NULL, и без него
  такие строки не конфликтовали бы, а upsert (ON CONFLICT) плодил бы дубли.
- В отличие от baseline, здесь server_default'ы: строки пишутся через
  INSERT ... ON CONFLICT в обход ORM-дефолтов.

Revision ID: 3b877d403a8d
Revises: 22fe4fd8bb35
Create Date: 2026-09-16 19:06:20.762877

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '3b877d403a8d'
down_revision: Union[str, Sequence[str], None] = '22fe4fd8bb35'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'fsm_storage',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('bot_id', sa.BigInteger(), nullable=False),
        sa.Column('chat_id', sa.BigInteger(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('thread_id', sa.BigInteger(), nullable=True),
        sa.Column('business_connection_id', sa.String(length=255), nullable=True),
        sa.Column('destiny', sa.String(length=255), server_default='default', nullable=False),
        sa.Column('state', sa.Text(), nullable=True),
        sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"),
                  nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('bot_id', 'chat_id', 'user_id', 'thread_id', 'business_connection_id', 'destiny',
                            name='uq_fsm_storage_key', postgresql_nulls_not_distinct=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('fsm_storage')
