import concurrent.futures
import base64
import hashlib
import json
import sqlite3
import threading
import uuid
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import inspect
from sqlalchemy.engine import URL
from fastapi import FastAPI

from backend.app.gmail import GMAIL_SEND_SCOPE, GmailSendError, create_mime_message
from backend.app import main as main_module
from backend.app.main import (
    AIServiceError,
    EmailAIService,
    EmailDraft,
    EmailRequest,
    EmailReview,
    app,
)
from backend.app.frontend import SpaStaticFiles
from backend.app.policy import evaluate_auto_send
from backend.app.schema import metadata
from backend.app.storage import GmailConnectionRequiredError, Store

client = TestClient(app, headers={"Origin": "http://localhost:5173"})
SESSION = "test-session-token-000000000000"
OWNER = "jordan@example.com"
valid_request = {
    "recipient": "jordan@example.com",
    "purpose": "Thank them for the interview",
    "context": "The interview was yesterday for the design role.",
    "category": "Job application",
    "tone": "Warm & professional",
    "language": "English",
    "signature": "Best, Sam",
}


def _test_store(path=":memory:", *, migrate=False):
    store = Store(str(path))
    if migrate:
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        with store.engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    else:
        metadata.create_all(store.engine)
    return store


class SuccessfulAI:
    def generate_draft(self, request):
        return EmailDraft(subject="Thank you for the conversation", body="Hello Jordan,\n\nThank you for meeting with me.\n\nBest, Sam")

    def review_draft(self, request, draft):
        return EmailReview(notes=["Personalize with a specific interview detail."], risk_flags=[])


class ClearReviewAI(SuccessfulAI):
    def review_draft(self, request, draft):
        return EmailReview(notes=[], risk_flags=[])


class FailingAI:
    def generate_draft(self, request):
        raise AIServiceError("The AI service is temporarily unavailable. Please try again.")

    def review_draft(self, request, draft):
        raise AssertionError("Review should not run after generation fails.")


class MalformedAI:
    def generate_draft(self, request):
        raise AIServiceError("The AI service returned an invalid or oversized email draft.")

    def review_draft(self, request, draft):
        raise AssertionError("Review should not run after malformed output.")


def setup_function():
    app.state.ai_service = SuccessfulAI()
    app.state.store = _test_store()
    app.state.gmail_sender = None
    app.state.store.create_session(hashlib.sha256(SESSION.encode()).hexdigest(), OWNER, 3600)
    client.cookies.clear()
    client.cookies.set("mailpilot_session", SESSION)


def teardown_function():
    store = getattr(app.state, "store", None)
    if store:
        store.dispose()


def _generate(payload=valid_request, key=None):
    return client.post(
        "/api/emails/generate",
        headers={"Idempotency-Key": key or str(uuid.uuid4())},
        json=payload,
    )


def _enable_auto_send(recipients=None, categories=None, daily_limit=5):
    _connect_gmail()
    return client.post(
        "/api/settings/auto-send/enable",
        json={
            "confirm_phrase": "ENABLE AUTO-SEND",
            "consequences_acknowledged": True,
            "recipient_allowlist": recipients or ["jordan@example.com"],
            "categories": categories or ["job application"],
            "daily_limit": daily_limit,
        },
    )


class MockSender:
    def __init__(self):
        self.calls = 0

    def send(self, encrypted, key, recipient, subject, body):
        self.calls += 1
        return f"gmail-message-{self.calls}"


def test_health_endpoint():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_gmail_mime_message_is_encoded_and_rejects_header_injection():
    encoded = create_mime_message("jordan@example.com", "A subject", "A body")
    message = BytesParser(policy=default).parsebytes(
        base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    )
    assert message["To"] == "jordan@example.com"
    assert message["Subject"] == "A subject"
    assert message.get_content().strip() == "A body"
    with pytest.raises(ValueError):
        create_mime_message("jordan@example.com", "Hello\r\nBcc: attacker@example.com", "A body")


def test_generate_validates_request_fields():
    response = _generate({**valid_request, "purpose": "  "})
    assert response.status_code == 422


def test_generate_rejects_oversized_context():
    response = _generate({**valid_request, "context": "x" * 5001})
    assert response.status_code == 422


def test_generate_returns_reviewed_draft():
    response = _generate()
    assert response.status_code == 200
    result = response.json()
    assert result["subject"] == "Thank you for the conversation"
    assert result["review"]["notes"] == ["Personalize with a specific interview detail."]
    assert result["review"]["risk_flags"] == []
    assert result["id"]
    assert result["status"] == "draft"
    assert result["version"] == 1
    assert result["send_authorization"] == "manual_approval_required"


def test_auto_send_defaults_to_approval_required():
    response = client.get("/api/settings/auto-send")
    assert response.status_code == 200
    assert response.json()["mode"] == "approval_required"
    assert response.json()["enabled"] is False
    assert response.json()["recipient_allowlist"] == []


def test_auto_send_requires_connection_confirmation_and_explicit_consent():
    payload = {
        "confirm_phrase": "ENABLE AUTO-SEND",
        "consequences_acknowledged": True,
        "recipient_allowlist": ["jordan@example.com"],
        "categories": ["professional"],
        "daily_limit": 5,
    }
    assert client.post("/api/settings/auto-send/enable", json=payload).status_code == 409
    _connect_gmail()
    without_ack = {**payload, "consequences_acknowledged": False}
    assert client.post("/api/settings/auto-send/enable", json=without_ack).status_code == 422
    wrong_phrase = {**payload, "confirm_phrase": "enable"}
    assert client.post("/api/settings/auto-send/enable", json=wrong_phrase).status_code == 422


