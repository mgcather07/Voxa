"""Database engine and session handling."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base  # noqa: F401 - imported so metadata is registered

_settings = get_settings()

engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# Schema is managed by Alembic (app/migrations). New model changes get a
# revision via:  python -m app.makemigration "describe the change"
_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
BASELINE_REV = "0001"
_MIGRATE_LOCK_KEY = 91701  # advisory lock: one worker migrates, others wait


def alembic_config(url: str | None = None) -> AlembicConfig:
    """Programmatic Alembic config — no alembic.ini to ship or drift."""
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url or _settings.database_url)
    return cfg


def init_db() -> None:
    """Bring the schema to the current migration head at startup.

    Fresh database → every migration runs from the baseline up.
    Pre-Alembic install (tables exist, no alembic_version) → adopt it by
    stamping the baseline, then upgrade normally. A Postgres advisory lock
    serialises workers so only one migrates; the rest wait, then no-op.
    """
    lock_conn = engine.connect() if engine.dialect.name == "postgresql" else None
    if lock_conn is not None:
        lock_conn.exec_driver_sql(
            "SELECT pg_advisory_lock(%(k)s)", {"k": _MIGRATE_LOCK_KEY}
        )
    try:
        insp = inspect(engine)
        if insp.has_table("phones") and not insp.has_table("alembic_version"):
            command.stamp(alembic_config(), BASELINE_REV)
        command.upgrade(alembic_config(), "head")
    finally:
        if lock_conn is not None:
            try:
                lock_conn.exec_driver_sql(
                    "SELECT pg_advisory_unlock(%(k)s)", {"k": _MIGRATE_LOCK_KEY}
                )
            finally:
                lock_conn.close()


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
