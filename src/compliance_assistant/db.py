"""Engine, session and migration helpers.

The database location comes from ``DATABASE_URL`` so the same code runs on
SQLite today and PostgreSQL later without changes.
"""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SQLITE_PATH = PROJECT_ROOT / "data" / "db" / "register.sqlite"


def default_database_url() -> str:
    return os.environ.get("DATABASE_URL") or f"sqlite:///{DEFAULT_SQLITE_PATH}"


def get_engine(url: str | None = None) -> Engine:
    url = url or default_database_url()
    if url.startswith("sqlite:///") and ":memory:" not in url:
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, future=True)

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")  # SQLite ignores FKs by default
            cur.execute("PRAGMA journal_mode=WAL")  # readers never block the writer
            cur.close()

    return engine


def get_session(engine: Engine | None = None) -> Session:
    return sessionmaker(bind=engine or get_engine(), expire_on_commit=False)()


def alembic_config(url: str | None = None) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", (url or default_database_url()).replace("%", "%%"))
    return cfg


def migrate(url: str | None = None, revision: str = "head") -> None:
    """Bring the database schema up to date."""
    command.upgrade(alembic_config(url), revision)
