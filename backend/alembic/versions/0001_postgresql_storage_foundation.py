"""Create MailPilot storage schema and add current SQLite-era columns.

Revision ID: 0001_storage
Revises:
"""
from alembic import op
import sqlalchemy as sa

from app.schema import metadata

revision = "0001_storage"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    metadata.create_all(bind=bind, checkfirst=True)

    inspector = sa.inspect(bind)
    existing = {
        "drafts": {
            "category": sa.Column("category", sa.Text(), nullable=False, server_default="Professional"),
            "version": sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            "send_authorization": sa.Column("send_authorization", sa.Text()),
            "policy_json": sa.Column("policy_json", sa.Text()),
        },
        "gmail_connections": {
            "account_email": sa.Column("account_email", sa.Text(), nullable=False, server_default=""),
        },
        "account_settings": {
            "consent_version": sa.Column("consent_version", sa.Integer(), nullable=False, server_default="0"),
        },
    }
    for table_name, columns in existing.items():
        present = {column["name"] for column in inspector.get_columns(table_name)}
        for column_name, column in columns.items():
            if column_name not in present:
                op.add_column(table_name, column)

    inspector = sa.inspect(bind)
    indexes = {
        "drafts": {
            "drafts_owner_created": ["owner_id", "created_at"],
            "drafts_owner_updated": ["owner_id", "updated_at"],
        },
        "audit_events": {"audit_draft_created": ["draft_id", "created_at"]},
        "generation_requests": {
            "generation_owner_created": ["owner_id", "created_at"],
        },
    }
    indexes["audit_events"]["audit_owner_event_created"] = [
        "owner_id",
        "event_type",
        "created_at",
    ]
    for table_name, table_indexes in indexes.items():
        present = {index["name"] for index in inspector.get_indexes(table_name)}
        for index_name, columns in table_indexes.items():
            if index_name not in present:
                op.create_index(index_name, table_name, columns)


def downgrade():
    raise NotImplementedError(
        "The initial storage migration is intentionally irreversible; restore a verified backup "
        "or apply a forward migration to avoid deleting account and audit data."
    )
