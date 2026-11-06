"""Dashboard graph response cache.

The console reloads the memory graph on every page open, and the whole query
(nodes plus edges) is a read-only projection of Neo4j. Repeating it for the same
tenant and the same request parameters within a few seconds returns the same
bytes, so the response is cached in Redis for a short TTL.

Scope and limits:

* TTL only. A write does not invalidate the key, so the graph can lag behind a
  fresh ``add``/``delete`` by at most ``MEMOS_GRAPH_CACHE_TTL`` seconds. That is
  acceptable for a dashboard view; callers that need a guaranteed-fresh read
  must not go through this cache.
* Best effort. Any Redis error (down, timeout, wrong type) degrades to a cache
  miss. The cache never fails a request and never holds one for longer than
  ``MEMOS_GRAPH_CACHE_TIMEOUT``.
* Reuses the scheduler's Redis by default (``MEMSCHEDULER_REDIS_*``), because in
  this deployment that is the only Redis the service already knows about.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time

from typing import Any

from memos.log import get_logger


logger = get_logger(__name__)

KEY_PREFIX = "memos:graph:v1"
DEFAULT_TTL_SECONDS = 30
DEFAULT_TIMEOUT_SECONDS = 0.2
# 同一故障在故障期间会被每个请求触发，只按间隔记一条，避免刷满日志。
WARN_INTERVAL_SECONDS = 10.0


def cache_key(params: dict[str, Any]) -> str:
    """Stable key for one graph request.

    The tenant is part of the digest rather than the key so that a Redis dump
    does not spell out cube names; the digest is what separates tenants anyway.
    """
    payload = json.dumps(params, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{KEY_PREFIX}:{digest}"


class GraphCache:
    """Short-TTL cache for the dashboard graph payload."""

    def __init__(self, client: Any | None, ttl_seconds: int) -> None:
        self._client = client
        self._ttl_seconds = ttl_seconds
        self._last_warn_at = 0.0

    @property
    def enabled(self) -> bool:
        return self._client is not None and self._ttl_seconds > 0

    def get(self, key: str) -> bytes | None:
        """Return the cached JSON bytes, or None on a miss or any backend error."""
        if not self.enabled:
            return None
        try:
            raw = self._client.get(key)
        except Exception as exc:
            self._warn("get", exc)
            return None
        if raw is None:
            return None
        if isinstance(raw, str):
            return raw.encode("utf-8")
        if isinstance(raw, bytes | bytearray):
            return bytes(raw)
        self._warn("get", f"unexpected value type {type(raw).__name__}")
        return None

    def set(self, key: str, raw: bytes) -> None:
        """Store the JSON bytes for ``ttl_seconds``. Errors are swallowed."""
        if not self.enabled:
            return
        try:
            self._client.set(key, raw, ex=self._ttl_seconds)
        except Exception as exc:
            self._warn("set", exc)

    def _warn(self, operation: str, detail: Any) -> None:
        now = time.monotonic()
        if now - self._last_warn_at < WARN_INTERVAL_SECONDS:
            return
        self._last_warn_at = now
        logger.warning("graph cache %s failed, serving from Neo4j: %s", operation, detail)


def build_from_env() -> GraphCache:
    """Build the cache from environment variables; disabled on any missing piece."""
    ttl_seconds = _env_int("MEMOS_GRAPH_CACHE_TTL", DEFAULT_TTL_SECONDS)
    if ttl_seconds <= 0:
        return GraphCache(None, 0)

    try:
        import redis
    except ImportError:
        logger.warning("graph cache disabled: redis package is not installed")
        return GraphCache(None, 0)

    timeout_seconds = _env_float("MEMOS_GRAPH_CACHE_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)
    client = redis.Redis(
        host=_env_str(
            "MEMOS_GRAPH_CACHE_REDIS_HOST", _env_str("MEMSCHEDULER_REDIS_HOST", "localhost")
        ),
        port=_env_int("MEMOS_GRAPH_CACHE_REDIS_PORT", _env_int("MEMSCHEDULER_REDIS_PORT", 6379)),
        db=_env_int("MEMOS_GRAPH_CACHE_REDIS_DB", _env_int("MEMSCHEDULER_REDIS_DB", 0)),
        password=_env_str(
            "MEMOS_GRAPH_CACHE_REDIS_PASSWORD", _env_str("MEMSCHEDULER_REDIS_PASSWORD", "")
        )
        or None,
        socket_timeout=timeout_seconds,
        socket_connect_timeout=timeout_seconds,
    )
    return GraphCache(client, ttl_seconds)


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None or value == "" else value


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("invalid %s=%r, using %s", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("invalid %s=%r, using %s", name, raw, default)
        return default


_default_cache: GraphCache | None = None
_default_lock = threading.Lock()


def default_cache() -> GraphCache:
    """Process-wide cache; built on first use so importing this module stays cheap."""
    global _default_cache
    if _default_cache is None:
        with _default_lock:
            if _default_cache is None:
                _default_cache = build_from_env()
    return _default_cache


def reset_default_cache() -> None:
    """Drop the process-wide cache. Tests use this after changing the environment."""
    global _default_cache
    with _default_lock:
        _default_cache = None
