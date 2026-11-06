"""图谱响应的短 TTL 缓存：命中不再打 Neo4j，任何 Redis 故障都退化成直查。"""

import json

from types import SimpleNamespace

import pytest

from memos.api import graph_cache
from memos.api.handlers import memory_handler
from memos.api.product_models import GetMemoryGraphRequest


NODES = [
    {
        "id": "n1",
        "memory": "Alice likes tea",
        "metadata": {
            "memory_type": "LongTermMemory",
            "status": "activated",
            "created_at": "2026-10-20T10:00:00+00:00",
        },
    }
]


class FakeCache:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.store = {}
        self.gets = []
        self.sets = []

    def get(self, key):
        self.gets.append(key)
        return self.store.get(key)

    def set(self, key, raw):
        self.sets.append((key, raw))
        self.store[key] = raw


class RecordingLogger:
    def __init__(self):
        self.lines = []

    def info(self, message, *args, **kwargs):
        self.lines.append((message, args))

    def warning(self, message, *args, **kwargs):
        self.lines.append((message, args))

    def error(self, message, *args, **kwargs):
        self.lines.append((message, args))

    def debug(self, message, *args, **kwargs):
        self.lines.append((message, args))


def _cube(calls, exported=None):
    def get_all(**kwargs):
        calls.append(kwargs)
        return exported or {"nodes": NODES, "edges": [], "total_nodes": len(NODES)}

    return SimpleNamespace(text_mem=SimpleNamespace(graph_store=None, get_all=get_all))


def test_second_identical_request_is_served_from_cache(monkeypatch):
    cache = FakeCache()
    monkeypatch.setattr(graph_cache, "default_cache", lambda: cache)
    calls = []
    cube = _cube(calls)
    request = GetMemoryGraphRequest(mem_cube_id="cube-a", limit=10)

    first = memory_handler.handle_get_memory_graph(request, cube)
    second = memory_handler.handle_get_memory_graph(request, cube)

    assert len(calls) == 1
    assert len(cache.sets) == 1
    assert first.data == second.data
    assert second.data["nodes"][0]["id"] == "n1"


def test_cache_key_separates_tenants_and_parameters(monkeypatch):
    cache = FakeCache()
    monkeypatch.setattr(graph_cache, "default_cache", lambda: cache)
    calls = []
    cube = _cube(calls)

    memory_handler.handle_get_memory_graph(
        GetMemoryGraphRequest(mem_cube_id="cube-a", limit=10), cube
    )
    memory_handler.handle_get_memory_graph(
        GetMemoryGraphRequest(mem_cube_id="cube-b", limit=10), cube
    )
    memory_handler.handle_get_memory_graph(
        GetMemoryGraphRequest(mem_cube_id="cube-a", limit=20), cube
    )

    assert len(calls) == 3
    assert len(set(cache.gets)) == 3


def test_cached_payload_is_json_and_sized(monkeypatch):
    cache = FakeCache()
    monkeypatch.setattr(graph_cache, "default_cache", lambda: cache)
    memory_handler.handle_get_memory_graph(GetMemoryGraphRequest(mem_cube_id="cube-a"), _cube([]))

    ((key, raw),) = cache.sets
    assert key.startswith(graph_cache.KEY_PREFIX)
    assert json.loads(raw)["cube_id"] == "cube-a"


@pytest.mark.parametrize("cached", [b"{not json", b"[]", b"null"])
def test_unusable_cache_entry_falls_back_to_the_store(monkeypatch, cached):
    cache = FakeCache()
    monkeypatch.setattr(graph_cache, "default_cache", lambda: cache)
    calls = []
    cube = _cube(calls)
    request = GetMemoryGraphRequest(mem_cube_id="cube-a")

    key = graph_cache.cache_key(
        {
            "tenant": "cube-a",
            "types": None,
            "status": request.status,
            "limit": request.limit,
            "label_max": request.label_max,
            "include_edges": request.include_edges,
        }
    )
    cache.store[key] = cached

    response = memory_handler.handle_get_memory_graph(request, cube)

    assert len(calls) == 1
    assert response.data["nodes"][0]["id"] == "n1"


