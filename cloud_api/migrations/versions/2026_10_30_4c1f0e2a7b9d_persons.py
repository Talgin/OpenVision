"""persons

Revision ID: 4c1f0e2a7b9d
Revises: 9b133c782f2c
Create Date: 2026-10-30 16:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '4c1f0e2a7b9d'
down_revision = '9b133c782f2c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'persons',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('note', sa.String(length=500), nullable=True),
        sa.Column('photo_key', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('persons', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_persons_tenant_id'), ['tenant_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('persons', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_persons_tenant_id'))
    op.drop_table('persons')
