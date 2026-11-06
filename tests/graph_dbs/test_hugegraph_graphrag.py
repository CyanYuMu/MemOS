"""
Offline GraphRAG integration test for HugeGraphGraphDB.

Validates that:
1. HugeGraphGraphDB is accepted wherever BaseGraphDB is expected
   (GraphMemoryRetriever, Searcher, MemoryManager, etc.)
2. The retrieval pipeline (graph-recall + vector-recall) correctly delegates
   to the graph_store methods defined in BaseGraphDB / HugeGraphGraphDB
3. search_by_embedding, get_nodes, get_by_metadata, get_all_memory_items,
   get_subgraph all wire through without type errors

All HugeGraph / pyhugegraph I/O is mocked — no live server required.
"""

import uuid

from types import SimpleNamespace
from unittest.mock import MagicMock

from memos.graph_dbs.base import BaseGraphDB
from memos.graph_dbs.hugegraph import HugeGraphGraphDB
from memos.memories.textual.item import TextualMemoryItem
from memos.memories.textual.tree_text_memory.retrieve.recall import GraphMemoryRetriever
from memos.memories.textual.tree_text_memory.retrieve.retrieval_mid_structs import ParsedTaskGoal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _node(memory_type="LongTermMemory"):
    nid = str(uuid.uuid4())
    return {"id": nid, "memory": f"memory-{nid[:8]}", "metadata": {"memory_type": memory_type}}


def _make_hugegraph_mock() -> HugeGraphGraphDB:
    """Return a HugeGraphGraphDB instance with all I/O mocked out."""
    config = MagicMock()
    config.ip = "127.0.0.1"
    config.port = 8080
    config.graph = "hugegraph"
    config.graphspace = None
    config.user = None
    config.password = None
    config.user_name = "test_user"
    config.vec_config = None

    instance = HugeGraphGraphDB.__new__(HugeGraphGraphDB)
    instance.config = config
    instance.user_name = "test_user"
    instance._g = MagicMock()
    instance._gremlin = MagicMock()
    instance._vector_index = None
    return instance


# ---------------------------------------------------------------------------
# 1. Type-compatibility: HugeGraphGraphDB is-a BaseGraphDB
# ---------------------------------------------------------------------------


def test_hugegraph_is_subclass_of_base():
    assert issubclass(HugeGraphGraphDB, BaseGraphDB)


def test_hugegraph_accepted_as_graph_store():
    """GraphMemoryRetriever must accept a HugeGraphGraphDB without TypeError."""
    hg = _make_hugegraph_mock()
    embedder = MagicMock()
    retriever = GraphMemoryRetriever(hg, embedder)
    assert retriever.graph_store is hg


# ---------------------------------------------------------------------------
# 2. WorkingMemory path — get_all_memory_items
# ---------------------------------------------------------------------------


def test_retrieve_working_memory_via_hugegraph():
    hg = _make_hugegraph_mock()
    embedder = MagicMock()
    retriever = GraphMemoryRetriever(hg, embedder)

    nodes = [_node("WorkingMemory") for _ in range(3)]
    hg.get_all_memory_items = MagicMock(return_value=nodes)

    results = retriever.retrieve(
        query="",
        parsed_goal=ParsedTaskGoal(keys=[], tags=[]),
        top_k=5,
        memory_scope="WorkingMemory",
    )

    hg.get_all_memory_items.assert_called_once()
    assert len(results) == 3
    assert all(isinstance(r, TextualMemoryItem) for r in results)


# ---------------------------------------------------------------------------
# 3. Graph-recall path — get_by_metadata + get_nodes
# ---------------------------------------------------------------------------


def test_graph_recall_delegates_to_hugegraph():
    hg = _make_hugegraph_mock()
    embedder = MagicMock()
    retriever = GraphMemoryRetriever(hg, embedder)

    n1_id = str(uuid.uuid4())
    n2_id = str(uuid.uuid4())
    hg.get_by_metadata = MagicMock(side_effect=[[n1_id], [n2_id]])
    hg.get_nodes = MagicMock(
        return_value=[
            {"id": n1_id, "memory": "key match", "metadata": {"key": "key1"}},
            {
                "id": n2_id,
                "memory": "tag match",
                "metadata": {"tags": ["tag1", "tag2"]},
            },
        ]
    )

    parsed_goal = ParsedTaskGoal(keys=["key1"], tags=["tag1", "tag2"])
    results = retriever._graph_recall(parsed_goal, "LongTermMemory")

    assert hg.get_by_metadata.call_count >= 1
    assert hg.get_nodes.called
    assert len(results) == 2


# ---------------------------------------------------------------------------
# 4. Vector-recall path — search_by_embedding + get_nodes
# ---------------------------------------------------------------------------


def test_vector_recall_delegates_to_hugegraph():
    hg = _make_hugegraph_mock()
    embedder = MagicMock()
    retriever = GraphMemoryRetriever(hg, embedder)

    n1_id = str(uuid.uuid4())
    hg.search_by_embedding = MagicMock(return_value=[{"id": n1_id, "score": 0.95}])
    hg.get_nodes = MagicMock(return_value=[{"id": n1_id, "memory": "vector hit", "metadata": {}}])

    query_embedding = [[0.1] * 768]
    results = retriever._vector_recall(query_embedding, "LongTermMemory", top_k=5)

    hg.search_by_embedding.assert_called_once()
    assert len(results) == 1
    assert results[0].memory == "vector hit"