def test_disabled_cache_only_reads_the_store(monkeypatch):
    cache = FakeCache(enabled=False)
    monkeypatch.setattr(graph_cache, "default_cache", lambda: cache)
    calls = []
    cube = _cube(calls)

    memory_handler.handle_get_memory_graph(GetMemoryGraphRequest(mem_cube_id="cube-a"), cube)
    memory_handler.handle_get_memory_graph(GetMemoryGraphRequest(mem_cube_id="cube-a"), cube)

    assert len(calls) == 2


def test_audit_line_records_counts_cache_state_and_phases(monkeypatch):
    cache = FakeCache()
    monkeypatch.setattr(graph_cache, "default_cache", lambda: cache)
    log = RecordingLogger()
    monkeypatch.setattr(memory_handler, "logger", log)

    memory_handler.handle_get_memory_graph(
        GetMemoryGraphRequest(mem_cube_id="example-agent", limit=4000), _cube([])
    )

    message, args = log.lines[0]
    assert message.startswith("graph_audit")
    rendered = message % args
    assert "tenant=example-agent" in rendered
    assert "cache=miss" in rendered
    assert "nodes=1" in rendered
    assert "bytes=" in rendered
    assert "total_ms=" in rendered
    assert "phases_ms=" in rendered


class _BrokenClient:
    def get(self, key):
        raise ConnectionError("redis down")

    def set(self, key, raw, ex=None):
        raise ConnectionError("redis down")


def test_backend_errors_degrade_to_a_miss_without_raising():
    cache = graph_cache.GraphCache(_BrokenClient(), ttl_seconds=30)

    assert cache.enabled is True
    assert cache.get("k") is None
    cache.set("k", b"payload")


def test_disabled_cache_never_touches_the_client():
    cache = graph_cache.GraphCache(_BrokenClient(), ttl_seconds=0)

    assert cache.enabled is False
    assert cache.get("k") is None
    cache.set("k", b"payload")


def test_cache_key_is_stable_and_tenant_scoped():
    params = {"tenant": "cube-a", "limit": 4000, "types": ["Context"], "status": ["activated"]}

    assert graph_cache.cache_key(params) == graph_cache.cache_key(
        dict(reversed(list(params.items())))
    )
    assert graph_cache.cache_key(params) != graph_cache.cache_key({**params, "tenant": "cube-b"})
    assert graph_cache.cache_key(params) != graph_cache.cache_key({**params, "limit": 10})
    # 租户名不进 key 明文。
    assert "cube-a" not in graph_cache.cache_key(params)


def test_env_ttl_zero_disables_and_scheduler_redis_is_reused(monkeypatch):
    monkeypatch.setenv("MEMOS_GRAPH_CACHE_TTL", "0")
    assert graph_cache.build_from_env().enabled is False

    monkeypatch.setenv("MEMOS_GRAPH_CACHE_TTL", "30")
    monkeypatch.setenv("MEMSCHEDULER_REDIS_HOST", "memos-original-redis")
    monkeypatch.setenv("MEMSCHEDULER_REDIS_PORT", "6379")
    monkeypatch.setenv("MEMSCHEDULER_REDIS_DB", "2")
    cache = graph_cache.build_from_env()

    kwargs = cache._client.connection_pool.connection_kwargs
    assert cache.enabled is True
    assert kwargs["host"] == "memos-original-redis"
    assert kwargs["port"] == 6379
    assert kwargs["db"] == 2


@pytest.mark.parametrize("bad", ["not-a-number", ""])
def test_invalid_ttl_falls_back_to_the_default(monkeypatch, bad):
    monkeypatch.setenv("MEMOS_GRAPH_CACHE_TTL", bad)
    monkeypatch.setenv("MEMSCHEDULER_REDIS_HOST", "127.0.0.1")

    cache = graph_cache.build_from_env()

    assert cache.enabled is True
    assert cache._ttl_seconds == graph_cache.DEFAULT_TTL_SECONDS
