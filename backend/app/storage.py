import json
import hashlib
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS drafts (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                recipient TEXT NOT NULL,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                review_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                approved_hash TEXT,
                approved_at TEXT,
                send_attempt_key TEXT,
                provider_message_id TEXT,
                sent_at TEXT,
                send_error TEXT
            );
            CREATE INDEX IF NOT EXISTS drafts_owner_created
                ON drafts(owner_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS drafts_owner_updated
                ON drafts(owner_id, updated_at DESC);
            CREATE TABLE IF NOT EXISTS auth_sessions (
                session_hash TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS gmail_connections (
                owner_id TEXT PRIMARY KEY,
                credentials_encrypted TEXT NOT NULL,
                scopes_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                account_email TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                draft_id TEXT,
                event_type TEXT NOT NULL,
                details_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS audit_draft_created
                ON audit_events(draft_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS account_settings (
                owner_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL DEFAULT 'approval_required',
                enabled INTEGER NOT NULL DEFAULT 0,
                recipient_allowlist_json TEXT NOT NULL DEFAULT '[]',
                categories_json TEXT NOT NULL DEFAULT '[]',
                daily_limit INTEGER NOT NULL DEFAULT 5,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS system_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS generation_requests (
                owner_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                state TEXT NOT NULL,
                draft_id TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY(owner_id, idempotency_key)
            );
            """
        )
        self._migrate_drafts()
        connection_columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(gmail_connections)").fetchall()
        }
        if "account_email" not in connection_columns:
            self.connection.execute(
                "ALTER TABLE gmail_connections ADD COLUMN account_email TEXT NOT NULL DEFAULT ''"
            )
        self.connection.execute(
            """INSERT OR IGNORE INTO system_settings (setting_key, setting_value, updated_at)
            VALUES ('auto_send_kill_switch', ?, ?)""",
            ("1" if os.getenv("MAILPILOT_AUTO_SEND_KILL_SWITCH", "false").lower() == "true" else "0", utc_now()),
        )
        self.connection.commit()

    def claim_generation(self, owner_id: str, key: str, request_hash: str):
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT * FROM generation_requests WHERE owner_id = ? AND idempotency_key = ?",
                (owner_id, key),
            ).fetchone()
            if row:
                self.connection.commit()
                if row["request_hash"] != request_hash:
                    return "conflict", None
                if row["state"] == "complete" and row["draft_id"]:
                    return "duplicate", self.get_draft(owner_id, row["draft_id"])
                return "in_progress", None
            today = utc_now()[:10]
            count = self.connection.execute(
                """SELECT COUNT(*) AS count FROM generation_requests
                WHERE owner_id = ? AND substr(created_at, 1, 10) = ?""",
                (owner_id, today),
            ).fetchone()["count"]
            if count >= 50:
                self._audit(owner_id, "generation_rate_limited", details={"daily_limit": 50})
                self.connection.commit()
                return "rate_limited", None
            self.connection.execute(
                "INSERT INTO generation_requests VALUES (?, ?, ?, 'processing', NULL, ?)",
                (owner_id, key, request_hash, utc_now()),
            )
            self.connection.commit()
            return "claimed", None

    def fail_generation(self, owner_id: str, key: str):
        with self.lock:
            self.connection.execute(
                """UPDATE generation_requests SET state = 'failed'
                WHERE owner_id = ? AND idempotency_key = ? AND state = 'processing'""",
                (owner_id, key),
            )
            self.connection.commit()

    def _migrate_drafts(self):
        columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(drafts)").fetchall()
        }
        additions = {
            "category": "TEXT NOT NULL DEFAULT 'Professional'",
            "version": "INTEGER NOT NULL DEFAULT 1",
            "send_authorization": "TEXT",
            "policy_json": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                self.connection.execute(f"ALTER TABLE drafts ADD COLUMN {name} {declaration}")

    def _audit(self, owner_id: str, event_type: str, draft_id: str | None = None, details=None):
        self.connection.execute(
            "INSERT INTO audit_events VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), owner_id, draft_id, event_type, json.dumps(details or {}), utc_now()),
        )

    def create_session(self, session_hash: str, owner_id: str, max_age_seconds: int):
        with self.lock:
            expires = (datetime.now(timezone.utc) + timedelta(seconds=max_age_seconds)).isoformat()
            self.connection.execute(
                "INSERT OR REPLACE INTO auth_sessions VALUES (?, ?, ?)",
                (session_hash, owner_id, expires),
            )
            self.connection.commit()

    def session_owner(self, session_hash: str) -> str | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT owner_id, expires_at FROM auth_sessions WHERE session_hash = ?",
                (session_hash,),
            ).fetchone()
            if not row:
                return None
            if row["expires_at"] <= utc_now():
                self.connection.execute("DELETE FROM auth_sessions WHERE session_hash = ?", (session_hash,))
                self.connection.commit()
                return None
            return row["owner_id"]

    def delete_session(self, session_hash: str):
        with self.lock:
            self.connection.execute("DELETE FROM auth_sessions WHERE session_hash = ?", (session_hash,))
            self.connection.commit()

    def save_connection(
        self, owner_id: str, encrypted: str, scopes: list[str], account_email: str = ""
    ):
        with self.lock:
            self.connection.execute(
                """INSERT INTO gmail_connections
                (owner_id, credentials_encrypted, scopes_json, updated_at, account_email)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(owner_id) DO UPDATE SET
                credentials_encrypted = excluded.credentials_encrypted,
                scopes_json = excluded.scopes_json,
                updated_at = excluded.updated_at,
                account_email = excluded.account_email""",
                (owner_id, encrypted, json.dumps(scopes), utc_now(), account_email),
            )
            self.connection.commit()

    def get_connection(self, owner_id: str):
        with self.lock:
            row = self.connection.execute(
                """SELECT credentials_encrypted, scopes_json, updated_at, account_email
                FROM gmail_connections WHERE owner_id = ?""",
                (owner_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "credentials_encrypted": row["credentials_encrypted"],
                "scopes": json.loads(row["scopes_json"]),
                "updated_at": row["updated_at"],
                "account_email": row["account_email"],
            }

    def delete_connection(self, owner_id: str):
        with self.lock:
            self.connection.execute("DELETE FROM gmail_connections WHERE owner_id = ?", (owner_id,))
            self.connection.commit()

    def create_draft(
        self,
        owner_id: str,
        recipient: str,
        subject: str,
        body: str,
        review: dict,
        category: str,
        generation_key: str,
    ) -> dict:
        now = utc_now()
        draft_id = str(uuid.uuid4())
        with self.lock:
            self.connection.execute(
                """INSERT INTO drafts
                (id, owner_id, recipient, subject, body, review_json, created_at, updated_at, category)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (draft_id, owner_id, recipient, subject, body, json.dumps(review), now, now, category),
            )
            self._audit(owner_id, "draft_created", draft_id)
            self.connection.execute(
                """UPDATE generation_requests SET state = 'complete', draft_id = ?
                WHERE owner_id = ? AND idempotency_key = ? AND state = 'processing'""",
                (draft_id, owner_id, generation_key),
            )
            self.connection.commit()
        return self.get_draft(owner_id, draft_id)

    @staticmethod
    def _draft_dict(row):
        item = dict(row)
        item["review"] = json.loads(item.pop("review_json"))
        item["policy_decision"] = json.loads(item.pop("policy_json")) if item.get("policy_json") else None
        item.pop("policy_json", None)
        return item

    @staticmethod
    def _content_hash(recipient: str, subject: str, body: str, version: int) -> str:
        content = json.dumps(
            {"recipient": recipient, "subject": subject, "body": body, "version": version},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def get_draft(self, owner_id: str, draft_id: str):
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM drafts WHERE id = ? AND owner_id = ?", (draft_id, owner_id)
            ).fetchone()
            return self._draft_dict(row) if row else None

    def list_drafts(self, owner_id: str, limit: int = 100):
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM drafts WHERE owner_id = ? ORDER BY updated_at DESC LIMIT ?",
                (owner_id, limit),
            ).fetchall()
            return [self._draft_dict(row) for row in rows]

    def approve_draft(
        self, owner_id: str, draft_id: str, recipient: str, subject: str, body: str, digest: str
    ):
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT status, version FROM drafts WHERE id = ? AND owner_id = ?", (draft_id, owner_id)
            ).fetchone()
            if not row:
                self.connection.commit()
                return "not_found"
            if row["status"] in ("sending", "sent", "uncertain"):
                self.connection.commit()
                return "locked"
            now = utc_now()
            updated = self.connection.execute(
                """UPDATE drafts SET recipient = ?, subject = ?, body = ?, status = 'approved',
                approved_hash = ?, approved_at = ?, updated_at = ?, send_attempt_key = NULL,
                version = version + 1, send_authorization = NULL, policy_json = NULL,
                send_error = NULL WHERE id = ? AND owner_id = ?
                AND status NOT IN ('sending', 'sent', 'uncertain') AND version = ?""",
                (recipient, subject, body, digest, now, now, draft_id, owner_id, row["version"]),
            ).rowcount
            if not updated:
                self.connection.commit()
                return "locked"
            self._audit(owner_id, "draft_approved", draft_id)
            self.connection.commit()
        return self.get_draft(owner_id, draft_id)

    def get_account_settings(self, owner_id: str):
        with self.lock:
            self.connection.execute(
                """INSERT OR IGNORE INTO account_settings (owner_id, updated_at)
                VALUES (?, ?)""",
                (owner_id, utc_now()),
            )
            self.connection.commit()
            row = self.connection.execute(
                "SELECT * FROM account_settings WHERE owner_id = ?", (owner_id,)
            ).fetchone()
            return self._settings_dict(row)

    @staticmethod
    def _settings_dict(row):
        return {
            "mode": row["mode"],
            "enabled": bool(row["enabled"]),
            "recipient_allowlist": json.loads(row["recipient_allowlist_json"]),
            "categories": json.loads(row["categories_json"]),
            "daily_limit": row["daily_limit"],
            "updated_at": row["updated_at"],
        }

    def save_auto_send_settings(
        self, owner_id: str, recipients: list[str], categories: list[str], daily_limit: int
    ):
        with self.lock:
            now = utc_now()
            self.connection.execute(
                """INSERT INTO account_settings
                (owner_id, mode, enabled, recipient_allowlist_json, categories_json, daily_limit, updated_at)
                VALUES (?, 'auto_send', 1, ?, ?, ?, ?)
                ON CONFLICT(owner_id) DO UPDATE SET mode = 'auto_send', enabled = 1,
                recipient_allowlist_json = excluded.recipient_allowlist_json,
                categories_json = excluded.categories_json, daily_limit = excluded.daily_limit,
                updated_at = excluded.updated_at""",
                (owner_id, json.dumps(recipients), json.dumps(categories), daily_limit, now),
            )
            self._audit(owner_id, "auto_send_enabled", details={
                "recipient_count": len(recipients),
                "categories": categories,
                "daily_limit": daily_limit,
            })
            self.connection.commit()
        return self.get_account_settings(owner_id)

    def disable_auto_send(self, owner_id: str):
        with self.lock:
            self.connection.execute(
                """INSERT INTO account_settings (owner_id, mode, enabled, updated_at)
                VALUES (?, 'approval_required', 0, ?)
                ON CONFLICT(owner_id) DO UPDATE SET mode = 'approval_required', enabled = 0,
                updated_at = excluded.updated_at""",
                (owner_id, utc_now()),
            )
            self._audit(owner_id, "auto_send_revoked")
            self.connection.commit()
        return self.get_account_settings(owner_id)

    def kill_switch_enabled(self) -> bool:
        if os.getenv("MAILPILOT_AUTO_SEND_KILL_SWITCH", "false").lower() == "true":
            return True
        with self.lock:
            row = self.connection.execute(
                "SELECT setting_value FROM system_settings WHERE setting_key = 'auto_send_kill_switch'"
            ).fetchone()
            return bool(row and row["setting_value"] == "1")

    def engage_kill_switch(self, owner_id: str):
        with self.lock:
            self.connection.execute(
                """INSERT INTO system_settings (setting_key, setting_value, updated_at)
                VALUES ('auto_send_kill_switch', '1', ?)
                ON CONFLICT(setting_key) DO UPDATE SET setting_value = '1', updated_at = excluded.updated_at""",
                (utc_now(),),
            )
            self._audit(owner_id, "global_auto_send_kill_switch_engaged")
            self.connection.commit()

    def _auto_send_count_today(self, owner_id: str) -> int:
        today = utc_now()[:10]
        row = self.connection.execute(
            """SELECT COUNT(*) AS count FROM audit_events
            WHERE owner_id = ? AND event_type = 'auto_send_reserved' AND substr(created_at, 1, 10) = ?""",
            (owner_id, today),
        ).fetchone()
        return row["count"]

    def auto_send_count_today(self, owner_id: str) -> int:
        with self.lock:
            return self._auto_send_count_today(owner_id)

    def record_auto_send_policy(self, owner_id: str, draft_id: str, version: int, decision: dict):
        with self.lock:
            self._audit(owner_id, "auto_send_policy_evaluated", draft_id, {
                "version": version,
                "policy_decision": decision,
            })
            self.connection.commit()

    @contextmanager
    def send_lock(self):
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            finally:
                if self.connection.in_transaction:
                    self.connection.commit()

    def verify_auto_send_before_provider(self, owner_id: str, draft_id: str, expected_version: int):
        from .gmail import GMAIL_SEND_SCOPE
        from .policy import evaluate_auto_send

        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM drafts WHERE id = ? AND owner_id = ?", (draft_id, owner_id)
            ).fetchone()
            settings_row = self.connection.execute(
                "SELECT * FROM account_settings WHERE owner_id = ?", (owner_id,)
            ).fetchone()
            connection = self.connection.execute(
                "SELECT scopes_json FROM gmail_connections WHERE owner_id = ?", (owner_id,)
            ).fetchone()
            if not row or not settings_row or not connection:
                return None
            draft = self._draft_dict(row)
            if (
                draft["version"] != expected_version
                or draft["status"] != "sending"
                or draft["send_authorization"] != "auto_policy"
            ):
                return None
            settings = self._settings_dict(settings_row)
            if self.kill_switch_enabled():
                return None
            scopes = json.loads(connection["scopes_json"])
            if GMAIL_SEND_SCOPE not in scopes:
                return None
            decision = evaluate_auto_send(
                {**draft, "status": "draft"},
                settings,
                max(0, self._auto_send_count_today(owner_id) - 1),
                True,
                False,
            ).as_dict()
            decision["draft_hash"] = self._content_hash(
                draft["recipient"], draft["subject"], draft["body"], draft["version"]
            )
            if not decision["allowed"] or decision != draft["policy_decision"]:
                return None
            return {
                "draft": draft,
                "credentials_encrypted": self.connection.execute(
                    "SELECT credentials_encrypted FROM gmail_connections WHERE owner_id = ?",
                    (owner_id,),
                ).fetchone()["credentials_encrypted"],
            }

    def verify_manual_send_before_provider(
        self, owner_id: str, draft_id: str, expected_version: int, approved_hash: str
    ):
        from .gmail import GMAIL_SEND_SCOPE

        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM drafts WHERE id = ? AND owner_id = ?", (draft_id, owner_id)
            ).fetchone()
            connection = self.connection.execute(
                "SELECT credentials_encrypted, scopes_json FROM gmail_connections WHERE owner_id = ?",
                (owner_id,),
            ).fetchone()
            if not row or not connection:
                return None
            draft = self._draft_dict(row)
            if (
                draft["status"] != "sending"
                or draft["send_authorization"] != "manual_approval"
                or draft["version"] != expected_version
                or self._content_hash(draft["recipient"], draft["subject"], draft["body"], draft["version"])
                != draft["approved_hash"]
            ):
                return None
            if GMAIL_SEND_SCOPE not in json.loads(connection["scopes_json"]):
                return None
            return {"draft": draft, "connection": dict(connection)}

    def begin_auto_send(
        self, owner_id: str, draft_id: str, idempotency_key: str, expected_version: int, decision: dict
    ):
        from .policy import evaluate_auto_send

        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT * FROM drafts WHERE id = ? AND owner_id = ?", (draft_id, owner_id)
            ).fetchone()
            if not row:
                self.connection.commit()
                return "not_found", None
            draft = self._draft_dict(row)
            settings_row = self.connection.execute(
                "SELECT * FROM account_settings WHERE owner_id = ?", (owner_id,)
            ).fetchone()
            settings = self._settings_dict(settings_row) if settings_row else {
                "mode": "approval_required",
                "enabled": False,
                "recipient_allowlist": [],
                "categories": [],
                "daily_limit": 5,
            }
            gmail = self.connection.execute(
                "SELECT scopes_json FROM gmail_connections WHERE owner_id = ?", (owner_id,)
            ).fetchone()
            from .gmail import GMAIL_SEND_SCOPE

            connected = bool(gmail and GMAIL_SEND_SCOPE in json.loads(gmail["scopes_json"]))
            current_decision = evaluate_auto_send(
                draft,
                settings,
                self._auto_send_count_today(owner_id),
                connected,
                self.kill_switch_enabled(),
            ).as_dict()
            current_decision["draft_hash"] = self._content_hash(
                draft["recipient"], draft["subject"], draft["body"], draft["version"]
            )
            if (
                draft["version"] != expected_version
                or not decision.get("allowed")
                or not current_decision["allowed"]
                or decision != current_decision
            ):
                if draft["version"] != expected_version:
                    current_decision = {"allowed": False, "reasons": ["draft_version_changed"]}
                elif decision.get("draft_hash") != current_decision.get("draft_hash"):
                    current_decision = {"allowed": False, "reasons": ["draft_content_changed"]}
                elif decision != current_decision and current_decision["allowed"]:
                    current_decision = {"allowed": False, "reasons": ["policy_changed_before_send"]}
                self._audit(owner_id, "auto_send_blocked", draft_id, {
                    "version": draft["version"],
                    "policy_decision": current_decision,
                })
                self.connection.commit()
                return "blocked", {**draft, "policy_decision": current_decision}
            if row["status"] == "sent":
                self.connection.commit()
                if row["send_attempt_key"] == idempotency_key:
                    return "already_sent", self._draft_dict(row)
                return "duplicate", self._draft_dict(row)
            if row["status"] in ("sending", "uncertain"):
                self.connection.commit()
                return "uncertain" if row["status"] == "uncertain" else "in_progress", self._draft_dict(row)
            if row["status"] != "draft":
                self.connection.commit()
                return "ineligible", draft
            now = utc_now()
            updated = self.connection.execute(
                """UPDATE drafts SET status = 'sending', send_attempt_key = ?, send_error = NULL,
                updated_at = ?, send_authorization = 'auto_policy', policy_json = ?
                WHERE id = ? AND owner_id = ? AND status = 'draft' AND version = ?""",
                (idempotency_key, now, json.dumps(current_decision), draft_id, owner_id, expected_version),
            ).rowcount
            if not updated:
                self.connection.commit()
                current = self.connection.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
                if current and current["status"] in ("sending", "uncertain"):
                    return ("uncertain" if current["status"] == "uncertain" else "in_progress"), self._draft_dict(current)
                if current and current["status"] == "sent":
                    if current["send_attempt_key"] == idempotency_key:
                        return "already_sent", self._draft_dict(current)
                    return "duplicate", self._draft_dict(current)
                return "ineligible", self._draft_dict(current) if current else None
            self._audit(owner_id, "auto_send_reserved", draft_id, {
                "version": expected_version,
                "authorization": "auto_policy",
                "policy_decision": current_decision,
            })
            self._audit(owner_id, "send_started", draft_id, {
                "idempotency_key": idempotency_key,
                "authorization": "auto_policy",
                "version": expected_version,
                "policy_decision": current_decision,
            })
            self.connection.commit()
            row = self.connection.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
            return "started", self._draft_dict(row)

    def begin_send(
        self, owner_id: str, draft_id: str, idempotency_key: str, approved_hash: str, expected_version: int
    ):
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT * FROM drafts WHERE id = ? AND owner_id = ?", (draft_id, owner_id)
            ).fetchone()
            if not row:
                self.connection.commit()
                return "not_found", None
            if row["status"] == "sent":
                self.connection.commit()
                if row["send_attempt_key"] == idempotency_key:
                    return "already_sent", self._draft_dict(row)
                return "duplicate", self._draft_dict(row)
            if row["status"] in ("sending", "uncertain"):
                self.connection.commit()
                return "uncertain" if row["status"] == "uncertain" else "in_progress", self._draft_dict(row)
            if (
                row["status"] != "approved"
                or row["approved_hash"] != approved_hash
                or row["version"] != expected_version
                or self._content_hash(row["recipient"], row["subject"], row["body"], row["version"])
                != row["approved_hash"]
            ):
                self.connection.commit()
                return "approval_changed", self._draft_dict(row)
            now = utc_now()
            updated = self.connection.execute(
                """UPDATE drafts SET status = 'sending', send_attempt_key = ?, send_error = NULL,
                updated_at = ?, send_authorization = 'manual_approval', policy_json = NULL
                WHERE id = ? AND owner_id = ? AND status = 'approved' AND approved_hash = ? AND version = ?""",
                (idempotency_key, now, draft_id, owner_id, approved_hash, expected_version),
            ).rowcount
            if not updated:
                self.connection.commit()
                return "in_progress", self.get_draft(owner_id, draft_id)
            self._audit(owner_id, "send_started", draft_id, {
                "idempotency_key": idempotency_key,
                "authorization": "manual_approval",
                "version": expected_version,
            })
            self.connection.commit()
            updated = self.connection.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
            return "started", self._draft_dict(updated)

    def finish_send(
        self,
        owner_id: str,
        draft_id: str,
        status: str,
        message_id: str | None = None,
        error: str | None = None,
        policy_decision: dict | None = None,
    ):
        with self.lock:
            now = utc_now()
            sent_at = now if status == "sent" else None
            self.connection.execute(
                """UPDATE drafts SET status = ?, provider_message_id = ?, sent_at = ?,
                send_error = ?, updated_at = ?, policy_json = COALESCE(?, policy_json)
                WHERE id = ? AND owner_id = ?""",
                (
                    status,
                    message_id,
                    sent_at,
                    error,
                    now,
                    json.dumps(policy_decision) if policy_decision is not None else None,
                    draft_id,
                    owner_id,
                ),
            )
            row = self.connection.execute(
                "SELECT send_authorization, version, policy_json FROM drafts WHERE id = ? AND owner_id = ?",
                (draft_id, owner_id),
            ).fetchone()
            details = {
                "message_id": message_id,
                "error": error,
                "authorization": row["send_authorization"] if row else None,
                "version": row["version"] if row else None,
                "policy_decision": json.loads(row["policy_json"]) if row and row["policy_json"] else None,
            }
            self._audit(owner_id, f"send_{status}", draft_id, details)
            self.connection.commit()

    def audit_history(self, owner_id: str, limit: int = 100):
        with self.lock:
            rows = self.connection.execute(
                """SELECT id, draft_id, event_type, details_json, created_at
                FROM audit_events WHERE owner_id = ? ORDER BY created_at DESC LIMIT ?""",
                (owner_id, limit),
            ).fetchall()
            return [
                {**dict(row), "details": json.loads(row["details_json"])}
                for row in rows
            ]
