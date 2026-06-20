"""Engine/session factory. Reads ``DATABASE_URL`` at call time (so tests and the
dashboard can point at different databases without import-time binding)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


class DatabaseNotConfigured(RuntimeError):
    """Raised when persistence is requested but DATABASE_URL is unset."""


def database_url() -> str:
    load_dotenv()
    url = os.getenv("DATABASE_URL")
    if not url:
        raise DatabaseNotConfigured(
            "DATABASE_URL is not set. Add it to .env (see .env.example) or start the "
            "dev database with `docker compose up -d`."
        )
    return url


def get_engine(url: str | None = None) -> Engine:
    return create_engine(url or database_url(), future=True, pool_pre_ping=True)


def get_sessionmaker(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False, future=True)


@contextmanager
def session_scope(url: str | None = None) -> Iterator[Session]:
    """Transactional session: commit on success, rollback on error."""
    factory = get_sessionmaker(url)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
