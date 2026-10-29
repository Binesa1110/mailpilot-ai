import sqlalchemy as sa

metadata = sa.MetaData()

sa.Table(
    "drafts",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("owner_id", sa.Text, nullable=False),
    sa.Column("recipient", sa.Text, nullable=False),
    sa.Column("subject", sa.Text, nullable=False),
    sa.Column("body", sa.Text, nullable=False),
    sa.Column("review_json", sa.Text, nullable=False),
    sa.Column("created_at", sa.Text, nullable=False),
    sa.Column("updated_at", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default="draft"),
    sa.Column("approved_hash", sa.Text),
    sa.Column("approved_at", sa.Text),
    sa.Column("send_attempt_key", sa.Text),
    sa.Column("provider_message_id", sa.Text),
    sa.Column("sent_at", sa.Text),
    sa.Column("send_error", sa.Text),
    sa.Column("category", sa.Text, nullable=False, server_default="Professional"),
    sa.Column("version", sa.Integer, nullable=False, server_default="1"),
    sa.Column("send_authorization", sa.Text),
    sa.Column("policy_json", sa.Text),
)
sa.Index("drafts_owner_created", "owner_id", "created_at")
sa.Index("drafts_owner_updated", "owner_id", "updated_at")

sa.Table(
    "auth_sessions",
    metadata,
    sa.Column("session_hash", sa.Text, primary_key=True),
    sa.Column("owner_id", sa.Text, nullable=False),
    sa.Column("expires_at", sa.Text, nullable=False),
)
sa.Table(
    "users",
    metadata,
    sa.Column("owner_id", sa.Text, primary_key=True),
    sa.Column("email", sa.Text, nullable=False),
    sa.Column("created_at", sa.Text, nullable=False),
    sa.Column("updated_at", sa.Text, nullable=False),
)
sa.Table(
    "oauth_states",
    metadata,
    sa.Column("state_hash", sa.Text, primary_key=True),
    sa.Column("intent", sa.Text, nullable=False),
    sa.Column("owner_id", sa.Text),
    sa.Column("session_hash", sa.Text),
    sa.Column("expires_at", sa.Text, nullable=False),
)
sa.Table(
    "gmail_connections",
    metadata,
    sa.Column("owner_id", sa.Text, primary_key=True),
    sa.Column("credentials_encrypted", sa.Text, nullable=False),
    sa.Column("scopes_json", sa.Text, nullable=False),
    sa.Column("updated_at", sa.Text, nullable=False),
    sa.Column("account_email", sa.Text, nullable=False, server_default=""),
)
sa.Table(
    "audit_events",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("owner_id", sa.Text, nullable=False),
    sa.Column("draft_id", sa.Text),
    sa.Column("event_type", sa.Text, nullable=False),
    sa.Column("details_json", sa.Text, nullable=False),
    sa.Column("created_at", sa.Text, nullable=False),
)
sa.Index("audit_draft_created", "draft_id", "created_at")
sa.Index("audit_owner_event_created", "owner_id", "event_type", "created_at")

sa.Table(
    "account_settings",
    metadata,
    sa.Column("owner_id", sa.Text, primary_key=True),
    sa.Column("mode", sa.Text, nullable=False, server_default="approval_required"),
    sa.Column("enabled", sa.Integer, nullable=False, server_default="0"),
    sa.Column("recipient_allowlist_json", sa.Text, nullable=False, server_default="[]"),
    sa.Column("categories_json", sa.Text, nullable=False, server_default="[]"),
    sa.Column("daily_limit", sa.Integer, nullable=False, server_default="5"),
    sa.Column("consent_version", sa.Integer, nullable=False, server_default="0"),
    sa.Column("updated_at", sa.Text, nullable=False),
)
sa.Table(
    "system_settings",
    metadata,
    sa.Column("setting_key", sa.Text, primary_key=True),
    sa.Column("setting_value", sa.Text, nullable=False),
    sa.Column("updated_at", sa.Text, nullable=False),
)
sa.Table(
    "generation_requests",
    metadata,
    sa.Column("owner_id", sa.Text, primary_key=True),
    sa.Column("idempotency_key", sa.Text, primary_key=True),
    sa.Column("request_hash", sa.Text, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("draft_id", sa.Text),
    sa.Column("created_at", sa.Text, nullable=False),
)
sa.Index("generation_owner_created", "owner_id", "created_at")
