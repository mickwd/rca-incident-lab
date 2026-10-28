"""Feature flags guardados en Redis.

Los flags con prefijo ``fault:`` activan los defectos del laboratorio. Se leen
con un cache local de TTL corto para no consultar Redis en cada request. Si
Redis no responde el flag se considera apagado: un flag nunca debe tirar abajo
el servicio.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Protocol

import redis

log = logging.getLogger(__name__)

PREFIX = "fault:"

# Flags que entiende el servicio. labctl usa los mismos nombres.
KNOWN_FLAGS = ("memory-leak", "db-lock", "pool-leak", "cpu-spike")


class FlagStore(Protocol):
    def get(self, name: str) -> str | None: ...

    def set(self, name: str, value: str) -> None: ...

    def delete(self, name: str) -> None: ...

    def all(self) -> dict[str, str]: ...

    def ping(self) -> bool: ...


def is_enabled(store: FlagStore, name: str) -> bool:
    value = store.get(name)
    return value not in (None, "", "0", "off", "false")


def int_value(store: FlagStore, name: str, default: int) -> int:
    value = store.get(name)
    try:
        return int(value) if value else default
    except ValueError:
        return default


class InMemoryFlagStore:
    """Implementación en memoria usada en tests."""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._data: dict[str, str] = dict(initial or {})

    def get(self, name: str) -> str | None:
        return self._data.get(name)

    def set(self, name: str, value: str) -> None:
        self._data[name] = value

    def delete(self, name: str) -> None:
        self._data.pop(name, None)

    def all(self) -> dict[str, str]:
        return dict(self._data)

    def ping(self) -> bool:
        return True


class RedisFlagStore:
    def __init__(self, client: redis.Redis, ttl: float = 1.0) -> None:
        self._client = client
        self._ttl = ttl
        self._cache: dict[str, tuple[float, str | None]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_url(cls, url: str, ttl: float = 1.0) -> RedisFlagStore:
        client = redis.Redis.from_url(
            url, socket_timeout=0.5, socket_connect_timeout=0.5, decode_responses=True
        )
        return cls(client, ttl)

    def get(self, name: str) -> str | None:
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(name)
            if cached and now - cached[0] < self._ttl:
                return cached[1]
        try:
            value = self._client.get(PREFIX + name)
        except redis.RedisError as exc:
            log.warning("no se pudo leer el flag %s: %s", name, exc)
            value = None
        with self._lock:
            self._cache[name] = (now, value)
        return value

    def set(self, name: str, value: str) -> None:
        self._client.set(PREFIX + name, value)
        with self._lock:
            self._cache.pop(name, None)

    def delete(self, name: str) -> None:
        self._client.delete(PREFIX + name)
        with self._lock:
            self._cache.pop(name, None)

    def all(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for key in self._client.scan_iter(match=PREFIX + "*"):
            value = self._client.get(key)
            if value is not None:
                result[key.removeprefix(PREFIX)] = value
        return result

    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except redis.RedisError:
            return False