def test_gmail_disconnect_clears_auto_send_consent_and_is_owner_scoped():
    assert _enable_auto_send().status_code == 200
    other_owner = "other@example.com"
    app.state.store.save_connection(other_owner, "other-encrypted-placeholder", [GMAIL_SEND_SCOPE])
    app.state.store.save_auto_send_settings(
        other_owner,
        ["other@example.com"],
        ["professional"],
        2,
    )

    response = client.delete("/api/auth/gmail")
    assert response.status_code == 200
    assert response.json() == {
        "status": "disconnected",
        "auto_send_consent_cleared": True,
        "remote_revocation_attempted": False,
    }

    settings = client.get("/api/settings/auto-send").json()
    assert settings["enabled"] is False
    assert settings["mode"] == "approval_required"
    assert settings["recipient_allowlist"] == []
    assert settings["categories"] == []
    assert settings["gmail_connected"] is False
    assert app.state.store.get_connection(OWNER) is None

    other_settings = app.state.store.get_account_settings(other_owner)
    assert other_settings["enabled"] is True
    assert app.state.store.get_connection(other_owner) is not None
    audit_types = {event["event_type"] for event in client.get("/api/audit").json()}
    assert {"auto_send_revoked", "gmail_disconnected"} <= audit_types


def test_reconnecting_gmail_does_not_restore_auto_send_consent():
    app.state.ai_service = ClearReviewAI()
    assert _enable_auto_send().status_code == 200
    assert client.delete("/api/auth/gmail").status_code == 200
    _connect_gmail()

    settings = client.get("/api/settings/auto-send").json()
    assert settings["gmail_connected"] is True
    assert settings["enabled"] is False
    assert settings["mode"] == "approval_required"
    assert settings["recipient_allowlist"] == []
    assert settings["categories"] == []

    sender = MockSender()
    app.state.gmail_sender = sender
    response = _generate()
    assert response.status_code == 200
    assert response.json()["status"] == "draft"
    assert "auto_send_not_enabled" in response.json()["policy_decision"]["reasons"]
    assert sender.calls == 0


def test_reconnected_gmail_requires_and_accepts_explicit_auto_send_reconsent():
    app.state.ai_service = ClearReviewAI()
    assert _enable_auto_send().status_code == 200
    assert client.delete("/api/auth/gmail").status_code == 200
    _connect_gmail()
    assert client.get("/api/settings/auto-send").json()["enabled"] is False

    assert _enable_auto_send().status_code == 200
    assert client.get("/api/settings/auto-send").json()["enabled"] is True
    sender = MockSender()
    app.state.gmail_sender = sender
    response = _generate()
    assert response.status_code == 200
    assert response.json()["status"] == "sent"
    assert response.json()["send_authorization"] == "auto_policy"
    assert sender.calls == 1


def test_disconnect_consent_revocation_persists_after_store_restart(tmp_path):
    database = tmp_path / "disconnect.sqlite3"
    store = _test_store(database)
    store.save_connection(OWNER, "encrypted-placeholder", [GMAIL_SEND_SCOPE])
    store.save_auto_send_settings(OWNER, ["jordan@example.com"], ["job application"], 3)
    store.disconnect_gmail(OWNER)
    store.dispose()

    reopened = _test_store(database)
    settings = reopened.get_account_settings(OWNER)
    assert settings["enabled"] is False
    assert settings["mode"] == "approval_required"
    assert settings["recipient_allowlist"] == []
    assert settings["categories"] == []
    assert reopened.get_connection(OWNER) is None

    reopened.save_connection(OWNER, "reconnected-placeholder", [GMAIL_SEND_SCOPE])
    assert reopened.get_account_settings(OWNER)["enabled"] is False
    assert reopened.get_account_settings(OWNER)["mode"] == "approval_required"
    events = reopened.connection.execute(
        "SELECT event_type FROM audit_events WHERE owner_id = ?",
        (OWNER,),
    ).fetchall()
    assert {"auto_send_revoked", "gmail_disconnected"} <= {row["event_type"] for row in events}
    reopened.dispose()


def test_legacy_enabled_settings_do_not_restore_auto_send_after_migration(tmp_path):
    database = tmp_path / "legacy-settings.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        """CREATE TABLE account_settings (
        owner_id TEXT PRIMARY KEY,
        mode TEXT NOT NULL DEFAULT 'approval_required',
        enabled INTEGER NOT NULL DEFAULT 0,
        recipient_allowlist_json TEXT NOT NULL DEFAULT '[]',
        categories_json TEXT NOT NULL DEFAULT '[]',
        daily_limit INTEGER NOT NULL DEFAULT 5,
        updated_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """INSERT INTO account_settings
        (owner_id, mode, enabled, recipient_allowlist_json, categories_json, daily_limit, updated_at)
        VALUES (?, 'auto_send', 1, ?, ?, 5, '2026-01-01T00:00:00+00:00')""",
        (OWNER, json.dumps(["jordan@example.com"]), json.dumps(["job application"])),
    )
    connection.commit()
    connection.close()

    store = _test_store(database, migrate=True)
    store.save_connection(OWNER, "reconnected-placeholder", [GMAIL_SEND_SCOPE])
    settings = store.get_account_settings(OWNER)
    assert settings["enabled"] is False
    assert settings["mode"] == "approval_required"
    assert settings["recipient_allowlist"] == []
    assert settings["categories"] == []
    decision = evaluate_auto_send(
        {
            "status": "draft",
            "recipient": "jordan@example.com",
            "subject": "A note",
            "body": "Hello Jordan.",
            "category": "Job application",
            "review": {"notes": [], "risk_flags": []},
        },
        settings,
        0,
        True,
        False,
    )
    assert not decision.allowed
    assert "auto_send_not_enabled" in decision.reasons
    store.dispose()


