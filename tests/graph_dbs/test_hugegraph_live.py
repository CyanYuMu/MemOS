from __future__ import annotations

import os
import time
import uuid

import pytest

from memos.configs.graph_db import HugeGraphDBConfig
from memos.graph_dbs.hugegraph import HugeGraphGraphDB


pytestmark = pytest.mark.skipif(
    os.getenv("MEMOS_RUN_HUGEGRAPH_LIVE") != "1",
    reason="set MEMOS_RUN_HUGEGRAPH_LIVE=1 to run against HugeGraph and Qdrant",
)


def _backend(tenant: str) -> HugeGraphGraphDB:
    return HugeGraphGraphDB(
        HugeGraphDBConfig(
            ip=os.getenv("HUGEGRAPH_HOST", "127.0.0.1"),
            port=int(os.getenv("HUGEGRAPH_PORT", "18080")),
            graph=os.getenv("HUGEGRAPH_GRAPH", "hugegraph"),
            # HStore deploys address graphs through a graphspace; a bare
            # graph name cannot be resolved there.
            graphspace=os.getenv("HUGEGRAPH_GRAPHSPACE") or None,
            user=os.getenv("HUGEGRAPH_USER") or None,
            password=os.getenv("HUGEGRAPH_PASSWORD") or None,
            user_name=tenant,
            vec_config={
                "backend": "qdrant",
                "config": {
                    "host": os.getenv("QDRANT_HOST", "127.0.0.1"),
                    "port": int(os.getenv("QDRANT_PORT", "6333")),
                    "collection_name": "memos_hugegraph_live",
                    "vector_dimension": 3,
                    "distance_metric": "cosine",
                },
            },
        )
    )


def test_live_crud_recall_edges_cleanup_and_tenant_isolation() -> None:
    tenant_a = f"live-a-{uuid.uuid4()}"
    tenant_b = f"live-b-{uuid.uuid4()}"
    node_a = str(uuid.uuid4())
    node_b = str(uuid.uuid4())
    backend_a = _backend(tenant_a)
    backend_b = _backend(tenant_b)

    try:
        backend_a.add_node(
            node_a,
            "Alice prefers concise answers.",
            {
                "embedding": [1.0, 0.0, 0.0],
                "memory_type": "LongTermMemory",
                "status": "activated",
                "mem_cube_id": tenant_a,
            },
        )
        backend_a.add_node(
            node_b,
            "Use a short response.",
            {
                "embedding": [0.9, 0.1, 0.0],
                "memory_type": "WorkingMemory",
                "status": "activated",
                "mem_cube_id": tenant_a,
            },
        )
        backend_a.add_edge(node_a, node_b, "SUPPORTS")

        assert backend_a.get_node(node_a, include_embedding=True)["memory"] == (
            "Alice prefers concise answers."
        )
        grouped_counts = backend_a.get_grouped_counts(["memory_type"])
        assert {row["memory_type"]: row["count"] for row in grouped_counts} == {
            "LongTermMemory": 1,
            "WorkingMemory": 1,
        }
        assert backend_a.get_neighbors(node_a, "SUPPORTS") == [node_b]
        hits = backend_a.search_by_embedding(
            [1.0, 0.0, 0.0],
            top_k=5,
            scope="LongTermMemory",
            status="activated",
            return_fields=["memory", "memory_type", "status"],
        )
        assert hits[0]["id"] == node_a
        assert hits[0]["memory"] == "Alice prefers concise answers."
        fulltext_hits = backend_a.search_by_fulltext(
            ["concise"],
            top_k=5,
            scope="LongTermMemory",
            status="activated",
        )
        assert fulltext_hits == [{"id": node_a, "score": 1.0}]

        meta_ids = backend_a.get_by_metadata(
            [{"field": "memory_type", "op": "=", "value": "LongTermMemory"}],
            user_name=tenant_a,
            status="activated",
        )
        assert meta_ids == [node_a]

        node_b2 = str(uuid.uuid4())
        backend_b.add_node(
            node_b2,
            "Shared policy text.",
            {
                "embedding": [0.0, 1.0, 0.0],
                "memory_type": "WorkingMemory",
                "status": "activated",
                "mem_cube_id": tenant_b,
            },
        )
        # knowledgebase_ids act as additional user_names: invisible without,
        # recallable with (G4/G5 semantics, live).
        assert backend_a.search_by_fulltext(["policy"], user_name=tenant_a) == []
        kb_hits = backend_a.search_by_fulltext(
            ["policy"], user_name=tenant_a, knowledgebase_ids=[tenant_b]
        )
        assert [h["id"] for h in kb_hits] == [node_b2]
        kb_meta_ids = backend_a.get_by_metadata(
            [{"field": "memory_type", "op": "=", "value": "WorkingMemory"}],
            user_name=tenant_a,
            knowledgebase_ids=[tenant_b],
        )
        assert sorted(kb_meta_ids) == sorted([node_b, node_b2])

        assert backend_b.get_node(node_a) is None
        # Tenant isolation: backend_b sees its own node but never tenant_a's.
        visible = backend_b.search_by_embedding([1.0, 0.0, 0.0], top_k=5)
        assert [h["id"] for h in visible] == [node_b2]
        backend_b.delete_node(node_a)
        assert backend_a.get_node(node_a) is not None
    finally:
        backend_b.delete_node(node_b2)
        backend_a.delete_node(node_b)
        backend_a.delete_node(node_a)

    # HStore applies deletes asynchronously: live probes have caught the
    # physical delete landing >20s after the 2xx response (and the same
    # per-replica disagreement is documented for schema caches). Poll long
    # enough before declaring the cleanup failed.
    # The read path must trust the authoritative REST answer over the
    # lagging gremlin replica (fixed in _get_vertex_by_node_id), so the
    # deleted node disappears as soon as the DELETE lands.
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline and backend_a.get_node(node_a) is not None:
        time.sleep(1)
    assert backend_a.get_node(node_a) is None
    assert backend_a.search_by_embedding([1.0, 0.0, 0.0], top_k=5) == []


