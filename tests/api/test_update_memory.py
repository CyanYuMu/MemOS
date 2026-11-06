"""T2.1 update_memory behavior tests."""

from types import SimpleNamespace

from memos.api.handlers.cloud_handler import handle_update_memory
from memos.api.product_models import UpdateMemoryRequest


class FakeGraph:
    def __init__(self, node):
        self.node = node
        self.add_node_calls = []
        self.update_node_calls = []

    def get_user_names_by_memory_ids(self, ids):
        return {ids[0]: "tenant-1"}

    def get_node(self, memory_id, include_embedding=False, **kwargs):
        return self.node

    def add_node(self, node_id, memory, metadata, user_name=None):
        self.add_node_calls.append(
            {"id": node_id, "memory": memory, "metadata": dict(metadata), "user_name": user_name}
        )

    def update_node(self, node_id, fields, user_name=None):
        self.update_node_calls.append(
            {"id": node_id, "fields": dict(fields), "user_name": user_name}
        )


class FakeEmbedder:
    def embed(self, texts):
        return [[0.5, 0.25] for _ in texts]


class FakeTextMem:
    def __init__(self, graph, embedder=None):
        self.graph_store = graph
        self.embedder = embedder


def _cube(graph, embedder=None):
    return SimpleNamespace(text_mem=FakeTextMem(graph, embedder), pref_mem=None)


def _node(status="activated", memory="old content", **metadata):
    base = {
        "memory_type": "LongTermMemory",
        "status": status,
        "key": "old title",
        "version": 1,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "user_id": "tenant-1",
    }
    base.update(metadata)
    return {"id": "m-1", "memory": memory, "metadata": base}


def test_content_update_reembeds_and_writes_graph():
    graph = FakeGraph(_node())
    resp = handle_update_memory(
        UpdateMemoryRequest(
            memory_id="m-1", content="new content", title="new title", mem_cube_id="tenant-1"
        ),
        _cube(graph, FakeEmbedder()),
    )

    assert resp.data["success"] is True
    assert len(graph.add_node_calls) == 1
    call = graph.add_node_calls[0]
    assert call["memory"] == "new content"
    assert call["metadata"]["embedding"] == [0.5, 0.25]
    assert call["metadata"]["key"] == "new title"
    assert call["metadata"]["version"] == 2
    assert call["metadata"]["history"][0]["memory"] == "old content"
    assert call["user_name"] == "tenant-1"


def test_metadata_only_update_merges_payload_without_rewriting_content():
    graph = FakeGraph(_node())
    resp = handle_update_memory(
        UpdateMemoryRequest(memory_id="m-1", status="archived", mem_cube_id="tenant-1"),
        _cube(graph, FakeEmbedder()),
    )

    assert resp.data["success"] is True
    assert graph.add_node_calls == []
    assert len(graph.update_node_calls) == 1
    fields = graph.update_node_calls[0]["fields"]
    assert fields["status"] == "archived"
    assert "memory" not in fields


def test_frozen_memory_rejects_every_update():
    graph = FakeGraph(_node(status="frozen"))
    resp = handle_update_memory(
        UpdateMemoryRequest(memory_id="m-1", content="evil", mem_cube_id="tenant-1"),
        _cube(graph, FakeEmbedder()),
    )

    assert resp.code == 409
    assert "frozen" in resp.message
    assert graph.add_node_calls == []
    assert graph.update_node_calls == []


def test_expired_memory_is_read_only():
    graph = FakeGraph(_node(status="expired"))
    resp = handle_update_memory(
        UpdateMemoryRequest(memory_id="m-1", title="x", mem_cube_id="tenant-1"),
        _cube(graph, FakeEmbedder()),
    )

    assert resp.code == 409
    assert "expired" in resp.message


def test_missing_memory_returns_not_found():
    class EmptyGraph(FakeGraph):
        def get_node(self, memory_id, include_embedding=False, **kwargs):
            return None

    resp = handle_update_memory(
        UpdateMemoryRequest(memory_id="missing", content="x", mem_cube_id="tenant-1"),
        _cube(EmptyGraph(_node())),
    )

    assert resp.code == 404
    assert resp.data["success"] is False


def test_wrong_owner_is_rejected():
    graph = FakeGraph(_node(user_id="someone-else"))
    resp = handle_update_memory(
        UpdateMemoryRequest(memory_id="m-1", content="x", user_id="other-user"),
        _cube(graph, FakeEmbedder()),
    )

    assert resp.code == 403
    assert graph.add_node_calls == []


def test_archived_can_be_reactivated():
    graph = FakeGraph(_node(status="archived"))
    resp = handle_update_memory(
        UpdateMemoryRequest(memory_id="m-1", status="activated", mem_cube_id="tenant-1"),
        _cube(graph, FakeEmbedder()),
    )

    assert resp.data["success"] is True
    assert graph.update_node_calls[0]["fields"]["status"] == "activated"


def test_illegal_status_transition_is_rejected():
    graph = FakeGraph(_node(status="resolving"))
    resp = handle_update_memory(
        UpdateMemoryRequest(memory_id="m-1", status="activated", mem_cube_id="tenant-1"),
        _cube(graph, FakeEmbedder()),
    )

    assert resp.code == 409
    assert "transition" in resp.message


def test_update_requires_a_change():
    try:
        UpdateMemoryRequest(memory_id="m-1")
    except ValueError as e:
        assert "required" in str(e)
    else:  # pragma: no cover
        raise AssertionError("expected validation error")


def test_empty_content_is_rejected():
    try:
        UpdateMemoryRequest(memory_id="m-1", content="   ")
    except ValueError as e:
        assert "empty" in str(e)
    else:  # pragma: no cover
        raise AssertionError("expected validation error")


