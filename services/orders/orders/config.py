"""Configuración del servicio leída desde variables de entorno."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def build_database_url() -> str:
    """Arma la URL de conexión.

    DATABASE_URL tiene prioridad. Si no está definida se arma con DB_HOST,
    DB_PORT, DB_USER, DB_PASSWORD y DB_NAME. El escenario dns-broken pisa
    DB_HOST con un nombre que no existe en la red de Docker.
    """
    explicit = os.getenv("DATABASE_URL")
    if explicit:
        return explicit
    user = os.getenv("DB_USER", "orders")
    password = os.getenv("DB_PASSWORD", "orders")
    host = os.getenv("DB_HOST", "postgres")
    port = os.getenv("DB_PORT", "5432")
    name = os.getenv("DB_NAME", "orders")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{name}"


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_pool_timeout: float = 5.0
    uploads_dir: str = "/data/uploads"
    admin_token: str = "change-me"  # noqa: S105 - valor de laboratorio, se pisa con ADMIN_TOKEN
    flag_cache_ttl: float = 1.0
    app_name: str = "orders-api"


def get_settings() -> Settings:
    return Settings(
        database_url=build_database_url(),
        redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
        db_pool_size=_env_int("DB_POOL_SIZE", 5),
        db_max_overflow=_env_int("DB_MAX_OVERFLOW", 5),
        db_pool_timeout=_env_float("DB_POOL_TIMEOUT", 5.0),
        uploads_dir=os.getenv("UPLOADS_DIR", "/data/uploads"),
        admin_token=os.getenv("ADMIN_TOKEN", "change-me"),
        flag_cache_ttl=_env_float("FLAG_CACHE_TTL", 1.0),
        app_name=os.getenv("APP_NAME", "orders-api"),
    )
