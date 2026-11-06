from types import SimpleNamespace

import pytest

from pydantic import ValidationError

from memos.api import graph_cache
from memos.api.handlers import memory_handler
from memos.api.product_models import GetEventMemoriesRequest, GetMemoryGraphRequest
from memos.memories.textual.item import TreeNodeTextualMemoryMetadata


class _DisabledCache:
    enabled = False

    def get(self, key):
        return None

    def set(self, key, raw):
        return None


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    """Graph tests here assert the store call; caching is covered in test_memory_graph_cache."""
    monkeypatch.setattr(graph_cache, "default_cache", lambda: _DisabledCache())


def _cube(calls, exported):
    def get_all(**kwargs):
        calls.append(kwargs)
        return exported

    return SimpleNamespace(text_mem=SimpleNamespace(graph_store=None, get_all=get_all))


NODES = [
    {
        "id": "n1",
        "memory": "Alice likes tea",
        "metadata": {
            "memory_type": "LongTermMemory",
            "status": "activated",
            "created_at": "2026-10-20T10:00:00+00:00",
            "updated_at": "2026-10-20T10:00:00+00:00",
            "session_id": "s1",
            "user_id": "u1",
            "source_memory_ids": ["src-1"],
        },
    },
    {
        "id": "n2",
        "memory": "Alice met Bob",
        "metadata": {
            "memory_type": "Context",
            "status": "activated",
            "created_at": "2026-10-21T10:00:00+00:00",
            "session_id": "s1",
            "user_id": "agent-1",
            "merged_from": ["n1"],
        },
    },
    {
        "id": "n3",
        "memory": "a very long memory text",
        "metadata": {
            "memory_type": "InsightMemory",
            "status": "activated",
            "created_at": "2026-10-22T10:00:00+00:00",
            "user_id": "u1",
        },
    },
]

EDGES = [
    {"source": "n1", "target": "n2", "type": "PARENT"},
    {"source": "n2", "target": "n3", "type": "MERGED_TO"},
    {"source": "n1", "target": "n3", "type": "RELATE"},
    {"source": "n1", "target": "outside", "type": "PARENT"},
]


def test_graph_projects_nodes_edges_and_truncates_labels():
    calls = []
    request = GetMemoryGraphRequest(mem_cube_id="cube-a", limit=10, label_max=5)

    response = memory_handler.handle_get_memory_graph(
        request,
        _cube(calls, {"nodes": NODES, "edges": EDGES, "total_nodes": 5, "total_edges": 4}),
    )

    assert len(calls) == 1
    call = calls[0]
    assert call["user_name"] == "cube-a"
    assert call["page"] == 1
    assert call["page_size"] == 10
    assert call["memory_type"] is None
    assert call["status"] == ["activated"]
    # 白名单投影只取视图要的字段；sources 不在其中。
    assert call["include_properties"] == list(memory_handler._GRAPH_NODE_PROPERTIES)
    assert "sources" not in call["include_properties"]
    assert "project" in call["timing"]
    data = response.data
    assert data["cube_id"] == "cube-a"
    assert [node["id"] for node in data["nodes"]] == ["n1", "n2", "n3"]
    long_node = data["nodes"][2]
    assert long_node["memory"] == "a ver…"
    assert long_node["memoryTruncated"] is True
    assert data["nodes"][0]["sourceMemoryIds"] == ["src-1"]
    assert data["nodes"][1]["sourceMemoryIds"] == ["n1"]
    # Only known endpoints and PARENT/MERGED_TO survive.
    assert data["edges"] == [
        {"source": "n1", "target": "n2", "type": "PARENT"},
        {"source": "n2", "target": "n3", "type": "MERGED_TO"},
    ]
    assert data["total_nodes"] == 5
    assert data["truncated"] is True
    assert data["stats"]["types"] == [
        {"name": "Context", "count": 1},
        {"name": "InsightMemory", "count": 1},
        {"name": "LongTermMemory", "count": 1},
    ]


def test_graph_can_skip_edges_and_passes_filters():
    calls = []
    request = GetMemoryGraphRequest(
        user_id="cube-a",
        memory_types=["Context"],
        status=["activated", "archived"],
        include_edges=False,
    )

    response = memory_handler.handle_get_memory_graph(
        request,
        _cube(calls, {"nodes": NODES[:1], "edges": EDGES, "total_nodes": 1}),
    )

    assert calls[0]["user_name"] == "cube-a"
    assert calls[0]["memory_type"] == ["Context"]
    assert calls[0]["status"] == ["activated", "archived"]
    assert response.data["edges"] == []
    assert response.data["truncated"] is False


def test_event_memories_paginate_and_filter_by_text():
    calls = []
    request = GetEventMemoriesRequest(
        mem_cube_id="cube-a", search_query="  Alice  ", page=2, page_size=10
    )

    response = memory_handler.handle_get_event_memories(
        request,
        _cube(calls, {"nodes": NODES[:2], "total_nodes": 42}),
    )

    assert calls == [
        {
            "user_name": "cube-a",
            "page": 2,
            "page_size": 10,
            "filter": {"memory": {"like": "Alice"}},
            "memory_type": ["EventMemory", "Context", "InsightMemory", "DreamDiary"],
            "exclude_properties": ["sources"],
        }
    ]
    data = response.data
    assert data["total"] == 42
    assert data["page"] == 2
    assert data["page_size"] == 10
    assert [memory["id"] for memory in data["memories"]] == ["n1", "n2"]
    assert data["memories"][0]["memory"] == "Alice likes tea"
    assert data["memories"][0]["memoryTruncated"] is False


def test_event_memories_without_query_and_with_all_types():
    calls = []
    request = GetEventMemoriesRequest(user_id="cube-a", memory_types=[], status=[])

    memory_handler.handle_get_event_memories(
        request,
        _cube(calls, {"nodes": [], "total_nodes": 0}),
    )

    assert calls[0]["filter"] is None
    assert calls[0]["memory_type"] is None
    assert "status" not in calls[0]


def test_dream_metadata_can_be_read_as_a_memory_body():
    metadata = TreeNodeTextualMemoryMetadata(
        memory_type="InsightMemory", status="completed", source="dream"
    )

    assert metadata.source == "dream"


def test_requests_require_a_tenant():
    with pytest.raises(ValidationError):
        GetMemoryGraphRequest()
    with pytest.raises(ValidationError):
        GetEventMemoriesRequest()

    assert GetMemoryGraphRequest(user_id="cube-a").mem_cube_id == "cube-a"
    assert GetEventMemoriesRequest(user_id="cube-a").mem_cube_id == "cube-a"
