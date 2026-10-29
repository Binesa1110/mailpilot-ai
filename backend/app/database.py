import re
import threading
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine, URL
from sqlalchemy.pool import StaticPool

from dotenv import load_dotenv


def database_url() -> str:
    import os

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    configured = os.getenv("DATABASE_URL")
    if configured:
        return configured
    sqlite_path = os.getenv(
        "MAILPILOT_DB_PATH",
        str(Path(__file__).resolve().parents[1] / "data" / "mailpilot.sqlite3"),
    )
    Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
    return URL.create("sqlite", database=sqlite_path).render_as_string(hide_password=False)


def create_database_engine(url: str, *, pool_size: int = 5, max_overflow: int = 5) -> Engine:
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    options = {"pool_pre_ping": True, "future": True}
    if url in {"sqlite://", "sqlite:///:memory:"}:
        options["poolclass"] = StaticPool
        options["connect_args"] = {"check_same_thread": False}
    elif url.startswith("sqlite:"):
        options["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:
        options.update(pool_size=pool_size, max_overflow=max_overflow, pool_timeout=30)
    engine = create_engine(url, **options)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def configure_sqlite(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA busy_timeout = 30000")
            cursor.close()
    return engine


class _BufferedResult:
    def __init__(self, rows=(), rowcount=-1):
        self._rows = iter(rows)
        self.rowcount = rowcount

    def fetchone(self):
        return next(self._rows, None)

    def fetchall(self):
        return list(self._rows)


class ConnectionFacade:
    """Compatibility surface for the repository while each thread owns a pooled connection."""

    def __init__(self, engine: Engine):
        self.engine = engine
        self.local = threading.local()

    @property
    def _connection(self) -> Connection | None:
        return getattr(self.local, "connection", None)

    def _ensure_connection(self) -> Connection:
        connection = self._connection
        if connection is None or connection.closed:
            connection = self.engine.connect()
            self.local.connection = connection
        return connection

    @property
    def in_transaction(self) -> bool:
        connection = self._connection
        return bool(connection and connection.in_transaction())

    @staticmethod
    def _statement(sql: str, parameters):
        if not isinstance(parameters, dict):
            index = 0

            def replace(_match):
                nonlocal index
                name = f"p{index}"
                index += 1
                return f":{name}"

            statement = re.sub(r"\?", replace, sql)
            parameters = {f"p{i}": value for i, value in enumerate(parameters or ())}
        else:
            statement = sql
        return text(statement), parameters

    def execute(self, sql: str, parameters=()):
        if sql.strip().upper() in {"BEGIN", "BEGIN IMMEDIATE"}:
            connection = self._ensure_connection()
            if not connection.in_transaction():
                connection.begin()
                if (
                    sql.strip().upper() == "BEGIN IMMEDIATE"
                    and connection.dialect.name == "sqlite"
                ):
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
            return _BufferedResult()

        statement, values = self._statement(sql, parameters)
        connection = self._ensure_connection()
        had_transaction = connection.in_transaction()
        if not had_transaction:
            connection.begin()
        result = connection.execute(statement, values)
        if result.returns_rows:
            rows = result.mappings().all()
            rowcount = result.rowcount
            if not had_transaction:
                connection.rollback()
                connection.close()
                self.local.connection = None
            return _BufferedResult(rows, rowcount)
        return _BufferedResult(rowcount=result.rowcount)

    def commit(self):
        connection = self._connection
        if connection:
            if connection.in_transaction():
                connection.commit()
            connection.close()
            self.local.connection = None

    def rollback(self):
        connection = self._connection
        if connection:
            if connection.in_transaction():
                connection.rollback()
            connection.close()
            self.local.connection = None

    def close(self):
        self.rollback()