# ---------------------------------------------------------------------------
# 5. Full hybrid retrieve — graph + vector merged
# ---------------------------------------------------------------------------


def test_full_hybrid_retrieve_deduplicates():
    hg = _make_hugegraph_mock()
    embedder = MagicMock()
    retriever = GraphMemoryRetriever(hg, embedder)

    shared_id = str(uuid.uuid4())
    unique_id = str(uuid.uuid4())

    hg.get_by_metadata = MagicMock(return_value=[shared_id])
    hg.get_nodes = MagicMock(
        return_value=[
            {"id": shared_id, "memory": "shared node", "metadata": {}},
            {"id": unique_id, "memory": "unique node", "metadata": {}},
        ]
    )
    hg.search_by_embedding = MagicMock(
        return_value=[
            {"id": shared_id, "score": 0.9},
            {"id": unique_id, "score": 0.8},
        ]
    )

    parsed_goal = ParsedTaskGoal(keys=["k"], tags=[])
    results = retriever.retrieve(
        query="test query",
        parsed_goal=parsed_goal,
        top_k=10,
        memory_scope="LongTermMemory",
        query_embedding=[[0.1] * 768],
    )

    ids = [r.id for r in results]
    # Deduplication: each ID appears at most once
    assert len(ids) == len(set(ids))
    assert shared_id in ids


# ---------------------------------------------------------------------------
# 6. get_subgraph wires through (used in tree.py for context retrieval)
# ---------------------------------------------------------------------------


def _center_row(center_id: str, status: str) -> dict:
    return {
        "id": center_id,
        "label": "Memory",
        "memory": "center",
        "user_name": "test_user",
        "status": status,
    }


def test_get_subgraph_accepts_the_call_shape_tree_py_uses():
    """TreeTextMemory.search() calls get_subgraph(center_id=, depth=, center_status=, user_name=).

    Regression: the HugeGraph signature had no ``center_status``, so the live
    recall path raised ``TypeError: got an unexpected keyword argument``. The
    previous version of this test replaced ``get_subgraph`` with a MagicMock and
    then asserted the mock had been called, which is why the missing parameter
    went unnoticed. Drive the real method instead.
    """
    hg = _make_hugegraph_mock()
    center_id, neighbor_id = str(uuid.uuid4()), str(uuid.uuid4())
    # The center read is REST-first (delete-visibility probe, G11): drive the
    # real getVerticesById path instead of relying on the gremlin fallback.
    hg._g.getVerticesById.return_value = [
        SimpleNamespace(properties=_center_row(center_id, "activated"))
    ]

    def _exec(query: str):
        assert "repeat" in query  # the center lookup must NOT fall back to gremlin
        # Shapes probed on the live 1.8.0 HStore: an edge elementMap wraps its
        # endpoints as {"id":…, "label":…} dicts and carries scalar property
        # values, while vertex elementMaps are the same flat map.
        return [
            {
                "objects": [
                    {
                        "id": "edge-1",
                        "label": "MEMORY_RELATION",
                        "OUT": {"id": center_id, "label": "Memory"},
                        "IN": {"id": neighbor_id, "label": "Memory"},
                        "relation_type": "PARENT",
                        "user_name": "test_user",
                    },
                    {
                        "id": neighbor_id,
                        "label": "Memory",
                        "memory": "neighbor",
                        "user_name": "test_user",
                        "status": "archived",
                    },
                ]
            }
        ]

    hg._gremlin.exec.side_effect = _exec

    result = hg.get_subgraph(
        center_id=center_id, depth=2, center_status="activated", user_name="test_user"
    )

    assert result["core_node"]["id"] == center_id
    assert [n["id"] for n in result["neighbors"]] == [neighbor_id]
    assert result["edges"] == [{"source": center_id, "target": neighbor_id, "type": "PARENT"}]


def test_get_subgraph_returns_empty_when_center_status_mismatches():
    """An ineligible center yields no core node and no neighbours, like Neo4j."""
    hg = _make_hugegraph_mock()
    center_id = str(uuid.uuid4())
    hg._g.getVerticesById.return_value = [
        SimpleNamespace(properties=_center_row(center_id, "archived"))
    ]

    result = hg.get_subgraph(
        center_id=center_id, depth=2, center_status="activated", user_name="test_user"
    )

    assert result == {"core_node": None, "neighbors": [], "edges": []}
    hg._gremlin.exec.assert_not_called()  # REST already answered; no fallback


def test_get_subgraph_without_center_status_skips_the_filter():
    """``center_status=None`` disables the gate, matching the Neo4j backend."""
    hg = _make_hugegraph_mock()
    center_id = str(uuid.uuid4())
    hg._g.getVerticesById.return_value = [
        SimpleNamespace(properties=_center_row(center_id, "archived"))
    ]
    hg._gremlin.exec.return_value = []

    result = hg.get_subgraph(center_id=center_id, center_status=None, user_name="test_user")

    assert result["core_node"]["id"] == center_id
