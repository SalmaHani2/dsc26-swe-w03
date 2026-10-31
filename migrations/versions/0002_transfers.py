"""Create transfers and idempotency records."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "transfers",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("from_account_id", sa.String(32), nullable=False),
        sa.Column("to_account_id", sa.String(32), nullable=False),
        sa.Column("amount_piastres", sa.Integer(), nullable=False),
        sa.Column("memo", sa.String(140), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.CheckConstraint(
            "amount_piastres > 0",
            name="ck_transfers_amount_positive",
        ),
        sa.ForeignKeyConstraint(["from_account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["to_account_id"], ["accounts.id"]),
    )

    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("customer_id", sa.String(32), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("request_body", sa.JSON(), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("response_body", sa.JSON(), nullable=False),
        sa.UniqueConstraint(
            "customer_id",
            "key",
            name="uq_idempotency_customer_key",
        ),
    )


def downgrade():
    op.drop_table("idempotency_records")
    op.drop_table("transfers")