class ScopedGraph(FakeGraph):
    """Only the node's real tenant can see it (like Neo4j's get_node filter)."""

    def get_node(self, memory_id, include_embedding=False, **kwargs):
        if kwargs.get("user_name") != "tenant-1":
            return None
        return self.node


class TenantlessGraph(FakeGraph):
    def get_user_names_by_memory_ids(self, ids):
        return {ids[0]: None}


def test_update_never_falls_back_to_a_global_lookup():
    graph = ScopedGraph(_node())
    resp = handle_update_memory(
        UpdateMemoryRequest(memory_id="m-1", content="x", mem_cube_id="tenant-2", user_id="u1"),
        _cube(graph, FakeEmbedder()),
    )

    assert resp.code == 404
    assert graph.add_node_calls == []
    assert graph.update_node_calls == []


def test_update_without_resolvable_tenant_is_rejected():
    graph = TenantlessGraph(_node())
    resp = handle_update_memory(
        UpdateMemoryRequest(memory_id="m-1", content="x"), _cube(graph, FakeEmbedder())
    )

    assert resp.code == 400
    assert "mem_cube_id" in resp.message
    assert graph.add_node_calls == []


def test_reserved_metadata_cannot_rewrite_content_or_tenant():
    graph = FakeGraph(_node())
    resp = handle_update_memory(
        UpdateMemoryRequest(
            memory_id="m-1",
            metadata={"memory": "evil", "user_name": "other"},
            mem_cube_id="tenant-1",
        ),
        _cube(graph, FakeEmbedder()),
    )

    assert resp.code == 400
    assert "reserved" in resp.message
    assert graph.add_node_calls == []
    assert graph.update_node_calls == []


def test_embedding_failure_does_not_write_a_stale_graph_node():
    class BrokenEmbedder:
        def embed(self, texts):
            raise RuntimeError("embedding down")

    graph = FakeGraph(_node())
    resp = handle_update_memory(
        UpdateMemoryRequest(memory_id="m-1", content="new content", mem_cube_id="tenant-1"),
        _cube(graph, BrokenEmbedder()),
    )

    assert resp.code == 500
    assert graph.add_node_calls == []
    assert graph.update_node_calls == []


def test_content_update_without_an_embedder_fails_closed():
    graph = FakeGraph(_node())
    resp = handle_update_memory(
        UpdateMemoryRequest(memory_id="m-1", content="new content", mem_cube_id="tenant-1"),
        _cube(graph, None),
    )

    assert resp.code == 500
    assert "embedder" in resp.message
    assert graph.add_node_calls == []
    assert graph.update_node_calls == []


class FakeVecDB:
    def __init__(self, payload=None):
        self.payload = payload
        self.upserts = []
        self.updates = []

    def get_by_id(self, point_id):
        if self.payload is None:
            return None
        return SimpleNamespace(id=point_id, payload=dict(self.payload))

    def upsert(self, items):
        self.upserts.append(items[0])
        self.payload = dict(items[0].payload)

    def update(self, point_id, data):
        self.updates.append((point_id, data))
        self.payload = dict(data["payload"])


def _graph_with_vec(vec_db):
    from memos.graph_dbs.neo4j import Neo4jGraphDB

    graph = object.__new__(Neo4jGraphDB)
    graph.vec_db = vec_db
    return graph


def test_vector_payload_merge_keeps_tenant_and_status_filters():
    vec = FakeVecDB(
        payload={
            "user_name": "tenant-1",
            "status": "activated",
            "memory_type": "LongTermMemory",
            "vector_sync": "success",
            "memory": "old",
        }
    )
    graph = _graph_with_vec(vec)

    graph._sync_vector_payload("m-1", {"status": "archived", "key": "new title"})

    assert vec.payload["user_name"] == "tenant-1"
    assert vec.payload["memory_type"] == "LongTermMemory"
    assert vec.payload["vector_sync"] == "success"
    assert vec.payload["status"] == "archived"
    assert vec.payload["key"] == "new title"


def test_vector_upsert_after_reembed_keeps_existing_payload():
    vec = FakeVecDB(
        payload={
            "user_name": "tenant-1",
            "status": "activated",
            "memory_type": "LongTermMemory",
            "vector_sync": "success",
        }
    )
    graph = _graph_with_vec(vec)

    graph._sync_vector_payload(
        "11111111-1111-4111-8111-111111111111", {"memory": "new", "embedding": [0.1, 0.2]}
    )

    assert len(vec.upserts) == 1
    payload = vec.upserts[0].payload
    assert payload["user_name"] == "tenant-1"
    assert payload["memory"] == "new"
    assert payload["vector_sync"] == "success"
    assert vec.upserts[0].vector == [0.1, 0.2]


def test_update_requires_a_tenant_or_subject():
    graph = FakeGraph(_node())
    resp = handle_update_memory(
        UpdateMemoryRequest(memory_id="m-1", content="x"), _cube(graph, FakeEmbedder())
    )

    assert resp.code == 400
    assert "mem_cube_id or user_id" in resp.message
    assert graph.add_node_calls == []


def test_agent_and_group_memories_cannot_be_updated_as_user_memory():
    for owner_type in ("agent", "group"):
        graph = FakeGraph(_node(owner_type=owner_type, owner_id="a1"))
        resp = handle_update_memory(
            UpdateMemoryRequest(
                memory_id="m-1", content="x", user_id="tenant-1", mem_cube_id="tenant-1"
            ),
            _cube(graph, FakeEmbedder()),
        )

        assert resp.code == 403
        assert graph.add_node_calls == []
