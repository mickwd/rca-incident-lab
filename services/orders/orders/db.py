"""Engine y sesiones de SQLAlchemy."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from .config import Settings


def build_engine(settings: Settings) -> Engine:
    """Crea el engine con un QueuePool acotado.

    El pool chico es intencional: hace visible el agotamiento de conexiones
    (escenario connection-pool-exhaustion) y el efecto cascada de db-lock.
    """
    connect_args: dict = {}
    if settings.database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    else:
        # application_name permite separar API y worker en pg_stat_activity
        connect_args["application_name"] = settings.app_name
        connect_args["connect_timeout"] = 5
    return create_engine(
        settings.database_url,
        poolclass=QueuePool,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_pre_ping=True,
        connect_args=connect_args,
    )


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
