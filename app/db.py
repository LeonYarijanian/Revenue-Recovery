"""Engine and sessions for the single SQLite file."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_session_factory: sessionmaker | None = None


def init_engine(url: str) -> Engine:
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    connect_args = {"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    _engine = engine
    _session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    return engine


def get_engine() -> Engine:
    if _engine is None:
        from app.config import get_settings

        init_engine(get_settings().DATABASE_URL)
    return _engine


def new_session() -> Session:
    get_engine()
    return _session_factory()


@contextmanager
def session_scope() -> Iterator[Session]:
    session = new_session()
    try:
        yield session
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as session:
        yield session
