"""Startup helpers: migrations and first administrator."""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import User
from app.security import hash_password
from app.services import audit

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]


def run_migrations() -> None:
    """Upgrade the database to the latest Alembic revision.

    On PostgreSQL an advisory lock serialises concurrent cold starts.
    """
    from alembic import command
    from alembic.config import Config

    from app.database import get_engine

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    engine = get_engine()
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(72411)"))
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    log.info("database migrations applied")


def ensure_admin(db: Session, email: str, password: str, name: str = "Administrator") -> User | None:
    """Create the first admin if no users exist yet. Never overwrites an existing account.

    Safe when several serverless instances start at once: on PostgreSQL an advisory lock
    serialises the check-and-insert, and a lost race (unique email) is treated as success.
    """
    try:
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(72412)"))
        if db.scalar(select(func.count(User.id))):
            db.rollback()
            return None
        user = User(email=email.strip().lower(), name=name, password_hash=hash_password(password), role="admin")
        db.add(user)
        db.flush()
        audit.record(db, "user.bootstrapped", "user", user.id, actor_type="system")
        db.commit()
    except IntegrityError:
        db.rollback()
        log.info("bootstrap administrator already created by another instance")
        return None
    log.info("bootstrap administrator created")
    return user


__all__ = ["ensure_admin", "run_migrations", "settings"]
