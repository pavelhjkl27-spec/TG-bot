"""request_status: статус заказа Requests.status ('new' / 'in_progress' / 'done')

Исторические строки (созданные до этой миграции) получают 'done', новые — 'new':

- add_column с server_default='done' и NOT NULL: Postgres заполняет все уже существующие
  строки этим значением в том же DDL (без отдельного UPDATE-backfill и без окна, где колонка NULL);
- затем alter_column меняет дефолт на 'new' — его получают только строки, вставленные после.

Старые заказы не должны выглядеть «висящими в работе»: для истории корректнее считать их закрытыми.

Revision ID: 4c97cc33d868
Revises: 3b877d403a8d
Create Date: 2026-09-17 20:58:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '4c97cc33d868'
down_revision: Union[str, Sequence[str], None] = '3b877d403a8d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('requests', sa.Column('status', sa.String(length=20), server_default='done', nullable=False))
    op.alter_column('requests', 'status', server_default='new')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('requests', 'status')
