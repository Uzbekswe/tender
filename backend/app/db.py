"""Database engine and sessions (SQLModel / SQLAlchemy 2, sync).

The engine is created lazily on first use, not at import time, so tests can
point DATA_DIR / DATABASE_URL somewhere else and call `reset_engine()`.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine, make_url
from sqlmodel import Session, SQLModel, create_engine

from app.settings import get_settings

_engine: Engine | None = None
_engine_lock = threading.Lock()


def _ensure_sqlite_dir(url: str) -> None:
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite" and parsed.database and parsed.database != ":memory:":
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)


def _create_engine(url: str) -> Engine:
    is_sqlite = make_url(url).get_backend_name() == "sqlite"
    _ensure_sqlite_dir(url)
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False} if is_sqlite else {},
    )
    if is_sqlite:
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record) -> None:  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            # The background runner and request handlers write to the same file.
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()
    return engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = _create_engine(get_settings().db_url)
    return _engine


def reset_engine() -> None:
    """Dispose the engine so the next call re-reads settings. For tests."""
    global _engine
    with _engine_lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None


def init_db() -> None:
    """Create all tables that do not exist yet. Idempotent."""
    from app import models  # noqa: F401  (registers the tables on SQLModel.metadata)

    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    _add_missing_columns(engine)


def _add_missing_columns(engine) -> None:
    """MVP migration: add columns that are in the models but not yet in an existing SQLite table.

    Plain additions only: a column with a plain default, or a nullable column without one (NULL for
    every existing row, e.g. Project.owner_id). Anything else needs Alembic (production path).
    """
    if engine.dialect.name != "sqlite":
        return
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                ddl = column.type.compile(dialect=engine.dialect)
                if column.default is None:
                    if not column.nullable:
                        continue
                    clause = ""
                else:
                    default = column.default.arg
                    if callable(default) or not isinstance(default, (str, int, float, bool)):
                        continue
                    literal = f"'{default}'" if isinstance(default, str) else str(int(default) if isinstance(default, bool) else default)
                    clause = f" DEFAULT {literal}"
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl}{clause}'))
                if column.index:
                    conn.execute(text(f'CREATE INDEX IF NOT EXISTS "ix_{table.name}_{column.name}" ON "{table.name}" ("{column.name}")'))


def get_session() -> Iterator[Session]:
    """FastAPI dependency. Routes call session.commit() themselves."""
    with Session(get_engine(), expire_on_commit=False) as session:
        yield session


@contextmanager
def session_scope() -> Iterator[Session]:
    """Session for background code: commits on success, rolls back on error."""
    session = Session(get_engine(), expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