def test_auto_send_cannot_be_enabled_while_kill_switch_is_active(monkeypatch):
    _connect_gmail()
    monkeypatch.setenv("MAILPILOT_AUTO_SEND_KILL_SWITCH", "true")
    response = client.post(
        "/api/settings/auto-send/enable",
        json={
            "confirm_phrase": "ENABLE AUTO-SEND",
            "consequences_acknowledged": True,
            "recipient_allowlist": ["jordan@example.com"],
            "categories": ["job application"],
            "daily_limit": 5,
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "The global auto-send kill switch is active."
    assert client.get("/api/settings/auto-send").json()["enabled"] is False


def test_concurrent_disconnect_and_auto_send_enable_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILPILOT_AUTO_SEND_KILL_SWITCH", "false")
    database = tmp_path / "concurrent-disconnect.sqlite3"
    enabling_store = _test_store(database)
    disconnecting_store = _test_store(database)
    enabling_store.save_connection(OWNER, "encrypted-placeholder", [GMAIL_SEND_SCOPE])
    barrier = threading.Barrier(2)

    def enable():
        barrier.wait()
        try:
            enabling_store.save_auto_send_settings(
                OWNER,
                ["jordan@example.com"],
                ["job application"],
                5,
            )
            return "enabled"
        except GmailConnectionRequiredError:
            return "blocked"

    def disconnect():
        barrier.wait()
        disconnecting_store.disconnect_gmail(OWNER)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        enabling = pool.submit(enable)
        disconnecting = pool.submit(disconnect)
        enable_result = enabling.result(timeout=10)
        disconnecting.result(timeout=10)

    assert enable_result in {"enabled", "blocked"}
    final_store = _test_store(database)
    settings = final_store.get_account_settings(OWNER)
    assert settings["enabled"] is False
    assert settings["mode"] == "approval_required"
    assert settings["recipient_allowlist"] == []
    assert settings["categories"] == []
    assert final_store.get_connection(OWNER) is None
    enabling_store.dispose()
    disconnecting_store.dispose()
    final_store.dispose()


def test_auto_send_sends_only_policy_eligible_draft():
    app.state.ai_service = ClearReviewAI()
    assert _enable_auto_send().status_code == 200
    sender = MockSender()
    app.state.gmail_sender = sender
    response = _generate()
    assert response.status_code == 200
    draft = response.json()
    assert draft["status"] == "sent"
    assert draft["send_authorization"] == "auto_policy"
    assert draft["policy_decision"]["allowed"] is True
    assert draft["policy_decision"]["reasons"] == []
    assert len(draft["policy_decision"]["draft_hash"]) == 64
    assert draft["message_id"] == "gmail-message-1"
    events = client.get("/api/audit").json()
    sent_event = next(event for event in events if event["event_type"] == "send_sent")
    assert sent_event["details"]["authorization"] == "auto_policy"
    assert sent_event["details"]["policy_decision"]["allowed"] is True
    assert sender.calls == 1


@pytest.mark.parametrize(
    ("body", "review", "reason"),
    [
        ("Hello {{name}}, your message is ready.", EmailReview(), "placeholder_detected"),
        ("Hello Jordan, as promised we will deliver by Friday.", EmailReview(), "potential_unsupported_claim"),
        ("Hello Jordan, the contract is binding.", EmailReview(), "sensitive_or_high_risk_content"),
        ("Hello Jordan.", EmailReview(risk_flags=["Potential unsupported commitment."]), "review_risk_flags_present"),
        ("Hello Jordan.", EmailReview(notes=["Verify the factual claim."]), "review_notes_require_manual_review"),
    ],
)
def test_auto_send_policy_requires_manual_review_for_risky_drafts(body, review, reason):
    settings = {
        "mode": "auto_send",
        "enabled": True,
        "recipient_allowlist": ["jordan@example.com"],
        "categories": ["professional"],
        "daily_limit": 5,
    }
    decision = evaluate_auto_send(
        {
            "status": "draft",
            "recipient": "jordan@example.com",
            "subject": "A note",
            "body": body,
            "category": "Professional",
            "review": review.model_dump(),
        },
        settings,
        0,
        True,
        False,
    )
    assert not decision.allowed
    assert reason in decision.reasons


def test_auto_send_policy_blocks_unclassified_category_and_multiple_recipients():
    settings = {
        "mode": "auto_send",
        "enabled": True,
        "recipient_allowlist": ["jordan@example.com"],
        "categories": ["professional"],
        "daily_limit": 5,
    }
    draft = {
        "status": "draft",
        "recipient": "jordan@example.com, other@example.com",
        "subject": "A note",
        "body": "Hello.",
        "category": "Miscellaneous",
        "review": {"notes": [], "risk_flags": []},
    }
    decision = evaluate_auto_send(draft, settings, 0, True, False)
    assert "category_unclassified" in decision.reasons
    assert "recipient_not_allowlisted_or_invalid" in decision.reasons
    assert "multiple_or_unusual_recipients" in decision.reasons


def test_auto_send_recipient_allowlist_violation_requires_manual_approval():
    app.state.ai_service = ClearReviewAI()
    _enable_auto_send(recipients=["jordan@example.com"])
    app.state.gmail_sender = MockSender()
    response = _generate({**valid_request, "recipient": "other@example.com"})
    assert response.status_code == 200
    assert response.json()["status"] == "draft"
    assert "recipient_not_allowlisted_or_invalid" in response.json()["policy_decision"]["reasons"]
    assert app.state.gmail_sender.calls == 0


def test_auto_send_revocation_blocks_future_auto_sends():
    app.state.ai_service = ClearReviewAI()
    _enable_auto_send()
    assert client.post("/api/settings/auto-send/disable").status_code == 200
    sender = MockSender()
    app.state.gmail_sender = sender
    response = _generate()
    assert response.json()["status"] == "draft"
    assert "auto_send_not_enabled" in response.json()["policy_decision"]["reasons"]
    assert sender.calls == 0


def test_missing_gmail_permission_blocks_auto_send():
    app.state.ai_service = ClearReviewAI()
    _enable_auto_send()
    app.state.store.delete_connection(OWNER)
    sender = MockSender()
    app.state.gmail_sender = sender
    response = _generate()
    assert response.json()["status"] == "draft"
    assert "gmail_send_permission_missing" in response.json()["policy_decision"]["reasons"]
    assert sender.calls == 0


def test_global_kill_switch_blocks_auto_send(monkeypatch):
    app.state.ai_service = ClearReviewAI()
    _enable_auto_send()
    app.state.gmail_sender = MockSender()
    monkeypatch.setenv("MAILPILOT_ADMIN_OWNER_IDS", OWNER)
    assert client.post("/api/settings/auto-send/kill-switch").json()["kill_switch_enabled"] is True
    response = _generate()
    assert "global_kill_switch_enabled" in response.json()["policy_decision"]["reasons"]
    assert response.json()["status"] == "draft"
    assert app.state.gmail_sender.calls == 0


def test_global_kill_switch_requires_configured_operator():
    response = client.post("/api/settings/auto-send/kill-switch")
    assert response.status_code == 403


def test_auto_send_daily_limit_is_persistent_and_policy_enforced():
    app.state.ai_service = ClearReviewAI()
    _enable_auto_send(daily_limit=1)
    sender = MockSender()
    app.state.gmail_sender = sender
    first = _generate()
    second = _generate()
    assert first.json()["status"] == "sent"
    assert second.json()["status"] == "draft"
    assert "daily_auto_send_limit_reached" in second.json()["policy_decision"]["reasons"]
    assert sender.calls == 1


def test_auto_send_generation_idempotency_prevents_repeated_submissions():
    app.state.ai_service = ClearReviewAI()
    _enable_auto_send()
    sender = MockSender()
    app.state.gmail_sender = sender
    key = "generation-idempotency-key-001"
    first = _generate(key=key)
    repeated = _generate(key=key)
    assert first.status_code == repeated.status_code == 200
    assert first.json()["id"] == repeated.json()["id"]
    assert first.json()["message_id"] == repeated.json()["message_id"]
    assert sender.calls == 1


def test_auto_send_policy_rechecks_draft_version_before_reserving_send():
    store = app.state.store
    key = "test-draft-generation-key"
    store.claim_generation(OWNER, key, "request-fingerprint")
    draft = store.create_draft(
        OWNER,
        "jordan@example.com",
        "A thoughtful note",
        "Hello Jordan.",
        {"notes": [], "risk_flags": []},
        "Job application",
        key,
    )
    _enable_auto_send()
    settings = store.get_account_settings(OWNER)
    decision = evaluate_auto_send(
        draft,
        settings,
        0,
        True,
        False,
    ).as_dict()
    store.connection.execute("UPDATE drafts SET version = 2 WHERE id = ?", (draft["id"],))
    store.connection.commit()
    state, result = store.begin_auto_send(
        OWNER,
        draft["id"],
        "draft-version-idempotency-key",
        1,
        decision,
    )
    assert state == "blocked"
    assert result["policy_decision"]["reasons"] == ["draft_version_changed"]


def test_auto_send_policy_binds_exact_body_even_if_version_was_not_incremented():
    store = app.state.store
    key = "test-draft-generation-key-body"
    store.claim_generation(OWNER, key, "request-fingerprint")
    draft = store.create_draft(
        OWNER,
        "jordan@example.com",
        "A thoughtful note",
        "Hello Jordan.",
        {"notes": [], "risk_flags": []},
        "Job application",
        key,
    )
    _enable_auto_send()
    decision = evaluate_auto_send(
        draft,
        store.get_account_settings(OWNER),
        0,
        True,
        False,
    ).as_dict()
    decision["draft_hash"] = store._content_hash(
        draft["recipient"], draft["subject"], draft["body"], draft["version"]
    )
    store.connection.execute(
        "UPDATE drafts SET body = ? WHERE id = ?", ("Changed without incrementing version", draft["id"])
    )
    store.connection.commit()
    state, result = store.begin_auto_send(
        OWNER,
        draft["id"],
        "draft-body-idempotency-key",
        draft["version"],
        decision,
    )
    assert state == "blocked"
    assert result["policy_decision"]["reasons"] == ["draft_content_changed"]


def test_generation_idempotency_key_cannot_be_reused_for_different_request():
    key = "generation-idempotency-key-002"
    assert _generate(key=key).status_code == 200
    conflict = _generate({**valid_request, "purpose": "Different purpose"}, key=key)
    assert conflict.status_code == 409


def test_generation_cannot_enable_auto_send_through_ai_or_request_fields():
    response = _generate({**valid_request, "auto_send": True})
    assert response.status_code == 422
    assert client.get("/api/settings/auto-send").json()["enabled"] is False


def test_generation_daily_abuse_limit_is_persistent():
    for index in range(50):
        state, _ = app.state.store.claim_generation(OWNER, f"abuse-limit-key-{index:03}", str(index))
        assert state == "claimed"
    response = _generate(key="generation-over-limit-0001")
    assert response.status_code == 429


def test_auto_send_account_settings_persist_across_store_reopen(tmp_path):
    database = tmp_path / "mailpilot.sqlite3"
    store = _test_store(database)
    store.save_connection(OWNER, "encrypted-placeholder", [GMAIL_SEND_SCOPE])
    store.save_auto_send_settings(
        OWNER,
        ["Jordan@Example.COM"],
        ["job application"],
        3,
    )
    store.dispose()
    reopened = _test_store(database)
    settings = reopened.get_account_settings(OWNER)
    assert settings["enabled"] is True
    assert settings["mode"] == "auto_send"
    assert settings["recipient_allowlist"] == ["Jordan@Example.COM"]
    assert settings["daily_limit"] == 3
    reopened.dispose()


def test_concurrent_repeated_send_requests_call_provider_once():
    draft_id = _create_approved_draft()
    _connect_gmail()
    entered_provider = threading.Event()
    release_provider = threading.Event()

    class BlockingSender:
        calls = 0

        def send(self, *args):
            self.calls += 1
            entered_provider.set()
            assert release_provider.wait(timeout=10)
            return "gmail-message-concurrent"

    sender = BlockingSender()
    app.state.gmail_sender = sender
    second_client = TestClient(app, headers={"Origin": "http://localhost:5173"})
    second_client.cookies.set("mailpilot_session", SESSION)
    key = "concurrent-idempotency-key-001"
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_send, draft_id, key)
        assert entered_provider.wait(timeout=10)
        second = pool.submit(
            second_client.post,
            f"/api/drafts/{draft_id}/send",
            headers={"Idempotency-Key": key},
        )
        release_provider.set()
        first_response = first.result(timeout=15)
        second_response = second.result(timeout=15)
    assert first_response.status_code == second_response.status_code == 200
    assert first_response.json()["message_id"] == second_response.json()["message_id"]
    assert sender.calls == 1




def test_generate_requires_authentication():
    client.cookies.clear()
    response = _generate()
    assert response.status_code == 401


def test_storage_schema_is_created_only_by_explicit_alembic_migration(tmp_path):
    store = Store(str(tmp_path / "schema.sqlite3"))
    assert "drafts" not in set(inspect(store.engine).get_table_names())
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with store.engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    tables = set(inspect(store.engine).get_table_names())
    assert {
        "drafts",
        "auth_sessions",
        "users",
        "oauth_states",
        "gmail_connections",
        "account_settings",
        "system_settings",
        "generation_requests",
        "audit_events",
    } <= tables
    store.dispose()


def test_application_lifespan_uses_migrated_database_and_disposes_store(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "APP_ENV", "development")
    database = tmp_path / "lifespan.sqlite3"
    url = URL.create("sqlite", database=str(database)).render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", url)
    store = _test_store(database, migrate=True)
    store.dispose()
    disposed = []
    dispose = Store.dispose

    def track_disposal(instance):
        disposed.append(instance)
        dispose(instance)

    monkeypatch.setattr(Store, "dispose", track_disposal)

    with TestClient(app) as lifespan_client:
        assert lifespan_client.get("/api/health").json() == {"status": "ok"}
        active_store = app.state.store
    assert active_store in disposed


def test_single_origin_frontend_routing_serves_spa_and_preserves_api(tmp_path):
    (tmp_path / "index.html").write_text("<html>MailPilot SPA</html>", encoding="utf-8")
    (tmp_path / "asset.js").write_text("window.mailpilot = true;", encoding="utf-8")
    frontend_app = FastAPI()

    @frontend_app.get("/api/health")
    def health():
        return {"status": "ok"}

    frontend_app.mount(
        "/",
        SpaStaticFiles(directory=str(tmp_path), html=True, check_dir=True),
        name="frontend",
    )
    with TestClient(frontend_app) as frontend_client:
        assert "MailPilot SPA" in frontend_client.get("/").text
        assert "MailPilot SPA" in frontend_client.get("/drafts/example").text
        assert frontend_client.get("/asset.js").text == "window.mailpilot = true;"
        assert frontend_client.get("/api/health").json() == {"status": "ok"}
        missing_api = frontend_client.get("/api/not-a-route")
        assert missing_api.status_code == 404
        assert missing_api.json() == {"detail": "Not Found"}


def test_production_configuration_rejects_missing_secrets_and_http_origins(monkeypatch):
    monkeypatch.setattr(main_module, "APP_ENV", "production")
    for name in (
        "DATABASE_URL",
        "OPENAI_API_KEY",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_REDIRECT_URI",
        "MAILPILOT_TOKEN_ENCRYPTION_KEY",
        "MAILPILOT_FRONTEND_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    errors = main_module._production_configuration_errors()
    assert any("DATABASE_URL is required" in error for error in errors)
    assert any("OPENAI_API_KEY is required" in error for error in errors)
    assert any("MAILPILOT_FRONTEND_URL is required" in error for error in errors)

    monkeypatch.setenv("DATABASE_URL", "sqlite:///not-for-production.db")
    monkeypatch.setenv("MAILPILOT_FRONTEND_URL", "http://mailpilot.example")
    monkeypatch.setenv(
        "GOOGLE_REDIRECT_URI",
        "http://mailpilot.example/api/auth/google/callback",
    )
    errors = main_module._production_configuration_errors()
    assert any("managed PostgreSQL" in error for error in errors)
    assert any("HTTPS origin" in error for error in errors)
    assert any("HTTPS Google OAuth callback" in error for error in errors)


def test_production_configuration_accepts_same_origin_https_settings(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setattr(main_module, "APP_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://user:password@postgres.example:5432/mailpilot",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "configured-test-placeholder")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "configured-test-placeholder")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "configured-test-placeholder")
    monkeypatch.setenv(
        "GOOGLE_REDIRECT_URI",
        "https://mailpilot.example/api/auth/google/callback",
    )
    monkeypatch.setenv(
        "MAILPILOT_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    monkeypatch.setenv("MAILPILOT_FRONTEND_URL", "https://mailpilot.example")
    assert main_module._production_configuration_errors() == []
    assert main_module._secure_cookies() is True


def test_storage_facade_commits_and_rolls_back_explicit_transactions():
    store = app.state.store
    store.connection.execute(
        "INSERT INTO users (owner_id, email, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("rollback-user", "rollback@example.com", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )
    store.connection.rollback()
    assert store.get_user("rollback-user") is None

    store.connection.execute(
        "INSERT INTO users (owner_id, email, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("committed-user", "committed@example.com", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )
    store.connection.commit()
    assert store.get_user("committed-user")["email"] == "committed@example.com"


def test_cookie_authenticated_mutations_reject_missing_or_untrusted_origin():
    response = client.post(
        "/api/settings/auto-send/disable",
        headers={"Origin": "https://attacker.example"},
    )
    assert response.status_code == 403
    no_origin = client.post(
        "/api/settings/auto-send/disable",
        headers={"Origin": ""},
    )
    assert no_origin.status_code == 403


def test_production_session_cookies_are_always_secure(monkeypatch):
    monkeypatch.setattr(main_module, "APP_ENV", "production")
    monkeypatch.setenv("MAILPILOT_COOKIE_SECURE", "false")
    assert main_module._secure_cookies() is True
    monkeypatch.setattr(main_module, "APP_ENV", "development")
    assert main_module._secure_cookies() is False
    monkeypatch.setenv("MAILPILOT_COOKIE_SECURE", "true")
    assert main_module._secure_cookies() is True


def test_oauth_state_cookie_uses_secure_attributes_in_production(monkeypatch):
    class FakeFlow:
        def authorization_url(self, **kwargs):
            return "https://accounts.google.com/mock", "oauth-state"

    monkeypatch.setattr(main_module, "APP_ENV", "production")
    monkeypatch.setattr(main_module, "_oauth_flow", lambda **kwargs: FakeFlow())
    response = client.get("/api/auth/google/start", follow_redirects=False)
    assert response.status_code == 302
    cookie = response.headers["set-cookie"].lower()
    assert "secure" in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie


def test_oauth_callback_rejects_mismatched_state():
    response = client.get(
        "/api/auth/google/callback?code=not-used&state=attacker-state",
        headers={"Cookie": "mailpilot_oauth_state=expected-state"},
    )
    assert response.status_code == 400


def test_oauth_flow_requests_identity_only_or_minimum_gmail_scopes(monkeypatch):
    captured = []

    class FakeFlow:
        @classmethod
        def from_client_config(cls, config, scopes, **kwargs):
            captured.append(scopes)
            return cls()

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(main_module.Flow, "from_client_config", FakeFlow.from_client_config)
    main_module._oauth_flow(intent="sign_in")
    main_module._oauth_flow(intent="gmail")
    assert captured == [
        ["openid", "email", "profile"],
        ["openid", "email", GMAIL_SEND_SCOPE],
    ]


class MockOAuthCredentials:
    def __init__(self, scopes):
        self.scopes = scopes
        self.id_token = "mock-id-token"

    def has_scopes(self, requested):
        return set(requested).issubset(self.scopes)

    def to_json(self):
        return '{"token": "mock-only"}'


def _mock_oauth_flow(monkeypatch, states, scopes_by_intent):
    class FakeFlow:
        def __init__(self, intent):
            self.intent = intent
            self.credentials = MockOAuthCredentials(scopes_by_intent[intent])

        def authorization_url(self, **kwargs):
            state = states[self.intent]
            return f"https://accounts.google.com/mock?state={state}", state

        def fetch_token(self, **kwargs):
            return None

    monkeypatch.setattr(
        main_module,
        "_oauth_flow",
        lambda state=None, intent="sign_in": FakeFlow(intent),
    )


def _mock_verified_identity(monkeypatch, email="person@example.com", subject="person-sub"):
    monkeypatch.setattr(
        main_module.id_token,
        "verify_oauth2_token",
        lambda *args, **kwargs: {
            "sub": subject,
            "email": email,
            "email_verified": True,
        },
    )


def test_google_identity_sign_in_creates_session_without_gmail(monkeypatch):
    monkeypatch.setattr(main_module, "APP_ENV", "development")
    _mock_oauth_flow(
        monkeypatch,
        {"sign_in": "identity-only-state"},
        {"sign_in": list(main_module.IDENTITY_SCOPES)},
    )
    _mock_verified_identity(monkeypatch, subject="identity-only-user")

    start = client.get("/api/auth/google/sign-in", follow_redirects=False)
    assert start.status_code == 302
    callback = client.get(
        "/api/auth/google/callback?code=mock-code&state=identity-only-state",
        follow_redirects=False,
    )
    assert callback.status_code == 303
    session = client.get("/api/auth/session")
    assert session.status_code == 200
    assert session.json()["owner_id"] == "google:identity-only-user"
    assert app.state.store.get_connection("google:identity-only-user") is None
    assert session.json()["user_email"] == "person@example.com"
    assert session.json()["gmail_connected"] is False


def test_gmail_connection_is_optional_and_bound_to_signed_in_owner(monkeypatch):
    monkeypatch.setattr(main_module, "APP_ENV", "development")
    from cryptography.fernet import Fernet

    monkeypatch.setenv("MAILPILOT_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    states = {"sign_in": "signin-for-gmail-state", "gmail": "gmail-link-state"}
    _mock_oauth_flow(
        monkeypatch,
        states,
        {
            "sign_in": list(main_module.IDENTITY_SCOPES),
            "gmail": list(main_module.GMAIL_CONNECTION_SCOPES),
        },
    )
    identities = iter(
        [
            {"sub": "person-sub", "email": "person@example.com", "email_verified": True},
            {"sub": "gmail-sub", "email": "connected@gmail.com", "email_verified": True},
        ]
    )
    monkeypatch.setattr(
        main_module.id_token,
        "verify_oauth2_token",
        lambda *args, **kwargs: next(identities),
    )
    client.get("/api/auth/google/sign-in", follow_redirects=False)
    signed_in = client.get(
        f"/api/auth/google/callback?code=identity-code&state={states['sign_in']}",
        follow_redirects=False,
    )
    assert signed_in.status_code == 303
    owner_id = "google:person-sub"
    assert app.state.store.get_connection(owner_id) is None

    started = client.get("/api/auth/google/connect-gmail", follow_redirects=False)
    assert started.status_code == 302
    connected = client.get(
        f"/api/auth/google/callback?code=gmail-code&state={states['gmail']}",
        follow_redirects=False,
    )
    assert connected.status_code == 303
    assert app.state.store.get_connection(owner_id)["account_email"] == "connected@gmail.com"
    assert app.state.store.get_connection("google:gmail-sub") is None
    session = client.get("/api/auth/session").json()
    assert session["gmail_connected"] is True
    assert app.state.store.get_account_settings(owner_id)["enabled"] is False
    assert any(event["event_type"] == "gmail_connected" for event in client.get("/api/audit").json())


def test_oauth_valid_state_still_rejects_state_cookie_mismatch(monkeypatch):
    monkeypatch.setattr(main_module, "APP_ENV", "development")
    _mock_oauth_flow(
        monkeypatch,
        {"sign_in": "valid-state-cookie-mismatch"},
        {"sign_in": list(main_module.IDENTITY_SCOPES)},
    )
    client.get("/api/auth/google/sign-in", follow_redirects=False)
    client.cookies.set(
        main_module.OAUTH_COOKIE, "different-state", domain="testserver.local", path="/"
    )
    response = client.get(
        "/api/auth/google/callback?code=unused&state=valid-state-cookie-mismatch"
    )
    assert response.status_code == 400


def test_gmail_callback_requires_the_initiating_session_and_owner(monkeypatch):
    monkeypatch.setattr(main_module, "APP_ENV", "development")
    states = {"gmail": "session-bound-gmail-state"}
    _mock_oauth_flow(
        monkeypatch,
        states,
        {"gmail": set(main_module.GMAIL_CONNECTION_SCOPES)},
    )
    other_session = "another-user-session"
    other_owner = "google:another-user"
    app.state.store.create_session(
        hashlib.sha256(other_session.encode()).hexdigest(), other_owner, 3600
    )
    assert client.get("/api/auth/google/connect-gmail", follow_redirects=False).status_code == 302
    client.cookies.set("mailpilot_session", other_session)
    response = client.get(
        f"/api/auth/google/callback?code=unused&state={states['gmail']}"
    )
    assert response.status_code == 400
    assert app.state.store.get_connection(OWNER) is None
    assert app.state.store.get_connection(other_owner) is None


def test_google_account_owner_uses_stable_verified_subject_not_email():
    first = main_module._owner_id_from_identity(
        {"sub": "google-user-001", "email": "reassigned@example.com", "email_verified": True}
    )
    same_subject_new_email = main_module._owner_id_from_identity(
        {"sub": "google-user-001", "email": "new-address@example.com", "email_verified": True}
    )
    reassigned_email_new_subject = main_module._owner_id_from_identity(
        {"sub": "google-user-002", "email": "reassigned@example.com", "email_verified": True}
    )
    assert first == same_subject_new_email == "google:google-user-001"
    assert first != reassigned_email_new_subject
    with pytest.raises(ValueError):
        main_module._owner_id_from_identity(
            {"sub": "google-user-003", "email": "unverified@example.com", "email_verified": False}
        )


def test_ai_provider_failure_is_reported():
    app.state.ai_service = FailingAI()
    response = _generate()
    assert response.status_code == 502
    assert "temporarily unavailable" in response.json()["detail"]


def test_malformed_ai_output_is_rejected():
    app.state.ai_service = MalformedAI()
    response = _generate()
    assert response.status_code == 502
    assert "invalid or oversized" in response.json()["detail"]


def test_ai_service_rejects_malformed_json_response():
    class FakeResponse:
        output_text = "this is not JSON"

    class FakeResponses:
        def create(self, **kwargs):
            return FakeResponse()

    class FakeClient:
        responses = FakeResponses()

    service = EmailAIService()
    service.client = FakeClient()
    with pytest.raises(AIServiceError, match="malformed JSON"):
        service.generate_draft(EmailRequest.model_validate(valid_request))


@pytest.mark.parametrize(
    ("output_text", "error"),
    [
        ("[]", "invalid response"),
        (None, "invalid response"),
        ("", "empty response"),
    ],
)
def test_ai_service_rejects_invalid_json_response_shapes(output_text, error):
    class FakeResponse:
        pass

    class FakeResponses:
        def create(self, **kwargs):
            response = FakeResponse()
            response.output_text = output_text
            return response

    class FakeClient:
        responses = FakeResponses()

    service = EmailAIService()
    service.client = FakeClient()
    with pytest.raises(AIServiceError, match=error):
        service.generate_draft(EmailRequest.model_validate(valid_request))


def test_ai_service_rejects_empty_or_oversized_draft_fields():
    with pytest.raises(ValidationError):
        EmailDraft(subject=" ", body="A body")
    with pytest.raises(ValidationError):
        EmailDraft(subject="A subject", body="x" * 10001)


def test_review_failure_does_not_return_unreviewed_draft():
    class ReviewFailingAI:
        def generate_draft(self, request):
            return SuccessfulAI().generate_draft(request)

        def review_draft(self, request, draft):
            raise AIServiceError("The review could not be completed.")

    app.state.ai_service = ReviewFailingAI()
    response = _generate()
    assert response.status_code == 502
    assert response.json()["detail"] == "The review could not be completed."


def _create_approved_draft(recipient="jordan@example.com", subject="A thoughtful note", body="Hello Jordan.\n"):
    app.state.ai_service = SuccessfulAI()
    generated = _generate()
    assert generated.status_code == 200
    draft_id = generated.json()["id"]
    approval = client.post(
        f"/api/drafts/{draft_id}/approve",
        json={"recipient": recipient, "subject": subject, "body": body},
    )
    assert approval.status_code == 200
    return draft_id


def _connect_gmail():
    app.state.store.save_connection(OWNER, "encrypted-placeholder", [GMAIL_SEND_SCOPE])


def _send(draft_id, key="test-idempotency-key-001", json_body=None):
    return client.post(
        f"/api/drafts/{draft_id}/send",
        headers={"Idempotency-Key": key},
        json=json_body,
    )


def test_send_requires_authentication():
    draft_id = _create_approved_draft()
    client.cookies.clear()
    assert _send(draft_id).status_code == 401


def test_send_rejects_wrong_draft_owner():
    draft_id = _create_approved_draft()
    other_session = "another-session-token-00000000000"
    app.state.store.create_session(
        hashlib.sha256(other_session.encode()).hexdigest(),
        "other@example.com",
        3600,
    )
    client.cookies.set("mailpilot_session", other_session)
    response = _send(draft_id)
    assert response.status_code == 404


def test_send_rejects_modified_content_after_approval():
    draft_id = _create_approved_draft()
    _connect_gmail()
    app.state.store.connection.execute(
        "UPDATE drafts SET body = ? WHERE id = ?", ("Changed without approval", draft_id)
    )
    app.state.store.connection.commit()
    response = _send(draft_id)
    assert response.status_code == 409
    assert "changed after approval" in response.json()["detail"]


def test_send_requires_gmail_connection():
    draft_id = _create_approved_draft()
    response = _send(draft_id)
    assert response.status_code == 409
    assert "Connect Gmail" in response.json()["detail"]


def test_send_rejects_connection_without_current_send_permission():
    draft_id = _create_approved_draft()
    app.state.store.save_connection(OWNER, "encrypted-placeholder", ["openid", "email"])

    class MustNotSend:
        def send(self, *args):
            raise AssertionError("The provider must not be called without Gmail send scope.")

    app.state.gmail_sender = MustNotSend()
    response = _send(draft_id)
    assert response.status_code == 403
    assert "lacks send permission" in response.json()["detail"]


def test_approval_rejects_invalid_recipient():
    response = client.post(
        f"/api/drafts/{_create_approved_draft()}/approve",
        json={"recipient": "Jordan", "subject": "Hello", "body": "Hello"},
    )
    assert response.status_code == 422


def test_send_success_and_duplicate_request_are_idempotent():
    draft_id = _create_approved_draft()
    _connect_gmail()

    class MockSender:
        calls = 0

        def send(self, encrypted, key, recipient, subject, body):
            self.calls += 1
            assert recipient == "jordan@example.com"
            assert subject == "A thoughtful note"
            assert body == "Hello Jordan.\n"
            return "gmail-message-123"

    sender = MockSender()
    app.state.gmail_sender = sender
    first = _send(draft_id, json_body={"subject": "unapproved", "body": "arbitrary content"})
    duplicate = _send(draft_id)
    different_key = _send(draft_id, "different-idempotency-key-002")
    assert first.status_code == duplicate.status_code == 200
    assert first.json()["message_id"] == duplicate.json()["message_id"] == "gmail-message-123"
    assert different_key.status_code == 409
    assert sender.calls == 1


def test_send_provider_error_persists_failed_state():
    draft_id = _create_approved_draft()
    _connect_gmail()

    class FailedSender:
        def send(self, *args):
            raise GmailSendError("Gmail rejected the message.", False)

    app.state.gmail_sender = FailedSender()
    response = _send(draft_id)
    assert response.status_code == 502
    assert app.state.store.get_draft(OWNER, draft_id)["status"] == "failed"
    assert _send(draft_id, "retry-without-reapproval-003").status_code == 409


def test_uncertain_send_outcome_cannot_be_retried():
    draft_id = _create_approved_draft()
    _connect_gmail()

    class UncertainSender:
        def send(self, *args):
            raise GmailSendError("Outcome uncertain.", True)

    app.state.gmail_sender = UncertainSender()
    first = _send(draft_id)
    retry = _send(draft_id, "a-different-idempotency-key-002")
    assert first.status_code == 503
    assert retry.status_code == 409
    assert "uncertain" in retry.json()["detail"]


def test_send_history_is_owner_scoped_and_contains_audit_events():
    draft_id = _create_approved_draft()
    events = client.get("/api/audit").json()
    assert {"draft_created", "draft_approved"} <= {event["event_type"] for event in events}
    assert any(event["draft_id"] == draft_id for event in events)
    other_session = "another-history-session-0000000000"
    app.state.store.create_session(
        hashlib.sha256(other_session.encode()).hexdigest(),
        "other@example.com",
        3600,
    )
    client.cookies.set("mailpilot_session", other_session)
    assert client.get("/api/drafts").json() == []
    assert client.get("/api/audit").json() == []