def test_live_subgraph_edge_parsing_and_cube_name_hydration() -> None:
    """The path traversal must parse edge elementMaps (OUT/IN endpoints,
    list-valued relation_type) against the real server, and cube_name must
    drive the graph-side hydration of return_fields, not just the vector
    payload filter."""
    tenant = f"live-c-{uuid.uuid4()}"
    center_id = str(uuid.uuid4())
    neighbor_id = str(uuid.uuid4())
    backend = _backend(tenant)

    try:
        for node_id, body in (
            (center_id, "Center memory."),
            (neighbor_id, "Neighbor memory."),
        ):
            backend.add_node(
                node_id,
                body,
                {
                    "embedding": [1.0, 0.0, 0.0],
                    "memory_type": "LongTermMemory",
                    "status": "activated",
                    "mem_cube_id": tenant,
                },
            )
        backend.add_edge(center_id, neighbor_id, "PARENT")

        subgraph = backend.get_subgraph(
            center_id=center_id, depth=1, center_status="activated", user_name=tenant
        )
        assert subgraph["core_node"]["id"] == center_id
        assert [n["id"] for n in subgraph["neighbors"]] == [neighbor_id]
        assert subgraph["edges"] == [{"source": center_id, "target": neighbor_id, "type": "PARENT"}]

        hits = backend.search_by_embedding(
            [1.0, 0.0, 0.0], top_k=5, return_fields=["memory"], cube_name=tenant
        )
        by_id = {h["id"]: h for h in hits}
        assert by_id[center_id]["memory"] == "Center memory."
        assert by_id[neighbor_id]["memory"] == "Neighbor memory."
    finally:
        backend.delete_node(neighbor_id)
        backend.delete_node(center_id)

    # Same REST-authoritative read as the CRUD test above.
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline and backend.get_node(center_id) is not None:
        time.sleep(1)
    assert backend.get_node(center_id) is None
