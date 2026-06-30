"""Database connection and session management (shared Libingo Postgres)."""

import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from app.config import settings
from app.db.models import Base

logger = logging.getLogger(__name__)

_engine = None
SessionLocal = None


def init_engine() -> None:
    """Initialise the SQLAlchemy engine + session factory (idempotent)."""
    global _engine, SessionLocal
    if _engine is not None:
        return

    if not settings.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")

    engine_kwargs = {
        "poolclass": QueuePool,
        # Pool sized generously to support many concurrent workers (100-500 accounts).
        "pool_size": 20,
        "max_overflow": 40,
        "pool_recycle": 300,
        "pool_timeout": 30,
        "pool_pre_ping": True,
        "echo": settings.APP_DEBUG,
    }

    if settings.DATABASE_URL.startswith("postgresql"):
        engine_kwargs["connect_args"] = {
            "sslmode": settings.DB_SSLMODE,
            "connect_timeout": 20,
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 5,
            "options": "-c statement_timeout=60s",
        }

    _engine = create_engine(settings.DATABASE_URL, **engine_kwargs)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
    logger.info("Database engine initialised")


def init_db() -> None:
    """Create all uo_ tables if they do not exist."""
    init_engine()
    Base.metadata.create_all(bind=_engine)
    logger.info("Database tables ensured")


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a session."""
    init_engine()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Context manager with commit/rollback handling for background workers."""
    init_engine()
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception as rollback_error:  # pragma: no cover
            logger.warning("Rollback failed: %s", str(rollback_error)[:120])
        raise
    finally:
        db.close()
