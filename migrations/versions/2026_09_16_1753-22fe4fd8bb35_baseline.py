"""baseline: схема БД на момент перехода на Alembic

Отражает ровно ту схему, которую до этого создавал Base.metadata.create_all
(включая requests.group_message_id, на проде добавленный вручную через ALTER TABLE).

- Значения по умолчанию из моделей (is_active=True, created_at, тексты settings)
  работают только на стороне Python, поэтому server_default здесь нет — как и в
  схеме, созданной create_all.
- UniqueConstraint без явных имён: Postgres назовёт их так же, как при create_all
  (users_telegram_id_key, requests_group_message_id_key, ...), поэтому имена
  совпадают с продом и будущие drop_constraint их найдут.

На базе, где таблицы уже есть (прод), эту миграцию НЕ применяют, а помечают
как применённую: `alembic stamp head` — см. MIGRATIONS.md.

Revision ID: 22fe4fd8bb35
Revises:
Create Date: 2026-09-16 17:53:56.775213

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '22fe4fd8bb35'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('price_text', sa.Text(), nullable=False),
        sa.Column('about_us_text', sa.Text(), nullable=False),
        sa.Column('group_id', sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('group_id'),
    )
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('telegram_id', sa.BigInteger(), nullable=False),
        sa.Column('topic_id', sa.Integer(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('telegram_id'),
        sa.UniqueConstraint('topic_id'),
    )
    op.create_table(
        'requests',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('type', sa.String(length=20), nullable=False),
        sa.Column('name', sa.Text(), nullable=True),
        sa.Column('birthday', sa.String(length=10), nullable=True),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('group_message_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('group_message_id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('requests')
    op.drop_table('users')
    op.drop_table('settings')
