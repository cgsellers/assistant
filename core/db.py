from collections.abc import Generator
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from core.config import get_settings

SQLITE_PREFIX = "sqlite:///"


def _ensure_sqlite_dir(url: str) -> None:
    """SQLite won't create missing parent folders; do it for ./data/ledgerline.db."""
    if url.startswith(SQLITE_PREFIX):
        Path(url.removeprefix(SQLITE_PREFIX)).parent.mkdir(parents=True, exist_ok=True)


def _register_sqlite_pragmas(engine: Engine) -> None:
    """
    SQLite's defaults are wrong for us in three ways, and the settings are
    per-connection rather than per-database, so they are reapplied on connect.
    """

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")      # off by default in SQLite!
        cursor.execute("PRAGMA journal_mode=WAL")     # API can read while worker writes
        cursor.execute("PRAGMA busy_timeout=5000")    # wait 5s on lock instead of erroring
        cursor.close()


def create_db_engine(url: str) -> Engine:
    """
    Build an engine for `url` with our SQLite adjustments applied.

    Exists as a function rather than module-level code so tests can create an
    engine against a throwaway database and still get the same pragmas the
    application runs with -- foreign key enforcement in particular, which is
    off by default and would otherwise make tests pass on constraints that do
    not hold in production.
    """
    is_sqlite = url.startswith("sqlite")
    _ensure_sqlite_dir(url)

    engine = create_engine(
        url,
        # SQLite refuses connections used from a thread other than the one that
        # created them. FastAPI runs plain `def` handlers in a threadpool, so we
        # turn that check off; SQLAlchemy's pool still hands each request its
        # own connection.
        connect_args={"check_same_thread": False} if is_sqlite else {},
    )
    if engine.dialect.name == "sqlite":
        _register_sqlite_pragmas(engine)
    return engine


engine = create_db_engine(get_settings().database_url)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency: one session per request, closed afterwards."""
    with SessionLocal() as session:
        yield session
