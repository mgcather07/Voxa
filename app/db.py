"""Database engine and session handling."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base

_settings = get_settings()

engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# Additive column adds for tables that predate a column. create_all() never
# ALTERs an existing table, so new columns on an existing table are applied here.
# Postgres-only (guarded); a fresh DB gets them from create_all and these no-op.
_COLUMN_ADDS = [
    "ALTER TABLE call_quality ADD COLUMN IF NOT EXISTS codec VARCHAR(48)",
    "ALTER TABLE call_quality ADD COLUMN IF NOT EXISTS concealed_secs INTEGER",
    "ALTER TABLE call_quality ADD COLUMN IF NOT EXISTS severely_concealed_secs INTEGER",
]


def init_db() -> None:
    Base.metadata.create_all(engine)
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            for stmt in _COLUMN_ADDS:
                conn.execute(text(stmt))


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
