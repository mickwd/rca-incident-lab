"""Cache de productos (cache-aside) sobre Redis."""

from __future__ import annotations

import json
import logging
from typing import Protocol

import redis

from .metrics import CACHE_REQUESTS

log = logging.getLogger(__name__)


class ProductCache(Protocol):
    def get(self, product_id: int) -> dict | None: ...

    def set(self, product_id: int, value: dict) -> None: ...

    def invalidate(self, product_id: int) -> None: ...


class InMemoryProductCache:
    def __init__(self) -> None:
        self.data: dict[int, dict] = {}

    def get(self, product_id: int) -> dict | None:
        value = self.data.get(product_id)
        CACHE_REQUESTS.labels(result="hit" if value else "miss").inc()
        return value

    def set(self, product_id: int, value: dict) -> None:
        self.data[product_id] = value

    def invalidate(self, product_id: int) -> None:
        self.data.pop(product_id, None)


class RedisProductCache:
    """Si Redis falla se sigue sin cache (fail open) y se cuenta el error."""

    def __init__(self, client: redis.Redis, ttl_seconds: int = 30) -> None:
        self._client = client
        self._ttl = ttl_seconds

    @classmethod
    def from_url(cls, url: str) -> RedisProductCache:
        client = redis.Redis.from_url(
            url, socket_timeout=0.5, socket_connect_timeout=0.5, decode_responses=True
        )
        return cls(client)

    @staticmethod
    def _key(product_id: int) -> str:
        return f"product:{product_id}"

    def get(self, product_id: int) -> dict | None:
        try:
            raw = self._client.get(self._key(product_id))
        except redis.RedisError as exc:
            log.warning("cache no disponible: %s", exc)
            CACHE_REQUESTS.labels(result="error").inc()
            return None
        CACHE_REQUESTS.labels(result="hit" if raw else "miss").inc()
        return json.loads(raw) if raw else None

    def set(self, product_id: int, value: dict) -> None:
        try:
            self._client.set(self._key(product_id), json.dumps(value), ex=self._ttl)
        except redis.RedisError as exc:
            log.warning("no se pudo escribir en cache: %s", exc)

    def invalidate(self, product_id: int) -> None:
        try:
            self._client.delete(self._key(product_id))
        except redis.RedisError as exc:
            log.warning("no se pudo invalidar cache: %s", exc)
