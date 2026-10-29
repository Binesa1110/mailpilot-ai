import json
import hashlib
import os
import threading
import uuid
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import datetime, timedelta, timezone

from .database import ConnectionFacade, create_database_engine


AUTO_SEND_CONSENT_VERSION = 1
_SQLITE_LOCKS: dict[str, threading.RLock] = {}
_SQLITE_LOCKS_GUARD = threading.Lock()


def _sqlite_lock_for(engine):
    key = engine.url.render_as_string(hide_password=False)
    with _SQLITE_LOCKS_GUARD:
        return _SQLITE_LOCKS.setdefault(key, threading.RLock())


class GmailConnectionRequiredError(Exception):
    pass


class AutoSendKillSwitchActiveError(Exception):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_day_bounds() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


class Store:
    def __init__(self, path: str | os.PathLike[str] | None = None, *, engine=None):
        if engine is None:
            path_value = os.fspath(path) if path is not None else None
            if path_value is None or path_value == ":memory:":
                url = "sqlite:///:memory:"
            elif "://" in path_value:
                url = path_value
            else:
                from sqlalchemy.engine import URL

                url = URL.create("sqlite", database=path_value).render_as_string(hide_password=False)
            engine = create_database_engine(url)
        self.engine = engine
        self.path = path or str(engine.url)
        self.connection = ConnectionFacade(engine)
        self.lock = (
            _sqlite_lock_for(engine)
            if engine.dialect.name == "sqlite"
            else nullcontext()
        )

    def dispose(self):
        self.connection.close()
        self.engine.dispose()

    @contextmanager
    def _advisory_lock(self, key: str, *, shared: bool = False):
        if self.engine.dialect.name != "postgresql":
            yield
            return
        lock_key = int.from_bytes(
            hashlib.sha256(f"mailpilot:{key}".encode("utf-8")).digest()[:8],
            byteorder="big",
            signed=True,
        )
        connection = self.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        function = "pg_advisory_lock_shared" if shared else "pg_advisory_lock"
        unlock = "pg_advisory_unlock_shared" if shared else "pg_advisory_unlock"
        acquired = False
        try:
            connection.exec_driver_sql(f"SELECT {function}(%s)", (lock_key,))
            acquired = True
            yield
        finally:
            try:
                if acquired:
                    try:
                        connection.exec_driver_sql(f"SELECT {unlock}(%s)", (lock_key,))
                    except Exception:
                        connection.invalidate()
                        raise
            finally:
                connection.close()

    @contextmanager
    def _owner_lock(self, owner_id: str):
        with self._advisory_lock(f"owner:{owner_id}"):
            yield

    @contextmanager
    def _session_lock(self, session_hash: str | None):
        if session_hash:
            with self._advisory_lock(f"session:{session_hash}"):
                yield
        else:
            yield

    def claim_generation(self, owner_id: str, key: str, request_hash: str):
        with self.lock, self._owner_lock(owner_id):
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
            start, end = utc_day_bounds()
            count = self.connection.execute(
                """SELECT COUNT(*) AS count FROM generation_requests
                WHERE owner_id = ? AND created_at >= ? AND created_at < ?""",
                (owner_id, start, end),
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

    def _audit(self, owner_id: str, event_type: str, draft_id: str | None = None, details=None):
        self.connection.execute(
            "INSERT INTO audit_events VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), owner_id, draft_id, event_type, json.dumps(details or {}), utc_now()),
        )

    def create_session(self, session_hash: str, owner_id: str, max_age_seconds: int):
        with self.lock:
            expires = (datetime.now(timezone.utc) + timedelta(seconds=max_age_seconds)).isoformat()
            self.connection.execute(
                """INSERT INTO auth_sessions VALUES (?, ?, ?)
                ON CONFLICT(session_hash) DO UPDATE SET
                owner_id = excluded.owner_id, expires_at = excluded.expires_at""",
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

    def save_user(self, owner_id: str, email: str):
        with self.lock:
            now = utc_now()
            self.connection.execute(
                """INSERT INTO users (owner_id, email, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(owner_id) DO UPDATE SET
                    email = excluded.email, updated_at = excluded.updated_at""",
                (owner_id, email, now, now),
            )
            self.connection.commit()

    def get_user(self, owner_id: str):
        with self.lock:
            row = self.connection.execute(
                "SELECT owner_id, email, created_at, updated_at FROM users WHERE owner_id = ?",
                (owner_id,),
            ).fetchone()
            return dict(row) if row else None

    def create_oauth_state(
        self,
        state_hash: str,
        intent: str,
        owner_id: str | None,
        session_hash: str | None,
        max_age_seconds: int = 600,
    ):
        with self.lock:
            expires = (datetime.now(timezone.utc) + timedelta(seconds=max_age_seconds)).isoformat()
            self.connection.execute(
                "DELETE FROM oauth_states WHERE expires_at <= ?", (utc_now(),)
            )
            self.connection.execute(
                "INSERT INTO oauth_states VALUES (?, ?, ?, ?, ?)",
                (state_hash, intent, owner_id, session_hash, expires),
            )
            self.connection.commit()

    def oauth_state_intent(self, state_hash: str) -> str | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT intent, expires_at FROM oauth_states WHERE state_hash = ?",
                (state_hash,),
            ).fetchone()
            if not row or row["expires_at"] <= utc_now():
                return None
            return row["intent"]

    def consume_oauth_state(
        self, state_hash: str, intent: str, session_hash: str | None = None
    ):
        with self.lock, self._advisory_lock(f"oauth-state:{state_hash}"):
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                row = self.connection.execute(
                    "SELECT * FROM oauth_states WHERE state_hash = ?",
                    (state_hash,),
                ).fetchone()
                live_session = None
                if intent == "gmail" and session_hash:
                    live_session = self.connection.execute(
                        """SELECT owner_id FROM auth_sessions
                        WHERE session_hash = ? AND expires_at > ?""",
                        (session_hash, utc_now()),
                    ).fetchone()
                if (
                    not row
                    or row["intent"] != intent
                    or row["expires_at"] <= utc_now()
                    or (
                        intent == "gmail"
                        and (
                            not session_hash
                            or row["session_hash"] != session_hash
                            or not row["owner_id"]
                            or not live_session
                            or live_session["owner_id"] != row["owner_id"]
                        )
                    )
                ):
                    self.connection.commit()
                    return None
                self.connection.execute(
                    "DELETE FROM oauth_states WHERE state_hash = ?", (state_hash,)
                )
                self.connection.commit()
                return dict(row)
            except Exception:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise

    def delete_session(self, session_hash: str):
        with self.lock, self._session_lock(session_hash):
            self.connection.execute("DELETE FROM auth_sessions WHERE session_hash = ?", (session_hash,))
            self.connection.commit()

    def save_connection(
        self,
        owner_id: str,
        encrypted: str,
        scopes: list[str],
        account_email: str = "",
        session_hash: str | None = None,
    ) -> bool:
        with self.lock, self._owner_lock(owner_id), self._session_lock(session_hash):
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                if session_hash:
                    session = self.connection.execute(
                        """SELECT owner_id FROM auth_sessions
                        WHERE session_hash = ? AND expires_at > ?""",
                        (session_hash, utc_now()),
                    ).fetchone()
                    if not session or session["owner_id"] != owner_id:
                        self.connection.rollback()
                        return False
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
                self._audit(
                    owner_id,
                    "gmail_connected",
                    details={"send_scope_granted": "https://www.googleapis.com/auth/gmail.send" in scopes},
                )
                self.connection.commit()
                return True
            except Exception:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise

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

    def disconnect_gmail(self, owner_id: str):
        with self.lock, self._owner_lock(owner_id):
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                now = utc_now()
                self.connection.execute("DELETE FROM gmail_connections WHERE owner_id = ?", (owner_id,))
                self.connection.execute(
                    """INSERT INTO account_settings
                    (owner_id, mode, enabled, recipient_allowlist_json, categories_json,
                    daily_limit, consent_version, updated_at)
                    VALUES (?, 'approval_required', 0, '[]', '[]', 5, 0, ?)
                    ON CONFLICT(owner_id) DO UPDATE SET
                    mode = 'approval_required', enabled = 0,
                    recipient_allowlist_json = '[]', categories_json = '[]',
                    daily_limit = 5, consent_version = 0, updated_at = excluded.updated_at""",
                    (owner_id, now),
                )
                self._audit(
                    owner_id,
                    "auto_send_revoked",
                    details={"reason": "gmail_disconnected", "consent_cleared": True},
                )
                self._audit(
                    owner_id,
                    "gmail_disconnected",
                    details={
                        "local_connection_removed": True,
                        "remote_revocation_attempted": False,
                    },
                )
                self.connection.commit()
            except Exception:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise

    def delete_connection(self, owner_id: str):
        self.disconnect_gmail(owner_id)

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
                """INSERT INTO account_settings (owner_id, updated_at)
                VALUES (?, ?) ON CONFLICT(owner_id) DO NOTHING""",
                (owner_id, utc_now()),
            )
            self.connection.commit()
            row = self.connection.execute(
                "SELECT * FROM account_settings WHERE owner_id = ?", (owner_id,)
            ).fetchone()
            return self._settings_dict(row)

    @staticmethod
    def _settings_dict(row):
        consent_is_current = row["consent_version"] == AUTO_SEND_CONSENT_VERSION
        enabled = bool(row["enabled"]) and consent_is_current
        return {
            "mode": row["mode"] if enabled else "approval_required",
            "enabled": enabled,
            "recipient_allowlist": (
                json.loads(row["recipient_allowlist_json"]) if consent_is_current else []
            ),
            "categories": json.loads(row["categories_json"]) if consent_is_current else [],
            "daily_limit": row["daily_limit"],
            "updated_at": row["updated_at"],
        }

    def save_auto_send_settings(
        self, owner_id: str, recipients: list[str], categories: list[str], daily_limit: int
    ):
        from .gmail import GMAIL_SEND_SCOPE

        with self.lock, self._owner_lock(owner_id):
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                connection = self.connection.execute(
                    "SELECT scopes_json FROM gmail_connections WHERE owner_id = ?",
                    (owner_id,),
                ).fetchone()
                if not connection or GMAIL_SEND_SCOPE not in json.loads(connection["scopes_json"]):
                    raise GmailConnectionRequiredError
                if self.kill_switch_enabled():
                    raise AutoSendKillSwitchActiveError

                now = utc_now()
                self.connection.execute(
                    """INSERT INTO account_settings
                    (owner_id, mode, enabled, recipient_allowlist_json, categories_json,
                    daily_limit, consent_version, updated_at)
                    VALUES (?, 'auto_send', 1, ?, ?, ?, ?, ?)
                    ON CONFLICT(owner_id) DO UPDATE SET mode = 'auto_send', enabled = 1,
                    recipient_allowlist_json = excluded.recipient_allowlist_json,
                    categories_json = excluded.categories_json, daily_limit = excluded.daily_limit,
                    consent_version = excluded.consent_version, updated_at = excluded.updated_at""",
                    (
                        owner_id,
                        json.dumps(recipients),
                        json.dumps(categories),
                        daily_limit,
                        AUTO_SEND_CONSENT_VERSION,
                        now,
                    ),
                )
                self._audit(owner_id, "auto_send_enabled", details={
                    "recipient_count": len(recipients),
                    "categories": categories,
                    "daily_limit": daily_limit,
                })
                self.connection.commit()
            except Exception:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise
        return self.get_account_settings(owner_id)

    def disable_auto_send(self, owner_id: str):
        with self.lock, self._owner_lock(owner_id):
            self.connection.execute(
                """INSERT INTO account_settings
                (owner_id, mode, enabled, consent_version, updated_at)
                VALUES (?, 'approval_required', 0, ?, ?)
                ON CONFLICT(owner_id) DO UPDATE SET mode = 'approval_required', enabled = 0,
                consent_version = excluded.consent_version, updated_at = excluded.updated_at""",
                (owner_id, AUTO_SEND_CONSENT_VERSION, utc_now()),
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
        with self.lock, self._advisory_lock("global-send-policy"):
            self.connection.execute(
                """INSERT INTO system_settings (setting_key, setting_value, updated_at)
                VALUES ('auto_send_kill_switch', '1', ?)
                ON CONFLICT(setting_key) DO UPDATE SET setting_value = '1', updated_at = excluded.updated_at""",
                (utc_now(),),
            )
            self._audit(owner_id, "global_auto_send_kill_switch_engaged")
            self.connection.commit()

    def _auto_send_count_today(self, owner_id: str) -> int:
        start, end = utc_day_bounds()
        row = self.connection.execute(
            """SELECT COUNT(*) AS count FROM audit_events
            WHERE owner_id = ? AND event_type = 'auto_send_reserved'
            AND created_at >= ? AND created_at < ?""",
            (owner_id, start, end),
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
    def send_lock(self, owner_id: str, *, auto_send: bool = False):
        with self.lock, ExitStack() as locks:
            if auto_send:
                locks.enter_context(self._advisory_lock("global-send-policy", shared=True))
            locks.enter_context(self._owner_lock(owner_id))
            yield

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

        with self.lock, self._owner_lock(owner_id):
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
