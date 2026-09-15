from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from core.config import get_settings

settings = get_settings()

SQLITE_PREFIX = "sqlite:///"


def _ensure_sqlite_dir(url: str) -> None:
    """SQLite won't create missing parent folders; do it for ./data/ledgerline.db."""
    if url.startswith(SQLITE_PREFIX):
        Path(url.removeprefix(SQLITE_PREFIX)).parent.mkdir(parents=True, exist_ok=True)


_ensure_sqlite_dir(settings.database_url)

engine = create_engine(
    settings.database_url,
    # SQLite refuses connections used from a thread other than the one that created
    # them. FastAPI runs plain `def` handlers in a threadpool, so we turn that check
    # off; SQLAlchemy's pool still hands each request its own connection.
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)


if engine.dialect.name == "sqlite":

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")      # off by default in SQLite!
        cursor.execute("PRAGMA journal_mode=WAL")     # API can read while worker writes
        cursor.execute("PRAGMA busy_timeout=5000")    # wait 5s on lock instead of erroring
        cursor.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency: one session per request, closed afterwards."""
    with SessionLocal() as session:
        yield session