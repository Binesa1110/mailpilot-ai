import os
import threading
import time
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from backend.app.storage import Store


POSTGRES_TEST_URL = os.getenv("MAILPILOT_TEST_POSTGRES_URL")
ALLOW_POSTGRES_TESTS = os.getenv("MAILPILOT_ALLOW_POSTGRES_TESTS") == "1"
pytestmark = pytest.mark.skipif(
    not POSTGRES_TEST_URL or not ALLOW_POSTGRES_TESTS,
    reason=(
        "Set MAILPILOT_TEST_POSTGRES_URL to a disposable PostgreSQL test database and "
        "MAILPILOT_ALLOW_POSTGRES_TESTS=1 to run PostgreSQL integration tests."
    ),
)


@pytest.fixture
def postgres_store():
    store = Store(POSTGRES_TEST_URL)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with store.engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    yield store
    store.dispose()


def test_postgres_migration_and_repository_round_trip(postgres_store):
    owner_id = f"postgres-test:{uuid.uuid4()}"
    postgres_store.save_user(owner_id, "migration-test@example.invalid")
    user = postgres_store.get_user(owner_id)
    assert user["owner_id"] == owner_id
    assert user["email"] == "migration-test@example.invalid"


def test_postgres_send_lock_orders_disconnect_without_open_transaction(postgres_store):
    owner_id = f"postgres-test:{uuid.uuid4()}"
    disconnect_store = Store(POSTGRES_TEST_URL)
    postgres_store.save_connection(
        owner_id,
        "test-ciphertext",
        ["https://www.googleapis.com/auth/gmail.send"],
    )
    send_lock_acquired = threading.Event()
    release_send = threading.Event()
    disconnect_started = threading.Event()
    disconnect_finished = threading.Event()

    def simulated_send_operation():
        with postgres_store.send_lock(owner_id):
            send_lock_acquired.set()
            release_send.wait(timeout=10)

    def disconnect_operation():
        disconnect_started.set()
        disconnect_store.disconnect_gmail(owner_id)
        disconnect_finished.set()

    sender = threading.Thread(target=simulated_send_operation)
    sender.start()
    assert send_lock_acquired.wait(timeout=10)
    disconnector = threading.Thread(target=disconnect_operation)
    disconnector.start()
    assert disconnect_started.wait(timeout=10)
    time.sleep(0.2)
    assert not disconnect_finished.is_set()

    release_send.set()
    sender.join(timeout=10)
    disconnector.join(timeout=10)
    try:
        assert not sender.is_alive()
        assert not disconnector.is_alive()
        assert disconnect_finished.is_set()
        assert postgres_store.get_connection(owner_id) is None
        settings = postgres_store.get_account_settings(owner_id)
        assert settings["enabled"] is False
        assert settings["mode"] == "approval_required"
    finally:
        disconnect_store.dispose()
